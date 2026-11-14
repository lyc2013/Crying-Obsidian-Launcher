"""Resource providers for Modrinth (official/MCIM) and CurseForge (MCIM).

The UI only talks to :class:`ResourceProvider`; platform quirks stay here.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit, urlunsplit

from .http import ApiClient, ApiError, CancelToken
from .mapper import (
    cf_file,
    cf_item,
    cf_loaders_from_game_versions,
    cf_page_limit,
    cf_release_type_of,
    cf_version,
    dedupe_loaders,
    dependency_relation,
    modrinth_dependencies,
    modrinth_item,
    modrinth_version,
    page_offset,
    sort_versions_desc,
)
from .models import ResourceFile, ResourceItem, ResourceVersion, SearchResult

MODRINTH_API = "https://api.modrinth.com"
MODRINTH_MIRROR = "https://mod.mcimirror.top/modrinth"
MODRINTH_CDN = "https://cdn.modrinth.com"
MCIM_ROOT = "https://mod.mcimirror.top"
CURSEFORGE_API = "https://api.curseforge.com"
CURSEFORGE_MIRROR = "https://mod.mcimirror.top/curseforge"

MODRINTH_TYPES = {
    "mod": "mod",
    "modpack": "modpack",
    "shader": "shader",
    "resourcepack": "resourcepack",
    "datapack": "datapack",
}

CURSEFORGE_CLASSES = {
    "mod": 6,
    "modpack": 4471,
    "shader": 6552,
    "resourcepack": 12,
    "datapack": 6945,
}

SORTS = ("popularity", "updated", "follows", "name")

_CF_MOD_LOADER_IDS = {
    "Forge": 1,
    "Cauldron": 2,
    "LiteLoader": 3,
    "Fabric": 4,
    "Quilt": 5,
    "NeoForge": 6,
}


def build_modrinth_facets(project_type: str) -> list[list[str]]:
    """Modrinth facets: inner lists are OR, outer lists are AND."""
    return [[f"project_type:{project_type}"]]


def rewrite_cdn(url: str | None) -> str | None:
    """Point Modrinth/CurseForge CDN links at the MCIM mirror."""
    if not url:
        return url
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == "cdn.modrinth.com":
        return MCIM_ROOT + parts.path + (f"?{parts.query}" if parts.query else "")
    if host in ("edge.forgecdn.net", "media.forgecdn.net"):
        return MCIM_ROOT + parts.path + (f"?{parts.query}" if parts.query else "")
    return url


def _json_param(value) -> str:
    return json.dumps(value, separators=(",", ":"))


class ResourceProvider:
    source = "modrinth"
    kind_names: dict[str, str] = {}

    def __init__(self, client: ApiClient) -> None:
        self.client = client

    def search(
        self,
        kind: str,
        query: str,
        page: int,
        page_size: int,
        sort: str = "popularity",
        token: CancelToken | None = None,
    ) -> SearchResult:
        raise NotImplementedError

    def get_project(
        self,
        kind: str,
        project_id: str,
        seed: ResourceItem | None = None,
        token: CancelToken | None = None,
    ) -> ResourceItem:
        raise NotImplementedError

    def get_versions(
        self,
        kind: str,
        project_id: str,
        game_version: str | None = None,
        loader: str | None = None,
        token: CancelToken | None = None,
    ) -> list[ResourceVersion]:
        raise NotImplementedError

    def resolve_download_url(
        self, file: ResourceFile, token: CancelToken | None = None
    ) -> str:
        if file.url:
            return file.url
        raise ApiError("该文件没有可用的下载地址")

    def get_file(
        self, project_id: str, file_id: str, token: CancelToken | None = None
    ) -> ResourceFile | None:
        return None

    def get_dependencies(
        self, item: ResourceItem, token: CancelToken | None = None
    ) -> list[dict]:
        return []

    def close(self) -> None:
        self.client.close()


class ModrinthProvider(ResourceProvider):
    kind_names = MODRINTH_TYPES

    def __init__(self, client: ApiClient, mirror: bool = True) -> None:
        super().__init__(client)
        self.mirror = mirror
        self.base = MODRINTH_MIRROR if mirror else MODRINTH_API
        self.source = "modrinth" if mirror else "modrinth_official"
        self.used_official_fallback = False

    def _url(self, path: str) -> str:
        return self.base + path

    def _get(self, path: str, params: dict | None = None, token: CancelToken | None = None,
             ttl: float = 600):
        try:
            return self.client.get_json(self._url(path), params=params, token=token, ttl=ttl)
        except (ApiError, OSError) as exc:
            if not self.mirror:
                raise
            self.used_official_fallback = True
            return self.client.get_json(
                MODRINTH_API + path, params=params, token=token, ttl=ttl
            )

    def _localize(self, item: ResourceItem) -> ResourceItem:
        if self.mirror:
            item.icon_url = rewrite_cdn(item.icon_url)
            item.gallery = [rewrite_cdn(url) or url for url in item.gallery]
        return item

    def search(
        self,
        kind: str,
        query: str,
        page: int,
        page_size: int,
        sort: str = "popularity",
        token: CancelToken | None = None,
    ) -> SearchResult:
        project_type = self.kind_names.get(kind, "mod")
        facets = build_modrinth_facets(project_type)
        index = {
            "popularity": "downloads",
            "updated": "updated",
            "follows": "follows",
            "name": "downloads",
        }.get(sort, "downloads")
        offset = page_offset(page, page_size)
        params = {
            "query": query,
            "facets": _json_param(facets),
            "index": index,
            "offset": offset,
            "limit": page_size,
        }
        payload = self._get("/v2/search", params=params, token=token)
        hits = payload.get("hits") or []
        items = [
            self._localize(modrinth_item(hit, kind))
            for hit in hits
            if isinstance(hit, dict)
        ]
        if sort == "name":
            items.sort(key=lambda item: item.title.lower())
        total = int(payload.get("total_hits") or 0)
        return SearchResult(items=items, total=total, offset=params["offset"], limit=page_size)

    def get_project(
        self,
        kind: str,
        project_id: str,
        seed: ResourceItem | None = None,
        token: CancelToken | None = None,
    ) -> ResourceItem:
        payload = self._get(f"/v2/project/{project_id}", token=token, ttl=600)
        item = seed or ResourceItem(
            id=project_id, slug=project_id, title=payload.get("title", project_id),
            author="", summary="", kind=kind, source=self.source,
        )
        item.kind = kind
        item.source = self.source
        from .mapper import apply_modrinth_project

        apply_modrinth_project(item, payload)
        if not item.author:
            try:
                members = self._get(
                    f"/v2/project/{project_id}/members", token=token, ttl=600
                )
                authors = [
                    str(entry.get("user", {}).get("username") or "")
                    for entry in members or []
                    if isinstance(entry, dict)
                ]
                authors = [name for name in authors if name]
                item.author = authors[0] if authors else ""
            except ApiError:
                pass
        return self._localize(item)

    def get_versions(
        self,
        kind: str,
        project_id: str,
        game_version: str | None = None,
        loader: str | None = None,
        token: CancelToken | None = None,
    ) -> list[ResourceVersion]:
        params: dict[str, str] = {}
        if game_version:
            params["game_versions"] = _json_param([game_version])
        if loader:
            params["loaders"] = _json_param([loader.lower()])
        payload = self._get(
            f"/v2/project/{project_id}/version",
            params=params or None,
            token=token,
            ttl=300,
        )
        versions = [modrinth_version(entry) for entry in payload or [] if isinstance(entry, dict)]
        versions.sort(key=lambda version: str(version.published or ""), reverse=True)
        return versions

    def resolve_download_url(
        self, file: ResourceFile, token: CancelToken | None = None
    ) -> str:
        url = file.url or ""
        if not url:
            raise ApiError("该文件没有可用的下载地址")
        return rewrite_cdn(url) if self.mirror else url

    def get_dependencies(
        self, item: ResourceItem, token: CancelToken | None = None
    ) -> list[dict]:
        versions = self.get_versions(item.kind, item.id, token=token)
        dependencies = modrinth_dependencies(versions)
        ids = [entry["project_id"] for entry in dependencies]
        if not ids:
            return []
        titles: dict[str, dict] = {}
        try:
            payload = self._get(
                "/v2/projects",
                params={"ids": _json_param(ids)},
                token=token,
                ttl=600,
            )
            for project in payload or []:
                if isinstance(project, dict) and project.get("id"):
                    titles[str(project["id"])] = project
        except ApiError:
            pass
        result = []
        for entry in dependencies:
            project = titles.get(entry["project_id"], {})
            result.append(
                {
                    "project_id": entry["project_id"],
                    "title": str(project.get("title") or entry["project_id"]),
                    "icon_url": project.get("icon_url"),
                    "relation": dependency_relation(entry["relation"], "modrinth"),
                }
            )
        return result


class CurseForgeProvider(ResourceProvider):
    kind_names = CURSEFORGE_CLASSES

    def __init__(
        self, client: ApiClient, api_key: str = "", mirror: bool = True
    ) -> None:
        super().__init__(client)
        self.api_key = (api_key or "").strip()
        self.mirror = mirror
        self.source = "curseforge"

    def _attempts(self) -> list[tuple[str, str | None]]:
        """Official (with key) first, then the mirror without any key."""
        attempts: list[tuple[str, str | None]] = []
        if not self.mirror and self.api_key:
            attempts.append((CURSEFORGE_API, self.api_key))
            attempts.append((CURSEFORGE_MIRROR, None))
        else:
            attempts.append((CURSEFORGE_MIRROR, None))
            if self.api_key:
                attempts.append((CURSEFORGE_API, self.api_key))
        return attempts

    def _localize(self, item: ResourceItem) -> ResourceItem:
        item.icon_url = rewrite_cdn(item.icon_url)
        item.gallery = [rewrite_cdn(url) or url for url in item.gallery]
        return item

    def _localize_file(self, file: ResourceFile) -> ResourceFile:
        if file.url:
            rewritten = rewrite_cdn(file.url)
            if rewritten and rewritten != file.url:
                file.fallback_urls.append(file.url)
                file.url = rewritten
        return file

    def _localize_version(self, version: ResourceVersion) -> ResourceVersion:
        for file in version.files:
            self._localize_file(file)
        return version

    def _get(self, path: str, params: dict | None = None, token: CancelToken | None = None,
             ttl: float = 600):
        errors: list[str] = []
        for base, key in self._attempts():
            headers = {"x-api-key": key} if key else {}
            try:
                return self.client.get_json(
                    base + path, params=params, headers=headers, token=token, ttl=ttl
                )
            except ApiError as exc:
                errors.append(str(exc))
                continue
        raise ApiError("CurseForge 请求失败：" + ("；".join(errors) or "未知错误"))

    def search(
        self,
        kind: str,
        query: str,
        page: int,
        page_size: int,
        sort: str = "popularity",
        token: CancelToken | None = None,
    ) -> SearchResult:
        sort_field = {"popularity": 6, "updated": 3, "follows": 2, "name": 4}.get(sort, 6)
        offset = page_offset(page, page_size)
        params: dict[str, object] = {
            "gameId": 432,
            "classId": self.kind_names.get(kind, 6),
            "sortField": sort_field,
            "sortOrder": "desc",
            "index": offset,
            "pageSize": page_size,
        }
        if query:
            params["searchFilter"] = query
        payload = self._get("/v1/mods/search", params=params, token=token, ttl=300)
        data = payload.get("data") or []
        items = [
            self._localize(cf_item(entry, kind))
            for entry in data
            if isinstance(entry, dict)
        ]
        pagination = payload.get("pagination") or {}
        total = min(int(pagination.get("totalCount") or 0), 10000)
        return SearchResult(items=items, total=total, offset=offset, limit=page_size)

    def get_project(
        self,
        kind: str,
        project_id: str,
        seed: ResourceItem | None = None,
        token: CancelToken | None = None,
    ) -> ResourceItem:
        payload = self._get(f"/v1/mods/{project_id}", token=token, ttl=600)
        data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if not isinstance(data, dict):
            raise ApiError("CurseForge 返回了无效的项目数据")
        item = cf_item(data, kind)
        if not item.body:
            # The MCIM mirror omits "description"; the dedicated endpoint is
            # only available on the official API.
            try:
                payload = self._get(
                    f"/v1/mods/{project_id}/description", token=token, ttl=600
                )
                text = payload.get("data") if isinstance(payload, dict) else payload
                if isinstance(text, str):
                    item.body = text
            except ApiError:
                pass
        if seed is not None:
            item.gallery = item.gallery or seed.gallery
            item.icon_url = item.icon_url or seed.icon_url
            item.author = item.author or seed.author
        item.source = self.source
        return self._localize(item)

    def get_versions(
        self,
        kind: str,
        project_id: str,
        game_version: str | None = None,
        loader: str | None = None,
        token: CancelToken | None = None,
    ) -> list[ResourceVersion]:
        params: dict[str, object] = {"pageSize": 50, "index": 0}
        if game_version:
            params["gameVersion"] = game_version
        if loader:
            loader_id = _CF_MOD_LOADER_IDS.get(loader)
            if loader_id:
                params["modLoaderType"] = loader_id
        payload = self._get(
            f"/v1/mods/{project_id}/files", params=params, token=token, ttl=300
        )
        data = payload.get("data") or []
        versions: list[ResourceVersion] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if not entry.get("isAvailable", True):
                continue
            if int(entry.get("fileStatus") or 0) not in (4, 10):
                continue
            if loader and loader not in cf_loaders_from_game_versions(entry.get("gameVersions")):
                continue
            if game_version and game_version not in (entry.get("gameVersions") or []):
                continue
            versions.append(self._localize_version(cf_version(project_id, entry)))
        versions.sort(key=lambda version: int(version.id or 0), reverse=True)
        return versions

    def resolve_download_url(
        self, file: ResourceFile, token: CancelToken | None = None
    ) -> str:
        if file.url:
            return rewrite_cdn(file.url) if self.mirror else file.url
        if not file.project_id or not file.file_id:
            raise ApiError("缺少文件标识，无法获取下载地址")
        payload = self._get(
            f"/v1/mods/{file.project_id}/files/{file.file_id}/download-url",
            token=token,
            ttl=120,
        )
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, str) or not data:
            raise ApiError("作者限制了第三方启动器分发，请到 CurseForge 官网手动下载")
        rewritten = rewrite_cdn(data)
        if rewritten and rewritten != data:
            file.fallback_urls.append(data)
        return rewritten or data

    def get_file(
        self, project_id: str, file_id: str, token: CancelToken | None = None
    ) -> ResourceFile | None:
        payload = self._get(
            f"/v1/mods/{project_id}/files/{file_id}", token=token, ttl=300
        )
        data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
        if not isinstance(data, dict):
            return None
        file = cf_file(project_id, data, cf_release_type_of(data))
        return self._localize_file(file)

    def get_dependencies(
        self, item: ResourceItem, token: CancelToken | None = None
    ) -> list[dict]:
        raw = item.raw or {}
        files = raw.get("latestFiles") or []
        merged: dict[tuple[str, str], dict] = {}
        for file in files[:3]:
            if not isinstance(file, dict):
                continue
            for entry in file.get("dependencies") or []:
                if not isinstance(entry, dict):
                    continue
                relation = dependency_relation(entry.get("relationType"))
                if relation not in ("required", "optional", "incompatible"):
                    continue
                mod_id = entry.get("modId")
                if not mod_id:
                    continue
                merged.setdefault(
                    (str(mod_id), relation),
                    {"project_id": str(mod_id), "relation": relation, "title": "", "icon_url": None},
                )
        result = []
        for entry in merged.values():
            try:
                project = self.get_project(item.kind, entry["project_id"], token=token)
                entry["title"] = project.title
                entry["icon_url"] = project.icon_url
            except ApiError:
                entry["title"] = f"项目 {entry['project_id']}"
            result.append(entry)
        return result


def provider_for(
    source: str, api_key: str = "", proxy: str = "", concurrency: int = 6
) -> ResourceProvider:
    client = ApiClient(proxy=proxy, concurrency=concurrency)
    if source == "curseforge":
        return CurseForgeProvider(client, api_key=api_key, mirror=False)
    if source == "modrinth_official":
        return ModrinthProvider(client, mirror=False)
    return ModrinthProvider(client, mirror=True)


def check_mcim(timeout: float = 8) -> bool:
    """Quick health check used to show which source is actually in use."""
    from . import cache

    key = "mcim-health"
    cached = cache.memory_get(key)
    if cached is not None:
        return bool(cached)
    client = ApiClient(timeout=timeout)
    try:
        client.get_json(
            MODRINTH_MIRROR + "/v2/search",
            params={"query": "", "limit": 1},
            ttl=0,
            use_cache=False,
            timeout=timeout,
        )
        ok = True
    except Exception:
        ok = False
    finally:
        client.close()
    cache.memory_set(key, ok, ttl=60)
    return ok


def game_version_counts(versions: list[ResourceVersion]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for version in versions:
        for name in version.game_versions:
            if name:
                counts[name] = counts.get(name, 0) + 1
    ordered = {}
    for name in sort_versions_desc(counts):
        ordered[name] = counts[name]
    return ordered
