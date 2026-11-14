"""Platform-neutral data models for the resource browser."""

from __future__ import annotations

from dataclasses import dataclass, field

RESOURCE_KINDS = ("mod", "modpack", "shader", "resourcepack", "datapack")

KIND_LABELS = {
    "mod": "模组",
    "modpack": "整合包",
    "shader": "光影",
    "resourcepack": "资源包",
    "datapack": "数据包",
}

# Resource kinds that do not need a mod loader at all.
NO_LOADER_KINDS = ("modpack", "shader", "resourcepack", "datapack")

SOURCE_LABELS = {
    "curseforge": "CF",
    "modrinth": "MR",
    "modrinth_official": "MR",
}

SOURCE_NAMES = {
    "curseforge": "CurseForge",
    "modrinth": "Modrinth（MCIM 镜像）",
    "modrinth_official": "Modrinth 官方",
}


@dataclass
class ResourceFile:
    filename: str
    url: str | None = None
    size: int = 0
    sha1: str | None = None
    file_id: str | None = None
    version_id: str | None = None
    project_id: str | None = None
    published: str | None = None
    release_type: str = "release"
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    downloads: int = 0
    fallback_urls: list[str] = field(default_factory=list)
    primary: bool = False


@dataclass
class ResourceVersion:
    id: str
    name: str
    version_number: str
    published: str | None = None
    release_type: str = "release"
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    downloads: int = 0
    featured: bool = False
    changelog: str | None = None
    files: list[ResourceFile] = field(default_factory=list)
    dependencies: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    def primary_file(self) -> ResourceFile | None:
        for item in self.files:
            if item.primary:
                return item
        return self.files[0] if self.files else None


@dataclass
class ResourceItem:
    id: str
    slug: str
    title: str
    author: str
    summary: str
    icon_url: str | None = None
    downloads: int = 0
    follows: int = 0
    created: str | None = None
    updated: str | None = None
    categories: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    source: str = "modrinth"
    kind: str = "mod"
    body: str | None = None
    body_format: str = "markdown"
    gallery: list[str] = field(default_factory=list)
    website: str | None = None
    source_url: str | None = None
    license: str | None = None
    translated_summary: str | None = None
    translated_body: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class SearchResult:
    items: list[ResourceItem]
    total: int
    offset: int = 0
    limit: int = 20


@dataclass
class DependencyEntry:
    project_id: str | None
    title: str
    relation: str  # required / optional / incompatible
    icon_url: str | None = None
