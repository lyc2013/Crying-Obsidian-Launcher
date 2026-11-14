"""Field mapping between Modrinth / CurseForge payloads and our models.

Everything platform specific lives here so the UI never needs to know which
API produced an item.
"""

from __future__ import annotations

import html
import math
import re
from collections import OrderedDict
from datetime import datetime, timezone

from .models import (
    NO_LOADER_KINDS,
    ResourceFile,
    ResourceItem,
    ResourceVersion,
)

LOADER_ORDER = ("Forge", "NeoForge", "Fabric", "Quilt", "LiteLoader", "Rift", "Cauldron")

_LOADER_ALIASES = {
    "forge": "Forge",
    "neoforge": "NeoForge",
    "neo-forge": "NeoForge",
    "neo forge": "NeoForge",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "liteloader": "LiteLoader",
    "lite-loader": "LiteLoader",
    "lite loader": "LiteLoader",
    "rift": "Rift",
    "cauldron": "Cauldron",
}

CF_LOADER_ENUM = {
    0: None,
    1: "Forge",
    2: "Cauldron",
    3: "LiteLoader",
    4: "Fabric",
    5: "Quilt",
    6: "NeoForge",
}

CF_RELEASE_TYPES = {1: "release", 2: "beta", 3: "alpha"}
MR_RELEASE_TYPES = ("release", "beta", "alpha")

CF_RELATION = {
    1: "embedded",
    2: "optional",
    3: "required",
    4: "tool",
    5: "incompatible",
    6: "include",
}


def normalize_loader(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return CF_LOADER_ENUM.get(value)
    text = str(value).strip().lower()
    if not text:
        return None
    return _LOADER_ALIASES.get(text)


def dedupe_loaders(values) -> list[str]:
    found: set[str] = set()
    for value in values:
        name = normalize_loader(value)
        if name:
            found.add(name)
    return [name for name in LOADER_ORDER if name in found]


def cf_loaders_from_indexes(indexes) -> list[str]:
    return dedupe_loaders(
        entry.get("modLoader")
        for entry in (indexes or [])
        if isinstance(entry, dict)
    )


def cf_loaders_from_game_versions(game_versions) -> list[str]:
    return dedupe_loaders(game_versions or [])


def display_loaders(kind: str, loaders: list[str]) -> list[str]:
    """Chips shown on a card. Non-mod resources never need a loader."""
    if kind in NO_LOADER_KINDS:
        return ["无需加载器"]
    return loaders or ["未标注"]


def format_count(value: object) -> str:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return "0"
    if number < 0:
        number = 0
    if number >= 1_000_000_000:
        text, unit = f"{number / 1_000_000_000:.1f}", "B"
    elif number >= 1_000_000:
        text, unit = f"{number / 1_000_000:.1f}", "M"
    elif number >= 1_000:
        text, unit = f"{number / 1_000:.1f}", "K"
    else:
        return str(number)
    if text.endswith(".0"):
        text = text[:-2]
    return text + unit


def format_size(size: object) -> str:
    try:
        number = int(size)
    except (TypeError, ValueError):
        return "—"
    if number <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if number < 1024 or unit == "GB":
            if unit == "B":
                return f"{number} B"
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} GB"


def format_date(value: object) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d")


def version_key(version: str) -> tuple:
    """Segment-wise numeric comparison, never plain string sorting.

    Final releases rank above pre-releases/RCs of the same base version.
    """
    text = str(version or "")
    lower = text.lower()
    is_preview = bool(
        re.search(r"-(?:pre|rc|snapshot)-?\d", lower)
        or re.fullmatch(r"\d{2}w\d{2}[a-z]", lower)
        or "-snapshot-" in lower
    )
    # Ignore the pre-release suffix when comparing numbers, otherwise
    # "1.20.2-pre3" would outrank "1.20.2" on the fourth segment.
    base_text = re.split(r"-(?:pre|rc|snapshot)", lower, maxsplit=1)[0]
    parts = re.findall(r"\d+", base_text)
    numbers = tuple(int(part) for part in parts[:5])
    numbers += (0,) * (5 - len(numbers))
    return numbers + (1 if is_preview else 2, text)


def sort_versions_desc(versions) -> list[str]:
    return sorted({str(item) for item in versions if str(item)}, key=version_key, reverse=True)


def group_game_versions(versions) -> OrderedDict[str, int]:
    counts: dict[str, int] = {}
    for name in versions:
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    ordered = OrderedDict()
    for name in sort_versions_desc(counts):
        ordered[name] = counts[name]
    return ordered


def modrinth_item(hit: dict, kind: str) -> ResourceItem:
    categories = [str(item) for item in hit.get("categories") or []]
    loaders = dedupe_loaders(categories)
    gallery = []
    for entry in hit.get("gallery") or []:
        if isinstance(entry, str):
            gallery.append(entry)
        elif isinstance(entry, dict) and entry.get("url"):
            gallery.append(str(entry["url"]))
    return ResourceItem(
        id=str(hit.get("project_id") or ""),
        slug=str(hit.get("slug") or hit.get("project_id") or ""),
        title=str(hit.get("title") or "未命名"),
        author=str(hit.get("author") or ""),
        summary=str(hit.get("description") or ""),
        icon_url=hit.get("icon_url"),
        downloads=int(hit.get("downloads") or 0),
        follows=int(hit.get("follows") or 0),
        created=hit.get("date_created"),
        updated=hit.get("date_modified"),
        categories=categories,
        loaders=loaders,
        game_versions=list(hit.get("versions") or []),
        source="modrinth",
        kind=kind,
        gallery=gallery,
        license=str(hit["license"]) if isinstance(hit.get("license"), str) else None,
        raw=hit,
    )


def apply_modrinth_project(item: ResourceItem, project: dict) -> ResourceItem:
    item.title = str(project.get("title") or item.title)
    item.slug = str(project.get("slug") or item.slug)
    item.summary = str(project.get("description") or item.summary)
    item.icon_url = project.get("icon_url") or item.icon_url
    item.downloads = int(project.get("downloads") or item.downloads)
    item.follows = int(project.get("follows") or item.follows)
    item.created = project.get("published") or project.get("date_created") or item.created
    item.updated = project.get("updated") or project.get("date_modified") or item.updated
    item.body = project.get("body")
    item.body_format = "markdown"
    item.website = f"https://modrinth.com/{item.kind}/{item.slug}"
    item.source_url = project.get("source_url")
    item.license = str(project.get("license", {}).get("id") or "") or item.license
    item.categories = [str(entry) for entry in project.get("categories") or item.categories]
    if "loaders" in project:
        item.loaders = dedupe_loaders(project.get("loaders") or [])
    if "game_versions" in project:
        item.game_versions = list(project.get("game_versions") or [])
    gallery = []
    for entry in project.get("gallery") or []:
        if isinstance(entry, dict) and entry.get("url"):
            gallery.append(str(entry["url"]))
        elif isinstance(entry, str):
            gallery.append(entry)
    if gallery:
        item.gallery = gallery
    for key in ("translated_description", "translated_summary", "translated_body"):
        value = project.get(key)
        if isinstance(value, str) and value.strip():
            if key == "translated_summary":
                item.translated_summary = value
            else:
                item.translated_body = value
    item.raw = project
    return item


def modrinth_version(data: dict) -> ResourceVersion:
    files = []
    version_id = str(data.get("id") or "")
    for raw in data.get("files") or []:
        if not isinstance(raw, dict):
            continue
        hashes = raw.get("hashes") or {}
        files.append(
            ResourceFile(
                filename=str(raw.get("filename") or ""),
                url=raw.get("url"),
                size=int(raw.get("size") or 0),
                sha1=hashes.get("sha1"),
                file_id=str(raw.get("hashes", {}).get("sha1") or raw.get("filename") or ""),
                version_id=version_id,
                published=data.get("date_published"),
                release_type=str(data.get("version_type") or "release"),
                game_versions=list(data.get("game_versions") or []),
                loaders=dedupe_loaders(data.get("loaders") or []),
                dependencies=[
                    dict(entry)
                    for entry in data.get("dependencies") or []
                    if isinstance(entry, dict)
                ],
                downloads=int(data.get("downloads") or 0),
                primary=bool(raw.get("primary")),
            )
        )
    return ResourceVersion(
        id=version_id,
        name=str(data.get("name") or data.get("version_number") or ""),
        version_number=str(data.get("version_number") or data.get("name") or ""),
        published=data.get("date_published"),
        release_type=str(data.get("version_type") or "release"),
        game_versions=list(data.get("game_versions") or []),
        loaders=dedupe_loaders(data.get("loaders") or []),
        downloads=int(data.get("downloads") or 0),
        featured=bool(data.get("featured", False)),
        changelog=data.get("changelog"),
        files=files,
        dependencies=[
            dict(entry)
            for entry in data.get("dependencies") or []
            if isinstance(entry, dict)
        ],
        raw=data,
    )


def modrinth_dependencies(versions: list[ResourceVersion], limit: int = 3) -> list[dict]:
    """Aggregate the dependency entries of the newest few versions."""
    merged: dict[tuple[str, str], dict] = {}
    for version in versions[:limit]:
        for entry in version.dependencies:
            project_id = entry.get("project_id")
            relation = str(entry.get("dependency_type") or "required")
            if not project_id:
                continue
            key = (str(project_id), relation)
            merged.setdefault(
                key,
                {"project_id": str(project_id), "relation": relation, "title": ""},
            )
    return list(merged.values())


def cf_item(raw: dict, kind: str) -> ResourceItem:
    logo = raw.get("logo") or {}
    if isinstance(logo, dict):
        icon = logo.get("thumbnailUrl") or logo.get("url")
    else:
        icon = None
    authors = raw.get("authors") or []
    author = ""
    if authors and isinstance(authors[0], dict):
        author = str(authors[0].get("name") or "")
    categories = [
        str(entry.get("name"))
        for entry in raw.get("categories") or []
        if isinstance(entry, dict) and entry.get("name")
    ]
    indexes = raw.get("latestFilesIndexes") or []
    game_versions = sort_versions_desc(
        entry.get("gameVersion")
        for entry in indexes
        if isinstance(entry, dict) and entry.get("gameVersion")
    )
    links = raw.get("links") or {}
    screenshots = []
    for entry in raw.get("screenshots") or []:
        if isinstance(entry, dict):
            url = entry.get("thumbnailUrl") or entry.get("url")
            if url:
                screenshots.append(str(url))
    description = raw.get("description")
    summary = str(raw.get("summary") or "")
    translated_summary = raw.get("translatedSummary") or raw.get("translated_summary")
    translated_body = raw.get("translatedDescription") or raw.get("translated_description")
    return ResourceItem(
        id=str(raw.get("id") or ""),
        slug=str(raw.get("slug") or raw.get("id") or ""),
        title=str(raw.get("name") or "未命名"),
        author=author,
        summary=summary,
        icon_url=icon,
        downloads=int(raw.get("downloadCount") or 0),
        follows=int(raw.get("thumbsUpCount") or 0),
        created=raw.get("dateCreated"),
        updated=raw.get("dateModified") or raw.get("dateReleased"),
        categories=categories,
        loaders=cf_loaders_from_indexes(indexes),
        game_versions=game_versions,
        source="curseforge",
        kind=kind,
        body=str(description) if isinstance(description, str) else None,
        body_format="html",
        gallery=screenshots,
        website=links.get("websiteUrl"),
        source_url=links.get("sourceUrl"),
        license=str(raw.get("licenseType")),
        translated_summary=(
            str(translated_summary) if isinstance(translated_summary, str) else None
        ),
        translated_body=(
            str(translated_body) if isinstance(translated_body, str) else None
        ),
        raw=raw,
    )


def cf_file(mod_id: str, raw: dict, release_type: str | None = None) -> ResourceFile:
    hashes = raw.get("hashes") or []
    sha1 = None
    for entry in hashes:
        if isinstance(entry, dict) and int(entry.get("algo") or 0) == 1:
            sha1 = entry.get("value")
            break
    game_versions = []
    loaders = []
    for value in raw.get("gameVersions") or []:
        text = str(value)
        if normalize_loader(text):
            loaders.append(text)
        elif re.fullmatch(r"\d+(?:\.\d+){1,3}[a-z]?|\d{2}w\d{2}[a-z]", text.strip()):
            game_versions.append(text)
    if not release_type:
        release_type = CF_RELEASE_TYPES.get(int(raw.get("releaseType") or 0), "release")
    file_id = str(raw.get("id") or "")
    return ResourceFile(
        filename=str(raw.get("fileName") or ""),
        url=raw.get("downloadUrl"),
        size=int(raw.get("fileLength") or 0),
        sha1=sha1,
        file_id=file_id,
        version_id=file_id,
        project_id=mod_id,
        published=raw.get("fileDate"),
        release_type=release_type,
        game_versions=sort_versions_desc(game_versions),
        loaders=dedupe_loaders(loaders),
        dependencies=[
            dict(entry)
            for entry in raw.get("dependencies") or []
            if isinstance(entry, dict)
        ],
        downloads=int(raw.get("downloadCount") or 0),
    )


def cf_version(mod_id: str, raw: dict) -> ResourceVersion:
    file = cf_file(mod_id, raw)
    file_id = str(raw.get("id") or "")
    return ResourceVersion(
        id=file_id,
        name=str(raw.get("displayName") or raw.get("fileName") or ""),
        version_number=str(raw.get("displayName") or raw.get("fileName") or ""),
        published=raw.get("fileDate"),
        release_type=CF_RELEASE_TYPES.get(int(raw.get("releaseType") or 0), "release"),
        game_versions=file.game_versions,
        loaders=file.loaders,
        downloads=int(raw.get("downloadCount") or 0),
        files=[file],
        dependencies=file.dependencies,
        raw=raw,
    )


def cf_release_type_of(raw: dict) -> str:
    return CF_RELEASE_TYPES.get(int(raw.get("releaseType") or 0), "release")


def dependency_relation(value: object, platform: str = "curseforge") -> str:
    if platform == "modrinth":
        text = str(value or "required").lower()
        return text if text in ("required", "optional", "incompatible") else "required"
    try:
        code = int(value)
    except (TypeError, ValueError):
        return "required"
    return CF_RELATION.get(code, "required")


def slug_title(text: str, limit: int = 40) -> str:
    plain = re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()
    return plain[:limit] + ("…" if len(plain) > limit else "")


def sanitize_html(text: str) -> str:
    """Very small XSS guard for CurseForge descriptions.

    QTextBrowser does not execute JavaScript, but it still renders remote
    images and links; strip scripts, event handlers and javascript: URLs.
    """
    cleaned = re.sub(
        r"<\s*(script|iframe|object|embed|style)[^>]*>.*?<\s*/\s*\1\s*>",
        "",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"<\s*(script|iframe|object|embed|style)[^>]*/?>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"(href|src)\s*=\s*([\"'])\s*javascript:[^\"']*\2",
        r"\1=\2#\2",
        cleaned,
        flags=re.IGNORECASE,
    )
    return cleaned


def page_offset(page: int, page_size: int) -> int:
    """Both APIs use a zero-based item offset instead of a page number."""
    if page_size <= 0:
        return 0
    return max(0, int(page)) * int(page_size)


def total_pages(total: int, page_size: int) -> int:
    if page_size <= 0:
        return 1
    return max(1, math.ceil(max(0, total) / page_size))


def cf_page_limit(total: int, page_size: int) -> int:
    """CurseForge truncates totalCount at 10000; keep offsets below it."""
    if page_size <= 0:
        return 1
    capped = min(max(0, total), 10000)
    return max(1, math.ceil(capped / page_size))
