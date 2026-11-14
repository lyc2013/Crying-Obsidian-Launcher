"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .auth import AuthError, offline_account
from .config import GAME_DIR, PROJECT_SLUG, VERSIONS_META_DIR
from .install import InstallError, install_version
from .java import JavaError, choose_java, detect_java, inspect_java
from .launch import LaunchError, build_launch_spec, format_command, run_game
from .mirror import enable_mirror, mirror_name
from .version import (
    VersionError,
    fetch_version_summary,
    latest_versions,
    list_versions,
    repair,
)


def _format_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _format_size(size: Any) -> str:
    if not isinstance(size, int):
        return "unknown"
    return f"{size / 1024 / 1024:.1f} MiB"


def _cmd_versions(args: argparse.Namespace) -> int:
    try:
        latest = latest_versions()
        versions = list_versions()
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.release_only:
        versions = [item for item in versions if item.get("type") == "release"]

    print(f"Latest release : {latest.get('release', '-')}")
    print(f"Latest snapshot: {latest.get('snapshot', '-')}")
    print(f"Game directory : {GAME_DIR}")
    print(f"Mirror         : {mirror_name()}")
    print()

    for item in versions[: args.limit]:
        print(f"{item['id']:<24} {item['type']:<10} {_format_date(item['releaseTime'])}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    try:
        summary = fetch_version_summary(args.version, refresh=args.refresh)
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"id            : {summary['id']}")
    print(f"type          : {summary['type']}")
    print(f"releaseTime   : {summary['releaseTime']}")
    print(f"mainClass     : {summary['mainClass']}")
    print(f"javaVersion   : {summary['javaVersion']} ({summary['javaComponent']})")
    print(f"assetIndex    : {summary['assetIndex']}")
    print(f"libraries     : {summary['libraryCount']}")
    print(f"client.jar    : {_format_size(summary['clientSize'])}  sha1={summary['clientSha1']}")
    print(f"argumentFormat: {summary['argumentFormat']}")
    print(f"metadata cache: {VERSIONS_META_DIR / (str(summary['id']) + '.json')}")
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    game_dir = args.game_dir.expanduser() if args.game_dir else None
    try:
        result = install_version(
            args.version,
            game_dir=game_dir,
            install_java=True if args.install_jre else None,
            java_path=args.java,
        )
    except (InstallError, VersionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print()
    print(f"install complete: {result.version}")
    print(f"game directory : {result.game_dir}")
    print(f"mirror         : {mirror_name()}")
    print(f"client.jar     : {result.client_jar} (SHA1 verified)")
    print(
        f"assets         : index {result.asset_index_id}, "
        f"{result.asset_count} objects (verified)"
    )
    if result.java_executable:
        print(f"Java runtime   : {result.java_executable}")
    else:
        print("Java runtime   : skipped (using system Java)")
    return 0


def _cmd_repair(args: argparse.Namespace) -> int:
    game_dir = args.game_dir.expanduser() if args.game_dir else None
    try:
        report = repair(game_dir)
    except VersionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"game directory : {report['game_dir']}")
    print(f"scanned        : {report['scanned']}")
    print(f"mismatched     : {report['fixed']}")
    print(f"meta updated   : {report['updated']}")
    if report["rows"]:
        print()
        print("版本ID | 当前错误分类 | 正确分类 | 原因")
        for row in report["rows"]:
            print(f"{row['id']} | {row['old']} | {row['new']} | {row['reason']}")
    print()
    print(f"metadata       : {report['meta_path']}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    try:
        account = offline_account(args.username)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("account type   : offline")
    print(f"username       : {account.username}")
    print(f"uuid           : {account.uuid}")
    print(f"access token   : {account.access_token!r} (empty, as expected offline)")
    return 0


def _cmd_java(args: argparse.Namespace) -> int:
    if args.path:
        try:
            info = inspect_java(args.path)
        except JavaError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(info.describe())
        return 0

    installations = detect_java()
    if not installations:
        print("error: no Java installation found", file=sys.stderr)
        return 1

    required: int | None = None
    if args.mc_version:
        try:
            summary = fetch_version_summary(args.mc_version)
        except VersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        required = summary["javaVersion"]
        if not isinstance(required, int):
            print(f"error: {args.mc_version} does not declare a Java version", file=sys.stderr)
            return 1

    selected = choose_java(installations, required) if required is not None else None
    print(f"Detected {len(installations)} Java installation(s):")
    for info in installations:
        marker = "*" if selected is not None and info == selected else " "
        print(f" {marker} {info.describe()}")

    if required is not None:
        print()
        print(f"Minecraft {args.mc_version} needs Java {required}+")
        if selected is None:
            print(f"error: no Java {required}+ found", file=sys.stderr)
            return 1
        print(f"selected: {selected.executable} (Java {selected.version})")
    return 0


def _cmd_launch(args: argparse.Namespace) -> int:
    try:
        spec = build_launch_spec(
            args.version,
            args.username,
            game_dir=args.game_dir,
            java_path=args.java,
            memory_mb=args.memory,
            extra_jvm_args=args.jvm_arg,
            server=args.server,
            port=args.port,
        )
    except (LaunchError, JavaError, AuthError, VersionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"version : {spec.version}")
    print(f"account : {spec.account.username} (offline, {spec.account.uuid})")
    print(f"java    : {spec.java.executable} (Java {spec.java.version})")
    print(f"game dir: {spec.game_dir}")

    if args.dry_run:
        print(f"command : {format_command(spec.command)}")
        return 0

    print()
    print(f"Launching Minecraft {spec.version}...")
    print()
    try:
        code = run_game(spec)
    except LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    print()
    print(f"Minecraft exited with code {code}")
    return 0 if code == 0 else 1


def _cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .ui import run
    except ImportError as exc:
        print(
            f"error: the GUI needs PySide6 ({exc}); run `pip install -r requirements.txt`",
            file=sys.stderr,
        )
        return 1
    return run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROJECT_SLUG, description="Crying Obsidian Launcher (Minecraft Java Edition, MVP)"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--mirror",
        choices=("mojang", "bmclapi"),
        help="Download source (default: bmclapi, or $MC_MIRROR)",
    )

    mirror_parent = argparse.ArgumentParser(add_help=False)
    mirror_parent.add_argument(
        "--mirror",
        choices=("mojang", "bmclapi"),
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    versions_parser = subparsers.add_parser(
        "versions", parents=[mirror_parent], help="List Minecraft versions"
    )
    versions_parser.add_argument("--limit", type=int, default=10, help="Number of versions to show (default: 10)")
    versions_parser.add_argument("--release-only", action="store_true", help="Only show release versions")
    versions_parser.set_defaults(func=_cmd_versions)

    info_parser = subparsers.add_parser(
        "info", parents=[mirror_parent], help="Show metadata of a version"
    )
    info_parser.add_argument("version", help="Version id, for example 1.20.1")
    info_parser.add_argument("--refresh", action="store_true", help="Ignore cached metadata and fetch again")
    info_parser.set_defaults(func=_cmd_info)

    install_parser = subparsers.add_parser(
        "install", parents=[mirror_parent], help="Download and install a Minecraft version"
    )
    install_parser.add_argument("version", help="Version id, for example 1.20.1")
    install_parser.add_argument("--game-dir", type=Path, help="Override the game directory")
    install_parser.add_argument(
        "--install-jre",
        action="store_true",
        help="Always install the Mojang JRE, even if a compatible Java exists",
    )
    install_parser.add_argument(
        "--java",
        type=Path,
        help="Java home or executable; a compatible one skips the JRE download",
    )
    install_parser.set_defaults(func=_cmd_install)

    repair_parser = subparsers.add_parser(
        "repair",
        parents=[mirror_parent],
        help="Rescan local versions, fix category metadata and print a diff",
    )
    repair_parser.add_argument("--game-dir", type=Path, help="Override the game directory")
    repair_parser.set_defaults(func=_cmd_repair)

    login_parser = subparsers.add_parser(
        "login", parents=[mirror_parent], help="Create an offline account (username only)"
    )
    login_parser.add_argument("username", help="Username, 3-16 letters/digits/underscore")
    login_parser.set_defaults(func=_cmd_login)

    java_parser = subparsers.add_parser(
        "java", parents=[mirror_parent], help="List detected Java installations"
    )
    java_parser.add_argument(
        "--path", type=Path, help="Inspect one Java home directory or executable"
    )
    java_parser.add_argument(
        "--mc-version", help="Show which Java would launch this Minecraft version"
    )
    java_parser.set_defaults(func=_cmd_java)

    launch_parser = subparsers.add_parser(
        "launch", parents=[mirror_parent], help="Launch Minecraft with an offline account"
    )
    launch_parser.add_argument("version", help="Installed version id, for example 1.20.1")
    launch_parser.add_argument("--username", required=True, help="Offline username")
    launch_parser.add_argument("--game-dir", type=Path, help="Override the game directory")
    launch_parser.add_argument("--java", type=Path, help="Java home directory or executable")
    launch_parser.add_argument(
        "--memory", type=int, default=2048, help="Heap size in MB; 0 to omit (default: 2048)"
    )
    launch_parser.add_argument(
        "--jvm-arg", action="append", default=[], help="Extra JVM argument, can be repeated"
    )
    launch_parser.add_argument("--server", help="Join this server after starting")
    launch_parser.add_argument("--port", help="Server port (used with --server)")
    launch_parser.add_argument(
        "--dry-run", action="store_true", help="Print the command instead of running it"
    )
    launch_parser.set_defaults(func=_cmd_launch)

    gui_parser = subparsers.add_parser("gui", help="Open the desktop interface")
    gui_parser.set_defaults(func=_cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mirror:
        os.environ["MC_MIRROR"] = args.mirror

    if mirror_name() not in ("mojang", "bmclapi"):
        print(
            f"error: unsupported mirror {mirror_name()!r}; use mojang or bmclapi",
            file=sys.stderr,
        )
        return 1

    enable_mirror()
    return args.func(args)
