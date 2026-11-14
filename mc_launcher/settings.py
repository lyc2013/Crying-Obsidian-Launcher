"""Persistent launcher settings: instances, accounts, Java paths and defaults."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import APP_DIR

SETTINGS_PATH = APP_DIR / "settings.json"

DEFAULT_MAX_MEMORY = 2048
DEFAULT_MIN_MEMORY = 512
MIRRORS = ("mojang", "bmclapi")

ISOLATION_GLOBAL = "global"
ISOLATION_FORCE_ON = "force_on"
ISOLATION_FORCE_OFF = "force_off"
ISOLATION_OVERRIDES = (ISOLATION_GLOBAL, ISOLATION_FORCE_ON, ISOLATION_FORCE_OFF)


@dataclass
class GameDirectory:
    """A Minecraft root the launcher knows about, with its own isolation rule."""

    id: str
    path: str
    isolation_override: str = ISOLATION_GLOBAL
    isolation_detected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "isolationOverride": self.isolation_override,
            "isolationDetected": self.isolation_detected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GameDirectory | None":
        path = str(data.get("path") or "").strip()
        if not path:
            return None
        override = str(data.get("isolationOverride") or ISOLATION_GLOBAL)
        if override not in ISOLATION_OVERRIDES:
            override = ISOLATION_GLOBAL
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            path=path,
            isolation_override=override,
            isolation_detected=bool(data.get("isolationDetected", False)),
        )


@dataclass
class Instance:
    """One launchable profile: a version inside a game directory."""

    id: str
    name: str
    version: str
    game_dir: str
    max_memory: int | None = None
    min_memory: int | None = None
    jvm_args: str = ""
    java_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "game_dir": self.game_dir,
            "max_memory": self.max_memory,
            "min_memory": self.min_memory,
            "jvm_args": self.jvm_args,
            "java_path": self.java_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Instance | None":
        version = str(data.get("version") or "").strip()
        game_dir = str(data.get("game_dir") or "").strip()
        if not version or not game_dir:
            return None

        def optional_int(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) else None

        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            name=str(data.get("name") or version),
            version=version,
            game_dir=game_dir,
            max_memory=optional_int("max_memory"),
            min_memory=optional_int("min_memory"),
            jvm_args=str(data.get("jvm_args") or ""),
            java_path=str(data["java_path"]) if data.get("java_path") else None,
        )


@dataclass
class Settings:
    instances: list[Instance] = field(default_factory=list)
    game_dirs: list[GameDirectory] = field(default_factory=list)
    default_instance_id: str | None = None
    accounts: list[str] = field(default_factory=list)
    default_account: str | None = None
    java_paths: list[str] = field(default_factory=list)
    default_java: str | None = None
    mirror: str = "bmclapi"
    max_memory: int = DEFAULT_MAX_MEMORY
    min_memory: int = DEFAULT_MIN_MEMORY
    show_log: bool = False
    instance_isolation: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "instances": [item.to_dict() for item in self.instances],
            "game_dirs": [item.to_dict() for item in self.game_dirs],
            "default_instance_id": self.default_instance_id,
            "accounts": self.accounts,
            "default_account": self.default_account,
            "java_paths": self.java_paths,
            "default_java": self.default_java,
            "mirror": self.mirror,
            "max_memory": self.max_memory,
            "min_memory": self.min_memory,
            "show_log": self.show_log,
            "instanceIsolation": self.instance_isolation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        instances = [
            item
            for item in (Instance.from_dict(raw) for raw in data.get("instances", []) if isinstance(raw, dict))
            if item is not None
        ]
        game_dirs = [
            item
            for item in (
                GameDirectory.from_dict(raw)
                for raw in data.get("game_dirs", [])
                if isinstance(raw, dict)
            )
            if item is not None
        ]

        def int_or(default: int, key: str) -> int:
            value = data.get(key)
            return value if isinstance(value, int) and value > 0 else default

        mirror = str(data.get("mirror") or "bmclapi").lower()
        if mirror not in MIRRORS:
            mirror = "bmclapi"

        accounts = [str(name) for name in data.get("accounts", []) if str(name).strip()]
        settings = cls(
            instances=instances,
            game_dirs=game_dirs,
            default_instance_id=data.get("default_instance_id"),
            accounts=accounts,
            default_account=data.get("default_account"),
            java_paths=[str(path) for path in data.get("java_paths", []) if str(path).strip()],
            default_java=str(data["default_java"]) if data.get("default_java") else None,
            mirror=mirror,
            max_memory=int_or(DEFAULT_MAX_MEMORY, "max_memory"),
            min_memory=int_or(DEFAULT_MIN_MEMORY, "min_memory"),
            show_log=bool(data.get("show_log", False)),
            instance_isolation=bool(data.get("instanceIsolation", True)),
        )
        settings.sync_game_dirs()
        settings.resolve_defaults()
        return settings

    def sync_game_dirs(self) -> None:
        """Register every directory referenced by an instance (migration)."""
        for instance in self.instances:
            self.register_game_dir(instance.game_dir)

        # Directories saved before isolation detection existed follow the disk
        # layout once, then keep whatever the user chooses afterwards.
        for entry in self.game_dirs:
            if not entry.isolation_detected:
                entry.isolation_override = detect_isolation_override(entry.path)
                entry.isolation_detected = True

    def register_game_dir(self, path: str | os.PathLike[str]) -> GameDirectory:
        existing = self.find_game_dir(path)
        if existing is not None:
            return existing

        directory = GameDirectory(
            id=uuid.uuid4().hex[:8],
            path=str(path),
            isolation_override=detect_isolation_override(path),
            isolation_detected=True,
        )
        self.game_dirs.append(directory)
        return directory

    def find_game_dir(self, path: str | os.PathLike[str]) -> GameDirectory | None:
        key = _path_key(path)
        for entry in self.game_dirs:
            if _path_key(entry.path) == key:
                return entry
        return None

    def import_instances(
        self, game_dir: str | os.PathLike[str], versions: list[str]
    ) -> list[Instance]:
        """Create one instance per installed version, without touching game files."""
        key = _path_key(game_dir)
        known_versions = {
            item.version for item in self.instances if _path_key(item.game_dir) == key
        }
        known_names = {
            item.name for item in self.instances if _path_key(item.game_dir) == key
        }

        created: list[Instance] = []
        for version in versions:
            if not version or version in known_versions:
                continue

            name = version
            suffix = 2
            while name in known_names:
                name = f"{version} ({suffix})"
                suffix += 1

            instance = self.add_instance(name, version, game_dir)
            known_names.add(instance.name)
            known_versions.add(version)
            created.append(instance)
        return created

    def game_dir_for_instance(self, instance: Instance) -> GameDirectory:
        return self.register_game_dir(instance.game_dir)

    def get_effective_isolation(self, game_dir_id: str | None) -> bool:
        """Directory override wins; otherwise the global toggle applies."""
        entry = next((item for item in self.game_dirs if item.id == game_dir_id), None)
        if entry is None:
            return self.instance_isolation
        if entry.isolation_override == ISOLATION_FORCE_ON:
            return True
        if entry.isolation_override == ISOLATION_FORCE_OFF:
            return False
        return self.instance_isolation

    def isolation_for_instance(self, instance: Instance) -> bool:
        return self.get_effective_isolation(self.game_dir_for_instance(instance).id)

    def effective_game_root(self, instance: Instance) -> Path:
        """The directory Minecraft should use as its root for this launch."""
        base = Path(instance.game_dir).expanduser()
        if not self.isolation_for_instance(instance):
            return base

        folder = _sanitize_folder_name(instance.name) or instance.id
        return base / "instances" / folder

    def resolve_defaults(self) -> None:
        """Keep default ids/names pointing at things that still exist."""
        if not any(item.id == self.default_instance_id for item in self.instances):
            self.default_instance_id = self.instances[0].id if self.instances else None
        if self.default_account not in self.accounts:
            self.default_account = self.accounts[0] if self.accounts else None
        if self.min_memory > self.max_memory:
            self.min_memory = self.max_memory

    def default_instance(self) -> Instance | None:
        for item in self.instances:
            if item.id == self.default_instance_id:
                return item
        return self.instances[0] if self.instances else None

    def add_instance(
        self, name: str, version: str, game_dir: str | os.PathLike[str]
    ) -> Instance:
        instance = Instance(
            id=uuid.uuid4().hex[:8],
            name=name.strip() or version,
            version=version.strip(),
            game_dir=str(game_dir),
        )
        self.instances.append(instance)
        self.register_game_dir(instance.game_dir)
        if self.default_instance_id is None:
            self.default_instance_id = instance.id
        return instance

    def remove_instance(self, instance_id: str) -> None:
        self.instances = [item for item in self.instances if item.id != instance_id]
        self.resolve_defaults()

    def set_default_instance(self, instance_id: str) -> None:
        if any(item.id == instance_id for item in self.instances):
            self.default_instance_id = instance_id


def _path_key(path: str | os.PathLike[str]) -> str:
    expanded = Path(os.path.expanduser(str(path)))
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = expanded.absolute()
    return os.path.normcase(str(resolved))


def detect_isolation_override(game_dir: str | os.PathLike[str]) -> str:
    """Follow how the directory is already used on disk.

    A directory that already contains an `instances/` folder is treated as
    isolated, any other directory stays shared so existing mods/saves keep working.
    """
    root = Path(game_dir).expanduser()
    return ISOLATION_FORCE_ON if (root / "instances").is_dir() else ISOLATION_FORCE_OFF


def _sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return cleaned[:64]


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()

    if not isinstance(data, dict):
        return Settings()
    return Settings.from_dict(data)


def save_settings(settings: Settings) -> None:
    settings.resolve_defaults()
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, SETTINGS_PATH)
