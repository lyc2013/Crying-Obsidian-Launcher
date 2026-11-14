"""Thin wrapper around minecraft-launcher-lib's mod loader support."""

from __future__ import annotations

import time
from pathlib import Path

from minecraft_launcher_lib.mod_loader import get_mod_loader

LOADERS = (
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
    ("fabric", "Fabric"),
    ("quilt", "Quilt"),
)

LOADER_LABELS = {key: label for key, label in LOADERS}
LOADER_IDS = {label: key for key, label in LOADERS}


class LoaderError(Exception):
    pass


class LoaderInstallCancelled(LoaderError):
    pass


_cache: dict[tuple, tuple[float, object]] = {}
_TTL = 600


def _cached(key: tuple, producer):
    entry = _cache.get(key)
    if entry is not None and entry[0] > time.time():
        return entry[1]
    value = producer()
    _cache[key] = (time.time() + _TTL, value)
    return value


def supported_minecraft_versions(loader_id: str, stable_only: bool = False) -> list[str]:
    def produce():
        loader = get_mod_loader(loader_id)
        return list(loader.get_minecraft_versions(stable_only=stable_only))

    return list(_cached(("mc", loader_id, stable_only), produce))


def loader_versions(loader_id: str, mc_version: str) -> list[str]:
    def produce():
        loader = get_mod_loader(loader_id)
        return list(loader.get_loader_versions(mc_version, stable_only=False))

    return list(_cached(("versions", loader_id, mc_version), produce))


def latest_loader_version(loader_id: str, mc_version: str) -> str | None:
    def produce():
        loader = get_mod_loader(loader_id)
        try:
            return loader.get_latest_loader_version(mc_version)
        except Exception:
            return None

    return _cached(("latest", loader_id, mc_version), produce)


def installed_loader_version(loader_id: str, mc_version: str, game_dir) -> str | None:
    try:
        loader = get_mod_loader(loader_id)
        return loader.get_installed_version(mc_version, str(game_dir))
    except Exception:
        return None


def install_loader(
    loader_id: str,
    mc_version: str,
    game_dir,
    loader_version: str | None = None,
    callback: dict | None = None,
    cancel=None,
) -> str:
    """Install a mod loader, returning the generated version id.

    The library only offers progress callbacks; cancellation is cooperative by
    raising from those callbacks between individual steps.
    """

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise LoaderInstallCancelled("已取消加载器安装")

    def status(text: str) -> None:
        check()
        if callback and callback.get("setStatus"):
            callback["setStatus"](text)

    def progress(value: int) -> None:
        check()
        if callback and callback.get("setProgress"):
            callback["setProgress"](value)

    def maximum(value: int) -> None:
        check()
        if callback and callback.get("setMax"):
            callback["setMax"](value)

    wrapped = {"setStatus": status, "setProgress": progress, "setMax": maximum}
    loader = get_mod_loader(loader_id)
    try:
        return loader.install(
            mc_version,
            str(Path(game_dir)),
            loader_version=loader_version,
            callback=wrapped,
        )
    except LoaderInstallCancelled:
        raise
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            raise LoaderInstallCancelled("已取消加载器安装") from exc
        raise LoaderError(str(exc)) from exc
