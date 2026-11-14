"""Modpack import for Modrinth ``.mrpack`` and CurseForge ``manifest.json``."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from minecraft_launcher_lib import install as mc_install
from minecraft_launcher_lib import mrpack

from .download import DownloadError, download_file
from .http import ApiClient, CancelToken, RequestCancelled
from .providers import CurseForgeProvider, ResourceProvider

_LOADER_PREFIXES = {
    "neoforge": ("neoforge", "NeoForge"),
    "forge": ("forge", "Forge"),
    "fabric-loader": ("fabric", "Fabric"),
    "fabric": ("fabric", "Fabric"),
    "quilt-loader": ("quilt", "Quilt"),
    "quilt": ("quilt", "Quilt"),
}


class ModpackError(Exception):
    pass


def _read_json_from_zip(path: Path, member: str) -> dict | None:
    try:
        with zipfile.ZipFile(path) as archive:
            with archive.open(member) as handle:
                return json.load(handle)
    except (KeyError, OSError, ValueError, zipfile.BadZipFile):
        return None


def _split_loader(loader_key: str, value: str) -> tuple[str, str | None, str]:
    key = loader_key.lower()
    loader_id, label = _LOADER_PREFIXES.get(key, ("", ""))
    version = value
    if loader_id == "neoforge" and value.startswith("neoforge-"):
        version = value.split("-", 1)[1]
    elif loader_id == "forge" and value.startswith("forge-"):
        version = value.split("-", 1)[1]
    elif value.startswith(f"{key}-"):
        version = value.split("-", 1)[1]
    return loader_id, version, label


def inspect_modpack(path) -> dict:
    """Read name / MC version / loader info without installing anything."""
    archive = Path(path)
    if not archive.is_file():
        raise ModpackError("整合包文件不存在")

    index = _read_json_from_zip(archive, "modrinth.index.json")
    if isinstance(index, dict):
        dependencies = index.get("dependencies") or {}
        loader_id, loader_version, label = "", None, ""
        for key, value in dependencies.items():
            if key.lower() == "minecraft":
                continue
            loader_id, loader_version, label = _split_loader(str(key), str(value))
            if loader_id:
                break
        files = index.get("files") or []
        return {
            "kind": "mrpack",
            "name": str(index.get("name") or archive.stem),
            "version": str(index.get("versionId") or ""),
            "summary": str(index.get("summary") or ""),
            "mc_version": str(dependencies.get("minecraft") or ""),
            "loader_id": loader_id,
            "loader_label": label,
            "loader_version": loader_version,
            "file_count": len([entry for entry in files if isinstance(entry, dict)]),
        }

    manifest = _read_json_from_zip(archive, "manifest.json")
    if isinstance(manifest, dict):
        minecraft = manifest.get("minecraft") or {}
        loaders = minecraft.get("modLoaders") or []
        loader_id, loader_version, label = "", None, ""
        for entry in loaders:
            if not isinstance(entry, dict):
                continue
            raw_id = str(entry.get("id") or "")
            if not raw_id:
                continue
            prefix = raw_id.split("-", 1)[0].lower()
            if prefix in _LOADER_PREFIXES:
                loader_id = _LOADER_PREFIXES[prefix][0]
                loader_version = raw_id.split("-", 1)[1] if "-" in raw_id else None
                label = _LOADER_PREFIXES[prefix][1]
                if entry.get("primary"):
                    break
        files = manifest.get("files") or []
        return {
            "kind": "curseforge",
            "name": str(manifest.get("name") or archive.stem),
            "version": str(manifest.get("version") or ""),
            "summary": str(manifest.get("author") or ""),
            "mc_version": str(minecraft.get("version") or ""),
            "loader_id": loader_id,
            "loader_label": label,
            "loader_version": loader_version,
            "file_count": len([entry for entry in files if isinstance(entry, dict)]),
        }

    raise ModpackError("无法识别的整合包格式（缺少 modrinth.index.json / manifest.json）")


def _guard_member(name: str) -> bool:
    parts = Path(name).parts
    return not name.startswith(("/", "\\")) and ".." not in parts


def extract_overrides(archive_path: Path, destination: Path) -> int:
    """Extract ``overrides/`` (CurseForge) or ``overrides/`` from an archive."""
    extracted = 0
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                name = info.filename
                if not name.startswith("overrides/") or info.is_dir():
                    continue
                relative = name[len("overrides/"):]
                if not relative or not _guard_member(relative):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink)
                extracted += 1
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModpackError(f"解压 overrides 失败：{exc}") from exc
    return extracted


def install_mrpack(
    archive_path: Path,
    minecraft_dir: Path,
    modpack_dir: Path,
    callback: dict | None = None,
    cancel=None,
) -> dict:
    info = inspect_modpack(archive_path)

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise RequestCancelled("已取消整合包安装")

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

    modpack_dir.mkdir(parents=True, exist_ok=True)
    try:
        mrpack.install_mrpack(
            str(archive_path),
            str(minecraft_dir),
            str(modpack_dir),
            callback={"setStatus": status, "setProgress": progress, "setMax": maximum},
        )
    except RequestCancelled:
        raise
    except Exception as exc:
        if cancel is not None and cancel.is_set():
            raise RequestCancelled("已取消整合包安装") from exc
        raise ModpackError(f"整合包安装失败：{exc}") from exc

    try:
        launch_version = mrpack.get_mrpack_launch_version(str(archive_path))
    except Exception:
        launch_version = ""
    info["launch_version"] = launch_version
    return info


def install_curseforge_pack(
    archive_path: Path,
    minecraft_dir: Path,
    modpack_dir: Path,
    provider: ResourceProvider,
    client: ApiClient,
    callback: dict | None = None,
    cancel=None,
) -> dict:
    """Install a CurseForge modpack: vanilla + loader + manifest files."""
    info = inspect_modpack(archive_path)
    if info["kind"] != "curseforge":
        raise ModpackError("该整合包不是 CurseForge 格式")

    manifest = _read_json_from_zip(Path(archive_path), "manifest.json") or {}
    minecraft = manifest.get("minecraft") or {}
    mc_version = str(minecraft.get("version") or "")
    if not mc_version:
        raise ModpackError("manifest.json 缺少 Minecraft 版本")

    token = CancelToken()
    if cancel is not None:
        # bridge the caller's simple Event-like object into our token
        import threading

        def watch() -> None:
            while not token.cancelled:
                if cancel.is_set():
                    token.cancel()
                    return
                threading.Event().wait(0.2)

        threading.Thread(target=watch, daemon=True).start()

    def status(text: str) -> None:
        if callback and callback.get("setStatus"):
            callback["setStatus"](text)

    status("安装原版 " + mc_version)
    mc_install.install_minecraft_version(
        mc_version,
        str(minecraft_dir),
        callback={
            "setStatus": status,
            "setProgress": (callback or {}).get("setProgress"),
            "setMax": (callback or {}).get("setMax"),
        },
    )

    if info["loader_id"]:
        from ..loaders import install_loader

        status("安装加载器 " + info["loader_label"])
        install_loader(
            info["loader_id"],
            mc_version,
            minecraft_dir,
            loader_version=info["loader_version"],
            callback=callback,
            cancel=cancel,
        )

    status("解压整合包覆盖文件")
    modpack_dir.mkdir(parents=True, exist_ok=True)
    extract_overrides(Path(archive_path), modpack_dir)

    files = [entry for entry in manifest.get("files") or [] if isinstance(entry, dict)]
    mods_dir = modpack_dir / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    if callback and callback.get("setMax"):
        callback["setMax"](max(1, len(files)))
    for index, entry in enumerate(files):
        if cancel is not None and cancel.is_set():
            raise RequestCancelled("已取消整合包安装")
        project_id = str(entry.get("projectID") or "")
        file_id = str(entry.get("fileID") or "")
        if not project_id or not file_id:
            continue
        if not isinstance(provider, CurseForgeProvider):
            raise ModpackError("CurseForge 整合包需要 CurseForge 数据源")
        status(f"下载模组 {index + 1}/{len(files)}")
        try:
            file = provider.get_file(project_id, file_id, token=token)
            if file is None:
                continue
            download_file(client, provider, file, mods_dir, token=token)
        except (DownloadError, Exception) as exc:
            if isinstance(exc, RequestCancelled):
                raise
            status(f"跳过 {project_id}/{file_id}：{exc}")
        if callback and callback.get("setProgress"):
            callback["setProgress"](index + 1)

    info["launch_version"] = ""
    if info["loader_id"]:
        from ..loaders import installed_loader_version

        info["launch_version"] = (
            installed_loader_version(info["loader_id"], mc_version, minecraft_dir) or ""
        )
    return info


def install_archive(
    archive_path: Path,
    minecraft_dir: Path,
    modpack_dir: Path,
    provider: ResourceProvider,
    client: ApiClient,
    callback: dict | None = None,
    cancel=None,
) -> dict:
    info = inspect_modpack(archive_path)
    if info["kind"] == "mrpack":
        return install_mrpack(archive_path, minecraft_dir, modpack_dir, callback, cancel)
    return install_curseforge_pack(
        archive_path, minecraft_dir, modpack_dir, provider, client, callback, cancel
    )


def download_archive(
    client: ApiClient,
    provider: ResourceProvider,
    file,
    token: CancelToken | None = None,
) -> Path:
    """Download a modpack archive into a temporary directory."""
    from .download import safe_filename

    folder = Path(tempfile.mkdtemp(prefix="modpack-"))
    target = folder / safe_filename(file.filename or "modpack.zip")
    urls = []
    try:
        resolved = provider.resolve_download_url(file, token=token)
        if resolved:
            urls.append(resolved)
    except Exception:
        pass
    if file.url:
        urls.append(file.url)
    if not urls:
        raise ModpackError("整合包没有可用的下载地址")
    last_error: Exception | None = None
    for url in urls:
        try:
            from .download import download_raw

            download_raw(client, url, target, token=token)
            return target
        except Exception as exc:
            last_error = exc
            continue
    raise ModpackError(f"整合包下载失败：{last_error}")
