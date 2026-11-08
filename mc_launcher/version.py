"""Version metadata: list versions, cache and summarize a version JSON."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from minecraft_launcher_lib import utils as mc_utils

from .config import (
    HTTP_TIMEOUT_SECONDS,
    MANIFEST_TTL_SECONDS,
    MANIFEST_URL,
    META_DIR,
    USER_AGENT,
    VERSIONS_META_DIR,
    ensure_app_dirs,
)

_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VersionError(Exception):
    """Raised when version metadata cannot be resolved."""


def list_versions() -> list[dict[str, Any]]:
    """Return Mojang's version list (handled by minecraft-launcher-lib)."""
    try:
        return mc_utils.get_version_list()
    except requests.RequestException as exc:
        raise VersionError(f"Failed to fetch version list: {exc}") from exc


def latest_versions() -> dict[str, str]:
    """Return the latest release and snapshot ids."""
    try:
        return mc_utils.get_latest_version()
    except requests.RequestException as exc:
        raise VersionError(f"Failed to fetch latest versions: {exc}") from exc


def _http_get_json(url: str) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise VersionError(f"Failed to fetch {url}: {exc}") from exc

    try:
        return response.json()
    except ValueError as exc:
        raise VersionError(f"Invalid JSON from {url}: {exc}") from exc


def fetch_manifest(refresh: bool = False) -> dict[str, Any]:
    """Fetch version_manifest_v2.json, reusing a local copy for one hour."""
    ensure_app_dirs()
    cache_path = META_DIR / "version_manifest_v2.json"

    if not refresh and cache_path.is_file():
        age = time.time() - cache_path.stat().st_mtime
        if age < MANIFEST_TTL_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

    data = _http_get_json(MANIFEST_URL)
    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _validate_version_id(version_id: str) -> None:
    if not _VERSION_ID_RE.fullmatch(version_id):
        raise VersionError(f"Invalid version id: {version_id!r}")


def fetch_version_json(version_id: str, refresh: bool = False) -> dict[str, Any]:
    """Fetch and cache the official JSON for one version."""
    _validate_version_id(version_id)
    ensure_app_dirs()
    cache_path = VERSIONS_META_DIR / f"{version_id}.json"

    if not refresh and cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    manifest = fetch_manifest(refresh=refresh)
    entry = next((item for item in manifest.get("versions", []) if item.get("id") == version_id), None)
    if entry is None:
        raise VersionError(f"Version {version_id} was not found in Mojang's manifest")

    data = _http_get_json(entry["url"])
    if data.get("id") != version_id:
        raise VersionError(f"Metadata id mismatch: expected {version_id}, got {data.get('id')!r}")

    cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def local_version_json(
    version_id: str, game_dir: str | Path
) -> dict[str, Any] | None:
    """Read the version JSON that is already installed in a game directory."""
    path = Path(game_dir).expanduser() / "versions" / version_id / f"{version_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def inheritance_chain(version_id: str, game_dir: str | Path) -> list[dict[str, Any]]:
    """Return local version JSONs, the requested one first, then its parents."""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current: str | None = version_id
    while current and current not in seen:
        seen.add(current)
        node = local_version_json(current, game_dir)
        if node is None:
            break
        chain.append(node)
        parent = node.get("inheritsFrom")
        current = parent if isinstance(parent, str) else None
    return chain


def resolve_version_data(version_id: str, game_dir: str | Path) -> dict[str, Any]:
    """Merge a local version chain so javaVersion/logging resolve like the game.

    Modded installs (Forge/Fabric/modpacks) usually only store a small child
    JSON and inherit everything else, so Java and logging settings have to be
    looked up in the parent vanilla JSON.
    """
    chain = inheritance_chain(version_id, game_dir)
    merged: dict[str, Any] = {}
    libraries: list[dict[str, Any]] = []
    seen_libraries: set[str] = set()

    for node in chain:  # child first, so the child wins
        for key in ("javaVersion", "logging", "assetIndex", "mainClass", "type", "time", "releaseTime"):
            if key in node and key not in merged:
                merged[key] = node[key]
        for library in node.get("libraries") or []:
            name = library.get("name") if isinstance(library, dict) else None
            if not isinstance(name, str) or name in seen_libraries:
                continue
            seen_libraries.add(name)
            libraries.append(library)

    if libraries:
        merged["libraries"] = libraries
    return merged


def load_version_json(
    version_id: str, game_dir: str | Path | None = None, refresh: bool = False
) -> dict[str, Any]:
    """Prefer the installed version JSON, fall back to Mojang metadata."""
    if game_dir is not None:
        local = resolve_version_data(version_id, game_dir)
        if local:
            return local
    return fetch_version_json(version_id, refresh=refresh)


CATEGORY_RELEASE = "release"
CATEGORY_SNAPSHOT = "snapshot"
CATEGORY_OLD = "old"
CATEGORY_APRIL_FOOLS = "april_fools"

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_RELEASE: "正式版",
    CATEGORY_SNAPSHOT: "预览版",
    CATEGORY_OLD: "远古版",
    CATEGORY_APRIL_FOOLS: "愚人节版",
}

# Standard dev snapshot: 24w05a ; pre-release / release candidate: 1.20.5-pre1
_SNAPSHOT_RE = re.compile(r"^\d{2}w\d{2}[a-z]$")
_PRERELEASE_RE = re.compile(r"^\d+\.\d+(\.\d+)?-(pre|rc)\d+$")


def version_category(version_type: str, version_id: str) -> str:
    """Classify a Mojang version into one of the four launcher categories."""
    version_type = (version_type or "").lower()
    if version_type == "release":
        return CATEGORY_RELEASE
    if version_type in ("old_alpha", "old_beta"):
        return CATEGORY_OLD
    if version_type == "snapshot":
        if _SNAPSHOT_RE.match(version_id) or _PRERELEASE_RE.match(version_id):
            return CATEGORY_SNAPSHOT
        return CATEGORY_APRIL_FOOLS
    return CATEGORY_APRIL_FOOLS


def base_version_id(version_id: str, game_dir: str | Path) -> str | None:
    """Return the vanilla base version id of an installed (possibly modded) version."""
    chain = inheritance_chain(version_id, game_dir)
    if not chain:
        return None
    return chain[-1].get("id")


def instance_category_label(version_id: str, game_dir: str | Path) -> str:
    """Return the category label for an installed instance, based on its base version."""
    base = base_version_id(version_id, game_dir)
    if base is None:
        return "未知"
    try:
        manifest = fetch_manifest()
    except VersionError:
        return "未知"
    entry = next(
        (item for item in manifest.get("versions", []) if item.get("id") == base), None
    )
    if entry is None:
        return "未知"
    return CATEGORY_LABELS.get(version_category(entry.get("type", ""), base), "未知")


def instance_version_info(version_id: str, game_dir: str | Path) -> tuple[str, str]:
    """Return (base_version_id, category_label) for an installed instance."""
    base = base_version_id(version_id, game_dir) or version_id
    category = instance_category_label(version_id, game_dir)
    return base, category


_LOADER_LIBRARIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Fabric", ("net.fabricmc:fabric-loader",)),
    ("Quilt", ("org.quiltmc:quilt-loader",)),
    ("NeoForge", ("net.neoforged:neoforge", "net.neoforged:fmlloader")),
    ("Forge", ("net.minecraftforge:forge", "net.minecraftforge:fmlloader")),
    ("LiteLoader", ("com.mumfrey:liteloader",)),
    ("OptiFine", ("optifine:OptiFine",)),
)


def detect_mod_loader(version_id: str, game_dir: str | Path) -> tuple[str, str | None]:
    """Return (loader name, loader version) for an installed version."""
    chain = inheritance_chain(version_id, game_dir)
    library_names: list[str] = []
    for node in chain:
        for library in node.get("libraries") or []:
            name = library.get("name") if isinstance(library, dict) else None
            if isinstance(name, str):
                library_names.append(name)

    for loader, prefixes in _LOADER_LIBRARIES:
        for name in library_names:
            if any(name.startswith(prefix) for prefix in prefixes):
                parts = name.split(":")
                version = parts[2] if len(parts) > 2 else None
                return loader, version

    lowered = version_id.lower()
    for loader, keyword in (
        ("NeoForge", "neoforge"),
        ("Forge", "forge"),
        ("Fabric", "fabric"),
        ("Quilt", "quilt"),
        ("OptiFine", "optifine"),
    ):
        if keyword in lowered:
            return loader, None
    return "原版", None


def loader_label(version_id: str, game_dir: str | Path) -> str:
    loader, loader_version = detect_mod_loader(version_id, game_dir)
    return f"{loader} {loader_version}" if loader_version else loader


def summarize_version(data: dict[str, Any]) -> dict[str, Any]:
    """Pick the fields the launcher needs from a version JSON."""
    client = data.get("downloads", {}).get("client", {})
    java = data.get("javaVersion", {})
    assets = data.get("assetIndex", {})

    if "arguments" in data:
        argument_format = "arguments"
    elif "minecraftArguments" in data:
        argument_format = "minecraftArguments"
    else:
        argument_format = "unknown"

    return {
        "id": data.get("id"),
        "type": data.get("type"),
        "releaseTime": data.get("releaseTime"),
        "mainClass": data.get("mainClass"),
        "javaVersion": java.get("majorVersion"),
        "javaComponent": java.get("component"),
        "assetIndex": assets.get("id"),
        "libraryCount": len(data.get("libraries", [])),
        "clientSize": client.get("size"),
        "clientSha1": client.get("sha1"),
        "argumentFormat": argument_format,
    }


def fetch_version_summary(version_id: str, refresh: bool = False) -> dict[str, Any]:
    return summarize_version(fetch_version_json(version_id, refresh=refresh))
