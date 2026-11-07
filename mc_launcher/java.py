"""Detect local Java installations and pick one that can run a Minecraft version."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from minecraft_launcher_lib import java_utils as mc_java

_VERSION_RE = re.compile(r'version "([^"]+)"')
_MAC_JAVA_HOME = "/usr/libexec/java_home"
_MAC_VM_DIRS = (
    Path("/Library/Java/JavaVirtualMachines"),
    Path("/System/Library/Java/JavaVirtualMachines"),
)

# Common vendor install locations on Windows; PATH/JAVA_HOME may not cover them.
_WINDOWS_JAVA_ROOTS = (
    Path(r"C:\Program Files\Java"),
    Path(r"C:\Program Files (x86)\Java"),
    Path(r"C:\Program Files\Eclipse Adoptium"),
    Path(r"C:\Program Files (x86)\Eclipse Adoptium"),
    Path(r"C:\Program Files\Microsoft"),
    Path(r"C:\Program Files\Zulu"),
    Path(r"C:\Program Files\Amazon Corretto"),
    Path(r"C:\Program Files\BellSoft"),
)


class JavaError(Exception):
    """Raised when Java cannot be found or inspected."""


@dataclass(frozen=True)
class JavaInstallation:
    executable: Path
    home: Path
    major: int
    version: str
    vendor: str
    is_64bit: bool

    def describe(self) -> str:
        bits = "64-bit" if self.is_64bit else "unknown-bits"
        return f"Java {self.version} ({self.vendor}, {bits}) at {self.executable}"


def _executable_names() -> tuple[str, ...]:
    return ("java.exe",) if os.name == "nt" else ("java",)


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _resolve_executable(path: str | os.PathLike) -> Path:
    """Accept either a java executable or a home directory."""
    candidate = Path(path).expanduser().absolute()

    if candidate.is_file():
        return candidate

    for name in _executable_names():
        for relative in ("bin", "Contents/Home/bin"):
            executable = candidate / relative / name
            if executable.is_file():
                return executable

    raise JavaError(f"No Java executable found at {candidate} (expected bin/java)")


def _major_from_version(raw: str) -> int:
    parts = re.split(r"[._]", raw)
    try:
        # Legacy versions are reported as 1.8.0_452, where the real major is 8.
        if parts[0] == "1" and len(parts) > 1:
            return int(parts[1])
        return int(parts[0])
    except (ValueError, IndexError) as exc:
        raise JavaError(f"Unsupported Java version string: {raw!r}") from exc


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def inspect_java(path: str | os.PathLike) -> JavaInstallation:
    """Run `<java> -version` and return the parsed installation details."""
    executable = _resolve_executable(path)
    try:
        process = subprocess.run(
            [str(executable), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise JavaError(f"Failed to run {executable}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise JavaError(f"{executable} did not respond within 10 seconds") from exc

    output = f"{process.stderr or ''}\n{process.stdout or ''}"
    match = _VERSION_RE.search(output)
    if match is None:
        first_line = output.strip().splitlines()[0] if output.strip() else "(no output)"
        raise JavaError(f"Could not parse Java version from {executable}: {first_line}")

    raw_version = match.group(1)
    version_line = next(line for line in output.splitlines() if _VERSION_RE.search(line))
    vendor = version_line.split(' version "')[0].strip() or "unknown"

    return JavaInstallation(
        executable=executable,
        home=executable.parent.parent,
        major=_major_from_version(raw_version),
        version=raw_version,
        vendor=vendor,
        is_64bit="64-bit" in output.lower(),
    )


def _macos_candidates() -> list[Path]:
    paths: list[Path] = []
    try:
        process = subprocess.run(
            [_MAC_JAVA_HOME, "-V"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in (process.stderr or "").splitlines():
            marker = line.rfind(" /")
            if marker != -1:
                paths.append(Path(line[marker + 1 :].strip()))
    except (OSError, subprocess.TimeoutExpired):
        pass

    for base in _MAC_VM_DIRS:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            home = entry / "Contents" / "Home"
            if (home / "bin" / "java").is_file():
                paths.append(home)
    return paths


def _windows_candidates() -> list[Path]:
    paths: list[Path] = []
    for base in _WINDOWS_JAVA_ROOTS:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if (entry / "bin" / "java.exe").is_file():
                paths.append(entry)
    return paths


def detect_java(extra_paths: list[str | os.PathLike] | None = None) -> list[JavaInstallation]:
    """Find Java installations from JAVA_HOME, PATH and platform directories."""
    candidates: list[Path] = []

    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home))

    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))

    system = platform.system()
    if system == "Windows":
        # The library scans Program Files\Java; add other popular vendor dirs.
        candidates.extend(Path(item) for item in mc_java.find_system_java_versions())
        candidates.extend(_windows_candidates())
    elif system == "Linux":
        candidates.extend(Path(item) for item in mc_java.find_system_java_versions())
    elif system == "Darwin":
        candidates.extend(_macos_candidates())

    if extra_paths:
        candidates.extend(Path(item).expanduser() for item in extra_paths)

    installations: list[JavaInstallation] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            info = inspect_java(candidate)
        except (JavaError, OSError):
            continue

        key = str(info.executable.resolve())
        if key in seen:
            continue
        seen.add(key)
        installations.append(info)

    installations.sort(key=lambda item: (item.major, _version_key(item.version)))
    return installations


def choose_java(
    installations: list[JavaInstallation], required_major: int
) -> JavaInstallation | None:
    """Prefer an exact major match, otherwise the closest newer version."""
    exact = [item for item in installations if item.major == required_major]
    if exact:
        return max(exact, key=lambda item: _version_key(item.version))

    newer = [item for item in installations if item.major > required_major]
    if newer:
        return min(newer, key=lambda item: (item.major, _version_key(item.version)))
    return None


def resolve_java(
    required_major: int | None = None,
    manual_path: str | os.PathLike | None = None,
    extra_paths: list[str | os.PathLike] | None = None,
) -> JavaInstallation:
    """Return a usable Java, honouring a manually selected path first."""
    if manual_path is not None:
        info = inspect_java(manual_path)
        if required_major is not None and info.major < required_major:
            raise JavaError(
                f"{info.executable} is Java {info.major}, but this version needs Java {required_major}+"
            )
        return info

    installations = detect_java(extra_paths=extra_paths)
    if not installations:
        raise JavaError("No Java installation found (checked JAVA_HOME, PATH and system directories)")

    if required_major is None:
        return installations[-1]

    selected = choose_java(installations, required_major)
    if selected is None:
        found = ", ".join(f"Java {item.major}" for item in installations)
        raise JavaError(f"No Java {required_major}+ found; detected: {found}")
    return selected
