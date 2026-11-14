"""Two-level cache (memory + disk) for API payloads and images."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import APP_DIR

CACHE_DIR = APP_DIR / "cache" / "resources"
API_CACHE_DIR = CACHE_DIR / "api"
IMAGE_CACHE_DIR = CACHE_DIR / "images"

_memory: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def cache_key(*parts: object) -> str:
    raw = "\u0001".join(str(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def memory_get(key: str) -> Any | None:
    with _lock:
        entry = _memory.get(key)
        if entry is None:
            return None
        expires, value = entry
        if expires < time.time():
            _memory.pop(key, None)
            return None
        return value


def memory_set(key: str, value: Any, ttl: float = 600) -> None:
    with _lock:
        _memory[key] = (time.time() + ttl, value)


def memory_clear() -> None:
    with _lock:
        _memory.clear()


def _api_path(key: str) -> Path:
    return API_CACHE_DIR / f"{key}.json"


def load_json(key: str, max_age: float | None = 3600) -> Any | None:
    path = _api_path(key)
    try:
        if max_age is not None and time.time() - path.stat().st_mtime > max_age:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def store_json(key: str, value: Any) -> None:
    try:
        API_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _api_path(key)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def image_path(url: str) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
        suffix = ".img"
    return IMAGE_CACHE_DIR / f"{digest}{suffix}"


def cached_image(url: str) -> Path | None:
    if not url:
        return None
    path = image_path(url)
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        pass
    return None


def store_image(url: str, data: bytes) -> Path | None:
    try:
        IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = image_path(url)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return path
    except OSError:
        return None


def disk_usage() -> int:
    total = 0
    for directory in (API_CACHE_DIR, IMAGE_CACHE_DIR):
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    return total
