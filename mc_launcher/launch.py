"""Build the Java command and run Minecraft, streaming its logs."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from minecraft_launcher_lib import command as mc_command
from minecraft_launcher_lib import runtime
from minecraft_launcher_lib.exceptions import VersionNotFound

from . import __version__
from .auth import OfflineAccount, offline_account
from .config import GAME_DIR
from .java import JavaError, JavaInstallation, inspect_java, resolve_java
from .version import load_version_json


class LaunchError(Exception):
    """Raised when the game cannot be started."""


@dataclass(frozen=True)
class LaunchSpec:
    version: str
    game_dir: Path
    java: JavaInstallation
    account: OfflineAccount
    command: list[str]


def bundled_java(game_dir: Path, version_data: dict) -> JavaInstallation | None:
    """Return the Mojang runtime installed by `install`, if usable."""
    component = version_data.get("javaVersion", {}).get("component")
    if not component:
        return None

    try:
        path = runtime.get_executable_path(component, str(game_dir))
        if path and Path(path).is_file():
            return inspect_java(path)
    except (JavaError, OSError):
        return None
    return None


def _jvm_arguments(
    memory_mb: int, min_memory_mb: int = 0, extra: list[str] | None = None
) -> list[str]:
    arguments: list[str] = []
    if min_memory_mb > 0:
        arguments.append(f"-Xms{min_memory_mb}M")
    if memory_mb > 0:
        arguments.append(f"-Xmx{memory_mb}M")
    if extra:
        arguments.extend(extra)
    return arguments


def _logging_options(game_dir: Path, version_data: dict) -> dict[str, object]:
    logging_data = version_data.get("logging") or {}
    client = logging_data.get("client") or {}
    file_id = (client.get("file") or {}).get("id")
    if not file_id:
        return {}
    if not (game_dir / "assets" / "log_configs" / file_id).is_file():
        return {}
    return {"enableLoggingConfig": True}


def build_launch_spec(
    version: str,
    username: str,
    game_dir: Path | str | None = None,
    java_path: Path | str | None = None,
    memory_mb: int = 2048,
    min_memory_mb: int = 0,
    extra_jvm_args: list[str] | None = None,
    server: str | None = None,
    port: str | None = None,
    game_root: Path | str | None = None,
    java_candidates: list[str] | None = None,
) -> LaunchSpec:
    """Validate everything and return the full command without running it.

    game_dir is where versions/libraries/assets live. game_root is the actual
    --gameDir passed to Minecraft (used for instance isolation); it defaults to
    game_dir. java_candidates are extra Java paths added by the user, used when
    no explicit Java is selected and no bundled runtime is available.
    """
    target = Path(game_dir).expanduser() if game_dir else GAME_DIR
    root = Path(game_root).expanduser() if game_root else target
    account = offline_account(username)

    try:
        # Modded versions (Forge/Fabric/modpacks) only exist on disk, so their
        # JSON has to be read locally instead of from Mojang's manifest.
        version_data = load_version_json(version, target)
    except Exception as exc:
        raise LaunchError(f"Could not read metadata for {version}: {exc}") from exc

    required_major = version_data.get("javaVersion", {}).get("majorVersion")

    # Order: explicit path, then the runtime we installed, then the system Java.
    if java_path:
        java = resolve_java(required_major=required_major, manual_path=java_path)
    else:
        java = None
        bundled = bundled_java(target, version_data)
        if bundled is not None and (
            not isinstance(required_major, int) or bundled.major >= required_major
        ):
            java = bundled
        if java is None:
            java = resolve_java(required_major=required_major, extra_paths=java_candidates)

    options: dict[str, object] = {
        **account.to_launch_options(),
        "executablePath": str(java.executable),
        "gameDirectory": str(root),
        "launcherName": "Crying Obsidian",
        "launcherVersion": __version__,
        "jvmArguments": _jvm_arguments(memory_mb, min_memory_mb, extra_jvm_args),
    }
    options.update(_logging_options(target, version_data))
    if server:
        options["server"] = server
        if port:
            options["port"] = port

    try:
        command = mc_command.get_minecraft_command(version, str(target), options)
    except VersionNotFound as exc:
        raise LaunchError(
            f"{version} is not installed in {target}; run `install {version}` first"
        ) from exc

    return LaunchSpec(
        version=version,
        game_dir=root,
        java=java,
        account=account,
        command=command,
    )


def format_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def spawn_game(spec: LaunchSpec) -> subprocess.Popen[str]:
    """Start Minecraft with stdout and stderr merged into one stream."""
    try:
        return subprocess.Popen(
            spec.command,
            cwd=str(spec.game_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise LaunchError(f"Failed to start {spec.java.executable}: {exc}") from exc


def iter_output(process: subprocess.Popen[str]) -> Iterator[str]:
    if process.stdout is None:
        return
    for line in process.stdout:
        yield line.rstrip("\r\n")


def run_game(
    spec: LaunchSpec,
    on_line: Callable[[str], None] | None = None,
    on_process: Callable[[subprocess.Popen[str]], None] | None = None,
) -> int:
    """Run to completion, passing every log line to the callback."""
    emit = on_line or (lambda line: print(line, flush=True))
    process = spawn_game(spec)
    if on_process is not None:
        on_process(process)
    assert process.stdout is not None
    try:
        for line in process.stdout:
            emit(line.rstrip("\r\n"))
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        process.stdout.close()

