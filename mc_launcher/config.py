"""Filesystem paths and shared constants."""

from __future__ import annotations

import os
from pathlib import Path

from minecraft_launcher_lib.utils import get_minecraft_directory

from . import __version__

APP_NAME = "Crying Obsidian Launcher"
PROJECT_SLUG = "crying-obsidian"

# Our own data (metadata cache, accounts, settings) stays separate from the game
# dir. The folder keeps its original name so existing installs keep working.
APP_DIR = Path(os.environ.get("MC_LAUNCHER_HOME", Path.home() / ".mc-launcher")).expanduser()
META_DIR = APP_DIR / "meta"
VERSIONS_META_DIR = META_DIR / "versions"

# Reuse the official launcher's directory by default so existing assets/libraries
# are not downloaded twice. Override with MC_GAME_DIR for an isolated install.
GAME_DIR = Path(os.environ.get("MC_GAME_DIR", get_minecraft_directory())).expanduser()

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_TTL_SECONDS = 60 * 60
HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = f"{APP_NAME}/{__version__}"


def ensure_app_dirs() -> None:
    VERSIONS_META_DIR.mkdir(parents=True, exist_ok=True)
