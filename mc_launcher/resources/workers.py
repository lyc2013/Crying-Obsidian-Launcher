"""QThread helpers used by the resource browser."""

from __future__ import annotations

import time
from typing import Callable

from PySide6.QtCore import QThread, Signal

from .http import CancelToken, RequestCancelled


class TaskThread(QThread):
    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, task: Callable, token: CancelToken | None = None, parent=None) -> None:
        super().__init__(parent)
        self._task = task
        self.token = token or CancelToken()

    def run(self) -> None:
        try:
            result = self._task()
        except RequestCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.done.emit(result)

    def cancel(self) -> None:
        self.token.cancel()


class DownloadWorker(QThread):
    progress = Signal(int, int, float)
    status = Signal(str)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, task: Callable, parent=None) -> None:
        super().__init__(parent)
        self._task = task
        self.token = CancelToken()
        self.result: object = None
        self._last_time = 0.0
        self._last_bytes = 0
        self._speed = 0.0

    def callback(self, done: int, total: int) -> None:
        now = time.time()
        elapsed = now - self._last_time
        if elapsed >= 0.4:
            delta = max(0, done - self._last_bytes)
            self._speed = delta / elapsed if elapsed > 0 else 0.0
            self._last_time = now
            self._last_bytes = done
        self.progress.emit(done, total, self._speed)

    def run(self) -> None:
        try:
            result = self._task(self.token, self.callback, self.status.emit)
        except RequestCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.result = result
            # Only a real path is useful to the queue row; structured results
            # (modpack) are read from ``worker.result``.
            if isinstance(result, str):
                payload = result
            elif hasattr(result, "__fspath__"):
                payload = str(result)
            else:
                payload = ""
            self.succeeded.emit(payload)

    def cancel(self) -> None:
        self.token.cancel()
