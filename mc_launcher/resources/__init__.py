"""Resource download module (Modrinth / CurseForge)."""

from .download import DownloadError, download_file, download_raw, sha1_file, unique_target
from .http import ApiError, ApiClient, CancelToken, RequestCancelled
from .models import (
    KIND_LABELS,
    NO_LOADER_KINDS,
    RESOURCE_KINDS,
    SOURCE_LABELS,
    SOURCE_NAMES,
    ResourceFile,
    ResourceItem,
    ResourceVersion,
    SearchResult,
)
from .providers import (
    check_mcim,
    game_version_counts,
    provider_for,
    rewrite_cdn,
)

__all__ = [
    "ApiClient",
    "ApiError",
    "CancelToken",
    "DownloadError",
    "KIND_LABELS",
    "NO_LOADER_KINDS",
    "RequestCancelled",
    "RESOURCE_KINDS",
    "ResourceFile",
    "ResourceItem",
    "ResourceVersion",
    "SOURCE_LABELS",
    "SOURCE_NAMES",
    "SearchResult",
    "check_mcim",
    "download_file",
    "download_raw",
    "game_version_counts",
    "provider_for",
    "rewrite_cdn",
    "sha1_file",
    "unique_target",
]
