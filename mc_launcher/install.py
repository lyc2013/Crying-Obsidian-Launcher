"""Install game files through minecraft-launcher-lib, with progress and checks."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, TextIO

from minecraft_launcher_lib import install as mc_install
from minecraft_launcher_lib import runtime
from minecraft_launcher_lib.exceptions import VersionNotFound

from .config import GAME_DIR
from .java import JavaError, resolve_java
from .mirror import DownloadAborted, enable_mirror, reset_downloads
from .version import fetch_version_json


class InstallError(Exception):
    """Raised when installation or verification fails."""


@dataclass(frozen=True)
class InstallResult:
    version: str
    game_dir: Path
    client_jar: Path
    client_sha1: str
    asset_index_id: str | None
    asset_count: int
    java_executable: Path | None


class ProgressPrinter:
    """Render minecraft-launcher-lib progress callbacks on a CLI."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._tty = self._stream.isatty()
        self._lock = threading.Lock()
        self._status = ""
        self._maximum = 0
        self._last_bucket = -1
        self._last_width = 0

    def callback(self) -> dict[str, object]:
        return {
            "setStatus": self.set_status,
            "setProgress": self.set_progress,
            "setMax": self.set_max,
        }

    def set_status(self, text: str) -> None:
        with self._lock:
            if is_file_status(text):
                if self._tty:
                    self._render(text)
                return

            self._status = text
            if self._tty:
                self._render(text)
            else:
                print(text, file=self._stream, flush=True)

    def set_max(self, maximum: int) -> None:
        with self._lock:
            self._maximum = max(0, int(maximum))
            self._last_bucket = -1

    def set_progress(self, value: int) -> None:
        value = int(value)
        with self._lock:
            if self._maximum <= 0:
                return

            percent = min(100, int(value * 100 / self._maximum))
            if self._tty:
                self._render(f"{self._status}  {value}/{self._maximum} ({percent}%)")
                return

            bucket = percent // 10
            if bucket != self._last_bucket:
                self._last_bucket = bucket
                print(
                    f"{self._status}  {percent}% ({value}/{self._maximum})",
                    file=self._stream,
                    flush=True,
                )

    def finish(self) -> None:
        with self._lock:
            if self._tty and self._last_width:
                self._stream.write("\n")
                self._stream.flush()
                self._last_width = 0

    def _render(self, line: str) -> None:
        padding = max(0, self._last_width - len(line))
        self._stream.write("\r" + line + " " * padding)
        self._stream.flush()
        self._last_width = len(line)


_PHASE_STATUSES = {
    "Download Libraries",
    "Download Assets",
    "Install java runtime",
    "Installation complete",
    "Installation failed",
}


def is_file_status(text: str) -> bool:
    """Per-file statuses from the library, hidden when output is not a terminal."""
    return text.startswith("Download ") and text not in _PHASE_STATUSES


def sha1_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextlib.contextmanager
def _maybe_skip_jvm(
    version_data: dict,
    install_java: bool | None,
    progress: ProgressPrinter,
    java_path: Path | str | None = None,
    java_paths: list[str] | None = None,
) -> Iterator[None]:
    """Skip the Mojang JRE download when a compatible Java is available."""
    java_version = version_data.get("javaVersion") or {}
    component = java_version.get("component")
    required_major = java_version.get("majorVersion")
    if not component:
        yield
        return

    if install_java is False:
        skip = True
        reason = "disabled by user"
    elif install_java is None:
        skip = False
        reason = ""
        # A manually selected Java wins over auto-detection; if it is unusable,
        # fall back to whatever the system scan finds.
        candidates: list[tuple[Path | str | None, str]] = []
        if java_path:
            candidates.append((java_path, "selected"))
        for extra in java_paths or []:
            if extra:
                candidates.append((extra, "added"))
        candidates.append((None, "system"))
        for candidate, label in candidates:
            try:
                found = resolve_java(required_major=required_major, manual_path=candidate)
            except JavaError:
                continue
            skip = True
            reason = f"using {label} Java {found.version}"
            break
    else:
        skip = False
        reason = ""

    if not skip:
        yield
        return

    progress.set_status(f"Skipping Mojang JRE ({reason})")
    original = mc_install.install_jvm_runtime
    mc_install.install_jvm_runtime = lambda *args, **kwargs: None  # type: ignore[method-assign]
    try:
        yield
    finally:
        mc_install.install_jvm_runtime = original


def install_version(
    version_id: str,
    game_dir: Path | None = None,
    printer: ProgressPrinter | None = None,
    install_java: bool | None = None,
    java_path: Path | str | None = None,
    java_paths: list[str] | None = None,
) -> InstallResult:
    """Download/repair a version, then verify the important files.

    install_java: None = auto (skip the Mojang JRE when a compatible Java is
    available, checking java_path first), True = always install the JRE,
    False = never install it.
    """
    enable_mirror()
    reset_downloads()
    target = Path(game_dir).expanduser() if game_dir else GAME_DIR
    progress = printer or ProgressPrinter()

    # Fetching our metadata first gives a clear error before the long download
    # and provides the SHA1 values used for verification afterwards.
    version_data = fetch_version_json(version_id)

    progress.set_status(f"Installing Minecraft {version_id}")
    try:
        with _maybe_skip_jvm(version_data, install_java, progress, java_path, java_paths):
            mc_install.install_minecraft_version(version_id, str(target), progress.callback())
    except DownloadAborted as exc:
        raise InstallError("安装已取消") from exc
    except VersionNotFound as exc:
        raise InstallError(f"Version {version_id} was not found: {exc}") from exc
    except Exception as exc:
        if downloads_aborted():
            raise InstallError("安装已取消") from exc
        raise InstallError(f"Installation failed: {exc}") from exc
    finally:
        reset_downloads()
        progress.finish()

    # Verify against the installed version JSON, not the pre-fetched metadata:
    # a mirror may serve slightly different content, and the game directory is
    # the source of truth for what was actually downloaded.
    installed_json = target / "versions" / version_id / f"{version_id}.json"
    if not installed_json.is_file():
        raise InstallError(f"Version JSON was not created: {installed_json}")
    installed_data = json.loads(installed_json.read_text(encoding="utf-8"))

    client = installed_data.get("downloads", {}).get("client", {})
    client_jar = target / "versions" / version_id / f"{version_id}.jar"
    expected_sha1 = client.get("sha1")

    if not client_jar.is_file():
        raise InstallError(f"client.jar was not created: {client_jar}")

    actual_sha1 = sha1_file(client_jar)
    if expected_sha1 and actual_sha1 != expected_sha1:
        raise InstallError(
            f"client.jar SHA1 mismatch: expected {expected_sha1}, got {actual_sha1}"
        )

    asset_index_id, asset_count = _verify_asset_index(target, installed_data)
    java_executable = _find_java_runtime(target, installed_data)

    return InstallResult(
        version=version_id,
        game_dir=target,
        client_jar=client_jar,
        client_sha1=actual_sha1,
        asset_index_id=asset_index_id,
        asset_count=asset_count,
        java_executable=java_executable,
    )


def _verify_asset_index(game_dir: Path, version_data: dict) -> tuple[str | None, int]:
    asset_index = version_data.get("assetIndex", {})
    index_id = asset_index.get("id")
    if not index_id:
        return None, 0

    index_path = game_dir / "assets" / "indexes" / f"{index_id}.json"
    if not index_path.is_file():
        raise InstallError(f"Asset index was not created: {index_path}")

    expected_sha1 = asset_index.get("sha1")
    actual_sha1 = sha1_file(index_path)
    if expected_sha1 and actual_sha1 != expected_sha1:
        raise InstallError(
            f"Asset index SHA1 mismatch: expected {expected_sha1}, got {actual_sha1}"
        )

    data = json.loads(index_path.read_text(encoding="utf-8"))
    objects = data.get("objects", {})

    # The library silently skips downloads that return non-200, so verify that
    # every asset object really exists with the expected size.
    unique_objects: dict[str, int | None] = {}
    for entry in objects.values():
        digest = entry.get("hash")
        if digest:
            unique_objects[digest] = entry.get("size")

    missing: list[str] = []
    wrong_size: list[str] = []
    for digest, size in unique_objects.items():
        object_path = game_dir / "assets" / "objects" / digest[:2] / digest
        if not object_path.is_file():
            missing.append(digest)
        elif isinstance(size, int) and object_path.stat().st_size != size:
            wrong_size.append(digest)

    if missing:
        raise InstallError(f"{len(missing)} asset objects are missing (first: {missing[0]})")
    if wrong_size:
        raise InstallError(f"{len(wrong_size)} asset objects have a wrong size (first: {wrong_size[0]})")

    return index_id, len(objects)


def _find_java_runtime(game_dir: Path, version_data: dict) -> Path | None:
    component = version_data.get("javaVersion", {}).get("component")
    if not component:
        return None

    java_path = runtime.get_executable_path(component, str(game_dir))
    return Path(java_path) if java_path else None
