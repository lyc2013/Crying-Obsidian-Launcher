"""HTTP helpers shared by all resource providers.

All requests carry the launcher User-Agent, honour the configured proxy,
retry a few times and can be cancelled cooperatively.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from .. import __version__
from ..config import APP_NAME
from . import cache


class RequestCancelled(Exception):
    """Raised when a caller cancels an in-flight request."""


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise RequestCancelled("请求已取消")


def user_agent() -> str:
    return f"{APP_NAME}/{__version__}"


# Launcher-wide ceiling: several pages each own a client, but the total number
# of in-flight requests must stay at or below six.
_GLOBAL_SEMAPHORE = threading.BoundedSemaphore(6)


class ApiClient:
    """Small requests wrapper with cache, retries and cancellation."""

    def __init__(
        self, proxy: str | None = None, timeout: float = 20, concurrency: int = 6
    ) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent(),
                "Accept": "application/json, text/plain, */*",
            }
        )
        self.proxy = (proxy or "").strip()
        if self.proxy:
            self.session.proxies.update({"http": self.proxy, "https": self.proxy})
        self._semaphore = threading.BoundedSemaphore(max(1, min(6, int(concurrency or 6))))

    def close(self) -> None:
        self.session.close()

    def get_json(
        self,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        ttl: float = 600,
        token: CancelToken | None = None,
        timeout: float | None = None,
        use_cache: bool = True,
    ) -> Any:
        key = cache.cache_key("GET", url, sorted((params or {}).items()), sorted((headers or {}).items()))
        if use_cache:
            cached = cache.memory_get(key)
            if cached is not None:
                return cached
            if ttl > 0:
                stored = cache.load_json(key, max_age=min(ttl, 3600))
                if stored is not None:
                    cache.memory_set(key, stored, ttl=min(ttl, 600))
                    return stored

        data = self.request("GET", url, params=params, headers=headers, token=token, timeout=timeout)
        try:
            payload = data.json()
        except ValueError as exc:
            raise ApiError(f"响应不是合法 JSON：{url}") from exc
        if use_cache:
            cache.memory_set(key, payload, ttl=ttl)
            if ttl > 0:
                cache.store_json(key, payload)
        return payload

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any = None,
        token: CancelToken | None = None,
        timeout: float | None = None,
        retries: int = 3,
    ) -> requests.Response:
        merged = dict(headers or {})
        last_error: Exception | None = None
        for attempt in range(retries):
            if token is not None:
                token.raise_if_cancelled()
            started = time.time()
            try:
                with _GLOBAL_SEMAPHORE, self._semaphore:
                    response = self.session.request(
                        method,
                        url,
                        params=params,
                        headers=merged,
                        json=json_body,
                        timeout=timeout or self.timeout,
                    )
            except requests.Timeout as exc:
                last_error = ApiError(f"请求超时：{url}")
                if time.time() - started > 0:
                    self._backoff(attempt, token)
                continue
            except requests.RequestException as exc:
                last_error = ApiError(f"网络错误：{exc}")
                self._backoff(attempt, token)
                continue

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After", "")
                delay = 5.0
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    pass
                last_error = ApiError("请求过于频繁（429）", 429)
                self._sleep(min(max(delay, 0.5), 10.0), token)
                continue
            if 500 <= response.status_code < 600:
                last_error = ApiError(f"服务器错误 {response.status_code}", response.status_code)
                self._backoff(attempt, token)
                continue
            if response.status_code >= 400:
                message = _error_message(response)
                raise ApiError(message, response.status_code)
            return response
        raise last_error or ApiError(f"请求失败：{url}")

    def get_bytes(self, url: str, token: CancelToken | None = None) -> bytes:
        response = self.request("GET", url, token=token)
        return response.content

    def stream_download(
        self,
        url: str,
        path,
        on_chunk=None,
        token: CancelToken | None = None,
        timeout: float = 60,
    ) -> None:
        if token is not None:
            token.raise_if_cancelled()
        with self._semaphore:
            with self.session.get(url, stream=True, timeout=timeout) as response:
                if response.status_code >= 400:
                    raise ApiError(f"下载失败 {response.status_code}：{url}", response.status_code)
                total = int(response.headers.get("Content-Length") or 0)
                done = 0
                with open(path, "wb") as handle:
                    for chunk in response.iter_content(64 * 1024):
                        if token is not None:
                            token.raise_if_cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        done += len(chunk)
                        if on_chunk is not None:
                            on_chunk(done, total)

    @staticmethod
    def _backoff(attempt: int, token: CancelToken | None) -> None:
        delay = min(2 ** attempt, 8)
        ApiClient._sleep(delay, token)

    @staticmethod
    def _sleep(seconds: float, token: CancelToken | None) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if token is not None:
                token.raise_if_cancelled()
            time.sleep(min(0.2, max(0.0, deadline - time.time())))


def _error_message(response: requests.Response) -> str:
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("description") or payload.get("error") or "")
    except ValueError:
        detail = (response.text or "")[:200]
    return f"HTTP {response.status_code}" + (f"：{detail}" if detail else "")
