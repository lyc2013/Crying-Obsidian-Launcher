"""Resource file downloads with SHA-1 verification and retries."""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Callable

from .http import ApiClient, ApiError, CancelToken, RequestCancelled
from .models import ResourceFile
from .providers import ResourceProvider

ProgressFn = Callable[[int, int], None]
StatusFn = Callable[[str], None]


class DownloadError(Exception):
    pass


def sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return cleaned or "download.bin"


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    index = 1
    while True:
        candidate = path.with_name(f"{stem} ({index}){suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _candidate_urls(provider: ResourceProvider, file: ResourceFile, token: CancelToken | None) -> list[str]:
    urls: list[str] = []
    try:
        resolved = provider.resolve_download_url(file, token=token)
        if resolved:
            urls.append(resolved)
    except ApiError:
        pass
    for url in list(file.fallback_urls) + [file.url]:
        if url and url not in urls:
            urls.append(url)
    return urls


def download_file(
    client: ApiClient,
    provider: ResourceProvider,
    file: ResourceFile,
    target_dir: Path,
    token: CancelToken | None = None,
    on_progress: ProgressFn | None = None,
    on_status: StatusFn | None = None,
    attempts: int = 3,
) -> Path:
    """Download one resource file; retries transient errors with backoff."""
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_filename(file.filename)
    if not target.name:
        raise DownloadError("文件名无效")

    urls = _candidate_urls(provider, file, token)
    if not urls:
        raise DownloadError("作者限制了第三方启动器分发，请到官网手动下载")

    last_error: Exception | None = None
    for attempt in range(attempts):
        if token is not None:
            token.raise_if_cancelled()
        for url in urls:
            temporary = target.with_name(target.name + ".part")
            try:
                if on_status is not None:
                    on_status(f"正在下载 {target.name}")
                client.stream_download(
                    url,
                    temporary,
                    on_chunk=lambda done, total: on_progress(done, total) if on_progress else None,
                    token=token,
                )
                if file.sha1:
                    actual = sha1_file(temporary)
                    if actual.lower() != file.sha1.lower():
                        raise DownloadError(
                            f"SHA-1 校验失败：期望 {file.sha1}，实际 {actual}"
                        )
                temporary.replace(target)
                if on_progress is not None:
                    on_progress(target.stat().st_size, target.stat().st_size)
                return target
            except RequestCancelled:
                temporary.unlink(missing_ok=True)
                raise
            except Exception as exc:  # network error or checksum mismatch
                last_error = exc
                temporary.unlink(missing_ok=True)
                continue
        if attempt + 1 < attempts:
            delay = min(2 ** attempt, 8)
            if on_status is not None:
                on_status(f"下载失败，{delay} 秒后重试（{attempt + 1}/{attempts}）")
            deadline = time.time() + delay
            while time.time() < deadline:
                if token is not None:
                    token.raise_if_cancelled()
                time.sleep(0.2)
    raise DownloadError(str(last_error) if last_error else "下载失败")


def download_raw(
    client: ApiClient,
    url: str,
    target: Path,
    token: CancelToken | None = None,
    on_progress: ProgressFn | None = None,
) -> Path:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    try:
        client.stream_download(
            url,
            temporary,
            on_chunk=lambda done, total: on_progress(done, total) if on_progress else None,
            token=token,
        )
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target
