"""Game directory layout abstraction: PCL and MultiMC styles.

A *game directory* is the folder that holds one or more Minecraft versions.
Two layouts are supported:

* ``pcl``    — ``<dir>/.minecraft`` contains versions, libraries, assets and mods.
* ``multimc`` — versions live in ``<dir>/.minecraft``, while libraries, assets
  and the runtime are shared at ``<dir>/``.

Every path used by the launcher must be obtained from :class:`Layout`; business
code must never join ``.minecraft`` or ``versions`` itself.

Per-directory style is stored in ``<dir>/.launcher_profile`` (JSON).  A marker
file travels with the directory and survives moves/renames, unlike a settings
map keyed by absolute paths.  Automatic detection writes a marker with
``locked: false``; a manual choice writes ``locked: true``.  Whenever a marker
exists it wins and detection is never repeated.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import APP_DIR

STYLE_PCL = "pcl"
STYLE_MULTIMC = "multimc"
STYLES = (STYLE_PCL, STYLE_MULTIMC)

MARKER_NAME = ".launcher_profile"
MARKER_VERSION = 1

SOURCE_MARKER = "marker"
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
SOURCE_DEFAULT = "default"
SOURCE_LEGACY = "legacy"

_LEGACY_NAMES = (".minecraft", "minecraft")


class LayoutError(Exception):
    """Raised when an install/launch path view cannot be prepared."""


@dataclass(frozen=True)
class LayoutInfo:
    style: str
    source: str
    recognized: bool
    marker: Path


def _expanded(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser()


def marker_path(directory: str | os.PathLike[str]) -> Path:
    return _expanded(directory) / MARKER_NAME


def read_marker(directory: str | os.PathLike[str]) -> dict | None:
    """Read and validate the per-directory marker, if present."""
    path = marker_path(directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    style = str(data.get("profile") or "").lower()
    if style not in STYLES:
        return None
    return data


def write_marker(
    directory: str | os.PathLike[str], style: str, *, locked: bool
) -> Path:
    if style not in STYLES:
        raise ValueError(f"Unknown layout style: {style}")
    target = _expanded(directory)
    target.mkdir(parents=True, exist_ok=True)
    path = target / MARKER_NAME
    payload = {"profile": style, "version": MARKER_VERSION, "locked": bool(locked)}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
    return path


def _client_candidates(directory: Path) -> list[Path]:
    """Possible Minecraft client folders, ``.minecraft`` first."""
    candidates = [directory / name for name in _LEGACY_NAMES]
    return [candidate for candidate in candidates if candidate.is_dir()]


def _has_version_jar(client: Path) -> bool:
    versions = client / "versions"
    if not versions.is_dir():
        return False
    try:
        for entry in versions.iterdir():
            if entry.is_dir() and (entry / f"{entry.name}.jar").is_file():
                return True
    except OSError:
        return False
    return False


def detect_style(directory: str | os.PathLike[str]) -> str | None:
    """Detect the layout of an existing directory (rules a -> d)."""
    root = _expanded(directory)
    if not root.is_dir():
        return None

    # a. MultiMC/Prism instances always ship instance.cfg.
    if (root / "instance.cfg").is_file():
        return STYLE_MULTIMC

    candidates = _client_candidates(root)
    for client in candidates:
        # b. PCL: jar inside .minecraft plus .minecraft/libraries & assets.
        if (
            _has_version_jar(client)
            and (client / "libraries").is_dir()
            and (client / "assets").is_dir()
        ):
            return STYLE_PCL
        # c. MultiMC: libraries live next to .minecraft, not inside it.
        if (root / "libraries").is_dir() and not (client / "libraries").is_dir():
            return STYLE_MULTIMC
    return None


def resolve_layout(
    directory: str | os.PathLike[str],
    default_style: str = STYLE_PCL,
    *,
    persist: bool = True,
) -> tuple["Layout", LayoutInfo]:
    """Resolve the style of a directory, writing an automatic marker once."""
    root = _expanded(directory)
    style = default_style if default_style in STYLES else STYLE_PCL

    # Selecting a .minecraft folder itself keeps the legacy (pre-group) layout.
    if root.name.lower() in _LEGACY_NAMES:
        return (
            Layout(root, STYLE_PCL, direct=True),
            LayoutInfo(STYLE_PCL, SOURCE_LEGACY, True, root / MARKER_NAME),
        )

    marker = read_marker(root)
    if marker is not None:
        marked = str(marker["profile"])
        # An unlocked marker was written by automatic detection; a locked one
        # is an explicit user choice.
        source = SOURCE_MANUAL if marker.get("locked") else SOURCE_AUTO
        return Layout(root, marked), LayoutInfo(marked, source, True, root / MARKER_NAME)

    detected = detect_style(root)
    if detected is not None:
        if persist:
            write_marker(root, detected, locked=False)
        return (
            Layout(root, detected),
            LayoutInfo(detected, SOURCE_AUTO, True, root / MARKER_NAME),
        )

    return (
        Layout(root, style),
        LayoutInfo(style, SOURCE_DEFAULT, False, root / MARKER_NAME),
    )


def set_style(directory: str | os.PathLike[str], style: str) -> LayoutInfo:
    """Manually set and lock a directory's layout; never touches game files."""
    if style not in STYLES:
        raise ValueError(f"Unknown layout style: {style}")
    root = _expanded(directory)
    write_marker(root, style, locked=True)
    return LayoutInfo(style, SOURCE_MANUAL, True, root / MARKER_NAME)


class Layout:
    """All filesystem paths for one game directory."""

    def __init__(
        self,
        directory: str | os.PathLike[str],
        style: str = STYLE_PCL,
        *,
        direct: bool = False,
        game_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.directory = _expanded(directory)
        self.style = style if style in STYLES else STYLE_PCL
        self.direct = bool(direct) or self.directory.name.lower() in _LEGACY_NAMES
        if self.direct:
            # Old settings store the client root itself (e.g. ~/.minecraft).
            self.style = STYLE_PCL
            self.client_dir = self.directory
        else:
            self.client_dir = self._resolve_client_dir()
        self.game_root = _expanded(game_root) if game_root is not None else self.client_dir

    def _resolve_client_dir(self) -> Path:
        for name in _LEGACY_NAMES:
            candidate = self.directory / name
            if candidate.is_dir():
                return candidate
        return self.directory / _LEGACY_NAMES[0]

    # ------------------------------------------------------------------ paths
    @property
    def versions_dir(self) -> Path:
        return self.client_dir / "versions"

    @property
    def libraries_dir(self) -> Path:
        if self.style == STYLE_MULTIMC and not self.direct:
            return self.directory / "libraries"
        return self.client_dir / "libraries"

    @property
    def assets_dir(self) -> Path:
        if self.style == STYLE_MULTIMC and not self.direct:
            return self.directory / "assets"
        return self.client_dir / "assets"

    @property
    def runtime_dir(self) -> Path:
        if self.style == STYLE_MULTIMC and not self.direct:
            return self.directory / "runtime"
        return self.client_dir / "runtime"

    @property
    def mods_dir(self) -> Path:
        return self.game_root / "mods"

    @property
    def resourcepacks_dir(self) -> Path:
        return self.game_root / "resourcepacks"

    @property
    def shaderpacks_dir(self) -> Path:
        return self.game_root / "shaderpacks"

    @property
    def saves_dir(self) -> Path:
        return self.game_root / "saves"

    @property
    def instances_root(self) -> Path:
        return self.client_dir / "instances"

    def version_dir(self, version_id: str) -> Path:
        return self.versions_dir / version_id

    def version_json(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.json"

    def version_jar(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.jar"

    def datapacks_dir(self, save_name: str) -> Path:
        return self.saves_dir / save_name / "datapacks"

    def with_game_root(self, path: str | os.PathLike[str]) -> "Layout":
        return Layout(
            self.directory,
            self.style,
            direct=self.direct,
            game_root=path,
        )

    # ------------------------------------------------------------- install view
    def install_view(self) -> Path:
        """A directory the Minecraft library can use as ``minecraft_directory``.

        PCL already matches the library's single-root assumption.  MultiMC
        splits versions from libraries/assets, so a launcher-owned view with
        directory links is used instead of touching the user's directory.
        """
        if self.style != STYLE_MULTIMC or self.direct:
            return self.client_dir

        digest = hashlib.sha1(
            str(self.directory.resolve()).encode("utf-8", "ignore")
        ).hexdigest()[:12]
        view = APP_DIR / "layout_views" / f"{digest}-{self.style}"
        view.mkdir(parents=True, exist_ok=True)

        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.libraries_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

        for name, target in (
            ("versions", self.versions_dir),
            ("libraries", self.libraries_dir),
            ("assets", self.assets_dir),
            ("runtime", self.runtime_dir),
        ):
            self._ensure_directory_link(view / name, target)
        return view

    def _ensure_directory_link(self, link: Path, target: Path) -> None:
        if link.exists() or link.is_symlink():
            try:
                if link.resolve() == target.resolve():
                    return
            except OSError:
                pass
            self._remove_link(link)
        self._create_directory_link(link, target)

    @staticmethod
    def _remove_link(path: Path) -> None:
        try:
            if path.is_symlink() or os.path.islink(path):
                path.unlink()
            elif path.is_dir():
                # Windows junctions are removed with rmdir; never recurse into
                # the target directory.
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            raise LayoutError(f"无法更新布局视图 {path}：{exc}") from exc

    @staticmethod
    def _create_directory_link(link: Path, target: Path) -> None:
        try:
            os.symlink(target, link, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            pass

        if sys.platform.startswith("win"):
            try:
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                raise LayoutError(
                    f"无法创建目录联接 {link} -> {target}：{exc}\n"
                    "请以普通用户身份重试，或手动创建该目录联接。"
                ) from exc
            if result.returncode == 0:
                return
            raise LayoutError(
                f"无法创建目录联接 {link} -> {target}："
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

        raise LayoutError(
            f"无法创建符号链接 {link} -> {target}；"
            "请检查文件系统是否支持符号链接，或以管理员身份运行。"
        )
