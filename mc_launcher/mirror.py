"""Optional BMCLAPI mirror support for minecraft-launcher-lib.

The library has no built-in mirror option: its download hosts are hardcoded or
read from Mojang's version JSON. All of its downloads go through requests, so
selecting BMCLAPI here rewrites Mojang hosts at the HTTP layer. That covers
version metadata, libraries, assets, client.jar and the Java runtime without
modifying the library itself.
"""

from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.exceptions import RequestException


class DownloadAborted(RequestException):
    """Raised when the user cancels an install."""


_ABORT = threading.Event()

BMCLAPI_BASE = os.environ.get("MC_BMCLAPI_BASE", "https://bmclapi2.bangbang93.com").rstrip("/")

# Mojang host -> path prefix on BMCLAPI.
_HOST_TO_PREFIX = {
    "launchermeta.mojang.com": "",
    "piston-meta.mojang.com": "",
    "piston-data.mojang.com": "",
    "libraries.minecraft.net": "/maven",
    "resources.download.minecraft.net": "/assets",
}

_ORIGINAL_REQUEST = None
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_RETRIES = max(1, int(os.environ.get("MC_MIRROR_RETRIES", "5")))
_TIMEOUT = float(os.environ.get("MC_MIRROR_TIMEOUT", "30"))
_FALLBACK = os.environ.get("MC_MIRROR_FALLBACK", "1") != "0"


def mirror_name() -> str:
    return os.environ.get("MC_MIRROR", "bmclapi").strip().lower() or "bmclapi"


def set_mirror(name: str) -> None:
    """Switch the download source at runtime (mojang or bmclapi)."""
    os.environ["MC_MIRROR"] = name.strip().lower()


def using_bmclapi() -> bool:
    return mirror_name() == "bmclapi"


def rewrite_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    prefix = _HOST_TO_PREFIX.get(host)
    if prefix is None:
        return url

    base = urlsplit(BMCLAPI_BASE)
    return urlunsplit((base.scheme, base.netloc, prefix + parts.path, parts.query, parts.fragment))


def enable_mirror() -> None:
    """Wrap requests so every download has a timeout and can be cancelled.

    The wrapper is installed once, regardless of the mirror, because the
    library never passes a timeout and would otherwise wait forever on a
    stalled connection.
    """
    global _ORIGINAL_REQUEST
    if _ORIGINAL_REQUEST is not None:
        return

    _ORIGINAL_REQUEST = requests.Session.request

    def request_with_mirror(session, method, url, *args, **kwargs):
        if _ABORT.is_set():
            raise DownloadAborted("download aborted by user")

        kwargs.setdefault("timeout", _TIMEOUT)
        # The wrapper stays installed after switching back to Mojang, so the
        # source has to be checked on every call, not just when patching.
        if not using_bmclapi() or method.upper() not in ("GET", "HEAD"):
            return _ORIGINAL_REQUEST(session, method, url, *args, **kwargs)

        rewritten = rewrite_url(url)
        if rewritten == url:
            return _ORIGINAL_REQUEST(session, method, rewritten, *args, **kwargs)

        last_error: RequestException | None = None
        last_response = None
        for attempt in range(_RETRIES):
            try:
                response = _ORIGINAL_REQUEST(session, method, rewritten, *args, **kwargs)
            except RequestException as exc:
                last_error = exc
                if attempt < _RETRIES - 1:
                    time.sleep(0.5 * (2**attempt))
                    continue
                break

            # BMCLAPI redirects to signed CDN URLs; 403/429/5xx can be transient.
            if response.status_code in _RETRYABLE_STATUS:
                if attempt < _RETRIES - 1:
                    response.close()
                    time.sleep(0.5 * (2**attempt))
                    continue
                last_response = response
                break

            return response

        if _FALLBACK:
            if last_response is not None:
                last_response.close()
            try:
                return _ORIGINAL_REQUEST(session, method, url, *args, **kwargs)
            except RequestException:
                if last_error is not None:
                    raise last_error
                raise

        if last_response is not None:
            return last_response
        assert last_error is not None
        raise last_error

    requests.Session.request = request_with_mirror  # type: ignore[method-assign]


def abort_downloads() -> None:
    """Make pending and queued downloads fail fast so an install can stop."""
    _ABORT.set()


def reset_downloads() -> None:
    _ABORT.clear()


def downloads_aborted() -> bool:
    return _ABORT.is_set()
