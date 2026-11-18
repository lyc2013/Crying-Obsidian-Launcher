"""Tests for PCL / MultiMC directory layout resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mc_launcher.layout import (
    MARKER_NAME,
    Layout,
    detect_style,
    marker_path,
    read_marker,
    resolve_layout,
    set_style,
)


def _client(root: Path) -> Path:
    return root / ".minecraft"


def _make_pcl(root: Path, version: str = "1.20.1") -> None:
    client = _client(root)
    version_dir = client / "versions" / version
    version_dir.mkdir(parents=True)
    (version_dir / f"{version}.jar").write_bytes(b"jar")
    (client / "libraries").mkdir()
    (client / "assets").mkdir()


def test_pcl_paths(tmp_path):
    layout = Layout(tmp_path, "pcl")
    assert layout.client_dir == tmp_path / ".minecraft"
    assert layout.versions_dir == tmp_path / ".minecraft" / "versions"
    assert layout.libraries_dir == tmp_path / ".minecraft" / "libraries"
    assert layout.assets_dir == tmp_path / ".minecraft" / "assets"
    assert layout.mods_dir == tmp_path / ".minecraft" / "mods"
    assert layout.version_jar("1.20.1") == (
        tmp_path / ".minecraft" / "versions" / "1.20.1" / "1.20.1.jar"
    )


def test_multimc_paths(tmp_path):
    layout = Layout(tmp_path, "multimc")
    assert layout.versions_dir == tmp_path / ".minecraft" / "versions"
    assert layout.libraries_dir == tmp_path / "libraries"
    assert layout.assets_dir == tmp_path / "assets"
    assert layout.runtime_dir == tmp_path / "runtime"
    # Mods stay inside the client folder, as specified for MultiMC.
    assert layout.mods_dir == tmp_path / ".minecraft" / "mods"


def test_client_folder_may_be_named_minecraft(tmp_path):
    (tmp_path / "minecraft").mkdir()
    layout = Layout(tmp_path, "pcl")
    assert layout.client_dir == tmp_path / "minecraft"


def test_detect_multimc_by_instance_cfg(tmp_path):
    (tmp_path / "instance.cfg").write_text("name=test\n", encoding="utf-8")
    assert detect_style(tmp_path) == "multimc"


def test_detect_pcl_by_jar_and_shared_folders(tmp_path):
    _make_pcl(tmp_path)
    assert detect_style(tmp_path) == "pcl"


def test_detect_multimc_by_top_level_libraries(tmp_path):
    client = _client(tmp_path)
    client.mkdir()
    (client / "assets").mkdir()
    (tmp_path / "libraries").mkdir()
    assert detect_style(tmp_path) == "multimc"


def test_unrecognized_directory_uses_default_without_marker(tmp_path):
    layout, info = resolve_layout(tmp_path, "multimc")
    assert layout.style == "multimc"
    assert info.recognized is False
    assert info.source == "default"
    assert not marker_path(tmp_path).exists()


def test_detection_writes_marker_once(tmp_path):
    _make_pcl(tmp_path)
    layout, info = resolve_layout(tmp_path, "multimc")
    assert layout.style == "pcl"
    assert info.source == "auto"
    marker = read_marker(tmp_path)
    assert marker == {"profile": "pcl", "version": 1, "locked": False}


def test_resolution_can_preview_without_writing_marker(tmp_path):
    _make_pcl(tmp_path)
    layout, info = resolve_layout(tmp_path, "pcl", persist=False)
    assert layout.style == "pcl"
    assert info.source == "auto"
    assert not marker_path(tmp_path).exists()

    # A later resolution writes the marker and still reports automatic detection.
    layout, info = resolve_layout(tmp_path, "pcl")
    assert info.source == "auto"
    assert marker_path(tmp_path).exists()


def test_marker_wins_over_detection(tmp_path):
    _make_pcl(tmp_path)
    set_style(tmp_path, "multimc")
    layout, info = resolve_layout(tmp_path, "pcl")
    assert layout.style == "multimc"
    assert info.source == "manual"
    assert read_marker(tmp_path)["locked"] is True

    # Even after new PCL evidence appears, the locked marker wins.
    (tmp_path / "libraries").mkdir(exist_ok=True)
    layout, info = resolve_layout(tmp_path, "pcl")
    assert layout.style == "multimc"


def test_global_default_only_applies_to_unmarked_directories(tmp_path):
    marked = tmp_path / "marked"
    unmarked = tmp_path / "unmarked"
    marked.mkdir()
    unmarked.mkdir()
    set_style(marked, "pcl")

    marked_layout, _ = resolve_layout(marked, "multimc")
    unmarked_layout, info = resolve_layout(unmarked, "multimc")
    assert marked_layout.style == "pcl"
    assert unmarked_layout.style == "multimc"
    assert info.source == "default"


def test_legacy_client_root_is_supported(tmp_path):
    client = tmp_path / ".minecraft"
    client.mkdir()
    (client / "versions").mkdir()
    layout, info = resolve_layout(client, "multimc")
    assert layout.direct is True
    assert layout.style == "pcl"
    assert layout.client_dir == client
    assert layout.versions_dir == client / "versions"
    assert info.source == "legacy"


def test_switching_style_never_moves_files(tmp_path):
    _make_pcl(tmp_path)
    jar = _client(tmp_path) / "versions" / "1.20.1" / "1.20.1.jar"

    layout, _ = resolve_layout(tmp_path, "pcl")
    assert layout.version_jar("1.20.1") == jar

    set_style(tmp_path, "multimc")
    layout, _ = resolve_layout(tmp_path, "pcl")
    assert layout.style == "multimc"
    assert layout.version_jar("1.20.1") == jar
    assert jar.read_bytes() == b"jar"


def test_game_root_override_keeps_runtime_paths(tmp_path):
    layout = Layout(tmp_path, "multimc")
    isolated = layout.with_game_root(tmp_path / ".minecraft" / "instances" / "one")
    assert isolated.mods_dir == (
        tmp_path / ".minecraft" / "instances" / "one" / "mods"
    )
    assert isolated.versions_dir == layout.versions_dir
    assert isolated.libraries_dir == tmp_path / "libraries"


def test_marker_json_format(tmp_path):
    set_style(tmp_path, "multimc")
    payload = json.loads((tmp_path / MARKER_NAME).read_text(encoding="utf-8"))
    assert payload["profile"] == "multimc"
    assert payload["version"] == 1
    assert payload["locked"] is True


def test_pcl_install_view_is_client_dir(tmp_path):
    layout = Layout(tmp_path, "pcl")
    assert layout.install_view() == layout.client_dir


@pytest.mark.skipif(
    not hasattr(Path, "symlink_to"), reason="filesystem does not support links"
)
def test_multimc_install_view_links_shared_folders(tmp_path, monkeypatch):
    import mc_launcher.layout as layout_module

    monkeypatch.setattr(layout_module, "APP_DIR", tmp_path / "app")
    layout = Layout(tmp_path / "game", "multimc")
    view = layout.install_view()
    assert view.parent == tmp_path / "app" / "layout_views"
    assert (view / "versions").resolve() == layout.versions_dir.resolve()
    assert (view / "libraries").resolve() == layout.libraries_dir.resolve()
    assert (view / "assets").resolve() == layout.assets_dir.resolve()
