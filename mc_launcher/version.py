"""Version metadata: list versions, cache and summarize a version JSON."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from .config import (
    BMCLAPI_MANIFEST_URL,
    GAME_DIR,
    HTTP_TIMEOUT_SECONDS,
    MANIFEST_TIMEOUT_SECONDS,
    MANIFEST_TTL_SECONDS,
    MANIFEST_URL,
    META_DIR,
    USER_AGENT,
    VERSIONS_META_DIR,
    ensure_app_dirs,
)
from .mirror import mirror_name

logger = logging.getLogger(__name__)

_VERSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class VersionError(Exception):
    """Raised when version metadata cannot be resolved."""


def _manifest_sources() -> list[str]:
    """主源按设置优先，另一个作为回退源。"""
    if mirror_name() == "bmclapi":
        return [BMCLAPI_MANIFEST_URL, MANIFEST_URL]
    return [MANIFEST_URL, BMCLAPI_MANIFEST_URL]


def list_versions() -> list[dict[str, Any]]:
    """Return the normalized version list (same data for every source)."""
    return fetch_manifest()["versions"]


def latest_versions() -> dict[str, str]:
    """Return the latest release and snapshot ids."""
    latest = fetch_manifest().get("latest") or {}
    result = {key: value for key, value in latest.items() if value}
    if "release" in result or "snapshot" in result:
        return result
    # 缓存或镜像缺少 latest 字段时，用版本列表推算
    versions = fetch_manifest()["versions"]
    for entry in versions:
        if entry["type"] == "release":
            result.setdefault("release", entry["id"])
        elif entry["type"] == "snapshot":
            result.setdefault("snapshot", entry["id"])
        if {"release", "snapshot"} <= result.keys():
            break
    return result


def normalize_manifest(data: Any) -> dict[str, Any]:
    """Unify Mojang / BMCLAPI manifests into one identical structure."""
    if not isinstance(data, dict):
        raise VersionError("manifest root is not a JSON object")
    raw_versions = data.get("versions")
    if not isinstance(raw_versions, list) or not raw_versions:
        raise VersionError("manifest has no version entries")

    versions: list[dict[str, str]] = []
    for raw in raw_versions:
        if not isinstance(raw, dict):
            raise VersionError("manifest entry is not a JSON object")
        version_id = raw.get("id")
        version_type = raw.get("type")
        url = raw.get("url")
        release_time = raw.get("releaseTime")
        if not (
            isinstance(version_id, str)
            and version_id
            and isinstance(version_type, str)
            and version_type.strip()
            and isinstance(url, str)
            and url
            and isinstance(release_time, str)
            and release_time
        ):
            raise VersionError("manifest entry is missing required fields")
        versions.append(
            {
                "id": version_id,
                "type": version_type.strip().lower(),
                "releaseTime": release_time,
                "url": url,
            }
        )

    latest_raw = data.get("latest") if isinstance(data.get("latest"), dict) else {}
    latest = {
        key: value
        for key in ("release", "snapshot")
        if isinstance((value := latest_raw.get(key)), str) and value
    }
    return {"latest": latest, "versions": versions}


def _http_get_json(url: str, timeout: float | None = None) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            timeout=timeout or HTTP_TIMEOUT_SECONDS,
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
    """Fetch a normalized manifest from the preferred source, then the other.

    Cache is reused for one hour. If both sources fail, a stale cache is still
    better than nothing, so it is returned instead of raising.
    """
    ensure_app_dirs()
    cache_path = META_DIR / "version_manifest_v2.json"

    cached: dict[str, Any] | None = None
    if cache_path.is_file():
        try:
            cached = normalize_manifest(json.loads(cache_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, VersionError):
            cached = None
        else:
            if not refresh and time.time() - cache_path.stat().st_mtime < MANIFEST_TTL_SECONDS:
                return cached

    errors: list[str] = []
    for url in _manifest_sources():
        try:
            data = _http_get_json(url, timeout=MANIFEST_TIMEOUT_SECONDS)
            manifest = normalize_manifest(data)
        except VersionError as exc:
            errors.append(f"{url}: {exc}")
            continue
        cache_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest

    if cached is not None:
        logger.warning("Both manifest sources failed, using stale cache: %s", "; ".join(errors))
        return cached
    raise VersionError("Failed to fetch version manifest: " + "; ".join(errors))


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


# ---------------------------------------------------------------- categories

CATEGORY_RELEASE = "Release"
CATEGORY_SNAPSHOT = "Snapshot"
CATEGORY_PRERELEASE = "PreRelease"
CATEGORY_RELEASE_CANDIDATE = "ReleaseCandidate"
CATEGORY_ANCIENT = "Ancient"
CATEGORY_APRIL_FOOLS = "AprilFools"
CATEGORY_UNKNOWN = "Unknown"

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_RELEASE: "正式版",
    CATEGORY_SNAPSHOT: "预览版（快照）",
    CATEGORY_PRERELEASE: "预览版（预发布）",
    CATEGORY_RELEASE_CANDIDATE: "预览版（候选）",
    CATEGORY_ANCIENT: "远古版",
    CATEGORY_APRIL_FOOLS: "愚人节版",
    CATEGORY_UNKNOWN: "未知",
}

# 下载页四个分组：分组名 -> 组内的细分分类
DOWNLOAD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("正式版", (CATEGORY_RELEASE,)),
    ("预览版", (CATEGORY_SNAPSHOT, CATEGORY_PRERELEASE, CATEGORY_RELEASE_CANDIDATE)),
    ("远古版", (CATEGORY_ANCIENT,)),
    ("愚人节版", (CATEGORY_APRIL_FOOLS,)),
)

# 愚人节版本白名单（精确匹配，全部按小写比较）
APRIL_FOOLS_IDS = frozenset(
    name.lower()
    for name in (
        "2.0",
        "15w14a",
        "1.RV-Pre1",
        "3D Shareware",
        "3D Shareware v1.34",
        "20w14infinite",
        "22w13oneblockatatime",
        "23w13a_or_b",
        "24w14potato",
        "25w14craftmine",
        "26w14a",
    )
)

# manifest 里 type=snapshot，但历史上属于预发布的版本
HISTORICAL_PRERELEASES = frozenset({"1.4", "1.4.1"})

_SNAPSHOT_WEEK_RE = re.compile(r"^\d{2}w\d{2}[a-z]$")
_SNAPSHOT_SEMANTIC_RE = re.compile(r"-snapshot-", re.IGNORECASE)
_PRERELEASE_RE = re.compile(r"-pre-?\d+", re.IGNORECASE)
_RELEASE_CANDIDATE_RE = re.compile(r"-(?:rc|release-candidate)-?\d+", re.IGNORECASE)
_ANCIENT_AB_RE = re.compile(r"^(?:a|b)\d+\.", re.IGNORECASE)
_ANCIENT_PREFIX_RE = re.compile(
    r"^(?:pre|classic|indev|infdev|alpha|beta)", re.IGNORECASE
)
_RELEASE_NUMERIC_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")

# 旧版启动器的分类逻辑，只在 repair() 里用于生成“错分清单”
_LEGACY_PRERELEASE_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?-(?:pre|rc)\d+$")


def _classify(version_id: str, manifest_type: str | None, local_type: str | None) -> tuple[str, str]:
    """Return (category, reason). 优先级固定，绝不按显示名或目录名判断。"""
    version_id = (version_id or "").strip()
    if not version_id:
        return CATEGORY_UNKNOWN, "版本号为空"

    if version_id.lower() in APRIL_FOOLS_IDS:
        return CATEGORY_APRIL_FOOLS, "愚人节白名单"

    if _ANCIENT_AB_RE.match(version_id) or _ANCIENT_PREFIX_RE.match(version_id):
        return CATEGORY_ANCIENT, "远古版本正则"

    if _SNAPSHOT_WEEK_RE.match(version_id):
        return CATEGORY_SNAPSHOT, "周快照正则"

    if _SNAPSHOT_SEMANTIC_RE.search(version_id):
        return CATEGORY_SNAPSHOT, "语义化快照"

    if _RELEASE_CANDIDATE_RE.search(version_id):
        return CATEGORY_RELEASE_CANDIDATE, "候选版正则"

    if _PRERELEASE_RE.search(version_id):
        return CATEGORY_PRERELEASE, "预发布正则"

    if version_id in HISTORICAL_PRERELEASES:
        return CATEGORY_PRERELEASE, "历史预发布"

    type_hint = (local_type or manifest_type or "").strip().lower()
    if _RELEASE_NUMERIC_RE.match(version_id) and type_hint == "release":
        return CATEGORY_RELEASE, "数字版本号 + manifest.type=release"
    if type_hint == "release":
        return CATEGORY_RELEASE, "manifest.type=release"
    if type_hint == "snapshot":
        return CATEGORY_SNAPSHOT, "manifest.type=snapshot"
    if type_hint in ("old_alpha", "old_beta"):
        return CATEGORY_ANCIENT, f"manifest.type={type_hint}"

    logger.warning(
        "Unknown version category: id=%r manifest_type=%r local_type=%r",
        version_id,
        manifest_type,
        local_type,
    )
    return CATEGORY_UNKNOWN, "无匹配规则"


def classify_version(
    version_id: str, manifest_type: str | None = None, local_type: str | None = None
) -> str:
    """分类一个版本，本地 type 优先于 manifest type，返回统一 category。"""
    return _classify(version_id, manifest_type, local_type)[0]


def download_group(category: str) -> str | None:
    """Return the download page group name for a category."""
    for label, categories in DOWNLOAD_GROUPS:
        if category in categories:
            return label
    return None


def manifest_index(manifest: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    data = manifest if manifest is not None else fetch_manifest()
    return {entry["id"]: entry for entry in data.get("versions", [])}


# --------------------------------------------------------- category metadata

CATEGORY_META_PATH = META_DIR / "version_categories.json"
_category_cache: dict[str, str] | None = None


def _category_meta() -> dict[str, str]:
    global _category_cache
    if _category_cache is None:
        try:
            data = json.loads(CATEGORY_META_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _category_cache = (
            {str(key): str(value) for key, value in data.items()}
            if isinstance(data, dict)
            else {}
        )
    return _category_cache


def _save_category_meta() -> None:
    meta = _category_meta()
    CATEGORY_META_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATEGORY_META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def scan_local_versions(game_dir: str | Path) -> list[dict[str, Any]]:
    """扫描 <game_dir>/versions，读取每个版本的 version.json 元数据。"""
    versions_dir = Path(game_dir).expanduser() / "versions"
    result: list[dict[str, Any]] = []
    if not versions_dir.is_dir():
        return result

    for folder in sorted(versions_dir.iterdir()):
        if not folder.is_dir():
            continue
        preferred = folder / f"{folder.name}.json"
        candidates = sorted(folder.glob("*.json"))
        path = preferred if preferred.is_file() else (candidates[0] if candidates else None)
        if path is None:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        version_id = data.get("id")
        if not isinstance(version_id, str) or not version_id:
            logger.warning("Skip %s: version json has no id", path)
            continue
        parent = data.get("inheritsFrom")
        version_type = data.get("type")
        result.append(
            {
                "id": version_id,
                "type": version_type.lower() if isinstance(version_type, str) and version_type else None,
                "releaseTime": data.get("releaseTime"),
                "inheritsFrom": parent if isinstance(parent, str) and parent else None,
                "path": str(path),
            }
        )
    return result


def _legacy_category(manifest_type: str | None, version_id: str) -> str:
    """旧启动器用的分类，仅用于 repair() 输出对比。"""
    version_type = (manifest_type or "").lower()
    if version_type == "release":
        return CATEGORY_RELEASE
    if version_type in ("old_alpha", "old_beta"):
        return CATEGORY_ANCIENT
    if version_type == "snapshot":
        if _SNAPSHOT_WEEK_RE.match(version_id) or _LEGACY_PRERELEASE_RE.match(version_id):
            return CATEGORY_SNAPSHOT
        return CATEGORY_APRIL_FOOLS
    return CATEGORY_APRIL_FOOLS


def repair(game_dir: str | Path | None = None) -> dict[str, Any]:
    """重新分类本地版本，写回分类元数据，并返回错分清单。

    只修改 meta/version_categories.json，不会删除版本或重新下载文件。
    """
    directory = Path(game_dir).expanduser() if game_dir is not None else GAME_DIR
    local_versions = scan_local_versions(directory)
    if not local_versions:
        return {
            "game_dir": str(directory),
            "scanned": 0,
            "fixed": 0,
            "updated": 0,
            "rows": [],
            "meta_path": str(CATEGORY_META_PATH),
        }

    try:
        manifest = manifest_index()
    except VersionError as exc:
        logger.warning("repair(): manifest unavailable, using local types only: %s", exc)
        manifest = {}

    local_by_id = {item["id"]: item for item in local_versions}
    meta = _category_meta()
    rows: list[dict[str, str]] = []
    computed: dict[str, str] = {}
    updated = 0

    for version_id, item in local_by_id.items():
        parent = item.get("inheritsFrom")
        local_type = item.get("type")
        if local_type is None and parent in local_by_id:
            local_type = local_by_id[parent].get("type")
        manifest_type = (manifest.get(version_id) or {}).get("type")
        if manifest_type is None and parent in manifest:
            manifest_type = manifest[parent].get("type")

        category, reason = _classify(version_id, manifest_type, local_type)
        computed[version_id] = category

        # “当前错误分类”按旧启动器的逻辑计算，便于看清修了什么
        old = _legacy_category(manifest_type, version_id)
        if old != category:
            rows.append(
                {
                    "id": version_id,
                    "old": old,
                    "new": category,
                    "reason": reason,
                }
            )
        if meta.get(version_id) != category:
            updated += 1

    meta.update(computed)
    _save_category_meta()
    return {
        "game_dir": str(directory),
        "scanned": len(computed),
        "fixed": len(rows),
        "updated": updated,
        "rows": rows,
        "meta_path": str(CATEGORY_META_PATH),
    }


def version_category_for(
    version_id: str, game_dir: str | Path, manifest: dict[str, dict[str, Any]] | None = None
) -> str:
    """Return the stored/derived category for a version in a game directory."""
    cached = _category_meta().get(version_id)
    if cached and cached != CATEGORY_UNKNOWN:
        return cached

    local = local_version_json(version_id, game_dir)
    local_type = local.get("type") if isinstance(local, dict) else None
    if manifest is None:
        try:
            manifest = manifest_index()
        except VersionError:
            manifest = {}
    manifest_type = (manifest.get(version_id) or {}).get("type")

    category = classify_version(version_id, manifest_type, local_type)
    if category != CATEGORY_UNKNOWN:
        _category_meta()[version_id] = category
        _save_category_meta()
    return category


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
    category = version_category_for(base, game_dir)
    return CATEGORY_LABELS.get(category, "未知")


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
