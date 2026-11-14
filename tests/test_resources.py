"""Unit tests for the platform-neutral resource layer."""

from __future__ import annotations

import unittest

from mc_launcher.resources import mapper
from mc_launcher.resources.download import _candidate_urls
from mc_launcher.resources.models import ResourceFile
from mc_launcher.resources.providers import build_modrinth_facets


class VersionSortingTests(unittest.TestCase):
    def test_segment_wise_order(self) -> None:
        values = ["1.19.2", "1.21.4", "1.20.1", "1.9.4", "1.10"]
        self.assertEqual(
            mapper.sort_versions_desc(values),
            ["1.21.4", "1.20.1", "1.19.2", "1.10", "1.9.4"],
        )

    def test_release_ranks_above_prerelease(self) -> None:
        self.assertGreater(mapper.version_key("26.3"), mapper.version_key("26.3-rc-2"))
        self.assertGreater(
            mapper.version_key("1.20.2"), mapper.version_key("1.20.2-pre3")
        )

    def test_group_counts_and_order(self) -> None:
        counts = mapper.group_game_versions(["1.20.1", "1.21.4", "1.20.1", ""])
        self.assertEqual(list(counts), ["1.21.4", "1.20.1"])
        self.assertEqual(counts["1.20.1"], 2)


class ModrinthMappingTests(unittest.TestCase):
    def test_search_hit_is_normalised(self) -> None:
        hit = {
            "project_id": "AANobbMI",
            "slug": "sodium",
            "title": "Sodium",
            "author": "jellysquid3",
            "description": "Modern rendering engine",
            "icon_url": "https://cdn.modrinth.com/icon.png",
            "categories": ["fabric", "neoforge", "optimization"],
            "versions": ["1.21.4", "1.20.1"],
            "downloads": 1234567,
            "follows": 4321,
            "date_modified": "2026-11-01T00:00:00Z",
        }
        item = mapper.modrinth_item(hit, "mod")
        self.assertEqual(item.downloads, 1234567)
        self.assertEqual(item.follows, 4321)
        self.assertEqual(item.loaders, ["NeoForge", "Fabric"])
        self.assertEqual(item.game_versions, ["1.21.4", "1.20.1"])
        self.assertEqual(item.source, "modrinth")

    def test_non_mod_is_marked_as_loader_free(self) -> None:
        self.assertEqual(mapper.display_loaders("resourcepack", []), ["无需加载器"])
        self.assertEqual(mapper.display_loaders("mod", []), ["未标注"])
        self.assertEqual(mapper.display_loaders("mod", ["Fabric"]), ["Fabric"])

    def test_version_file_prefers_primary(self) -> None:
        version = mapper.modrinth_version(
            {
                "id": "v1",
                "version_number": "0.5.8",
                "version_type": "release",
                "game_versions": ["1.20.1"],
                "loaders": ["fabric"],
                "files": [
                    {"filename": "a.jar", "primary": False, "url": "u1"},
                    {"filename": "b.jar", "primary": True, "url": "u2"},
                ],
            }
        )
        self.assertEqual(version.primary_file().filename, "b.jar")
        self.assertEqual(version.primary_file().url, "u2")


class CurseForgeMappingTests(unittest.TestCase):
    def test_item_mapping_and_loader_dedupe(self) -> None:
        raw = {
            "id": 238222,
            "name": "Just Enough Items",
            "slug": "jei",
            "summary": "View recipes",
            "downloadCount": 647_400_000,
            "thumbsUpCount": 5000,
            "authors": [{"name": "mezz"}],
            "categories": [{"name": "Map and Information"}],
            "latestFilesIndexes": [
                {"gameVersion": "1.20.1", "modLoader": 1},
                {"gameVersion": "1.20.1", "modLoader": 6},
                {"gameVersion": "1.20.1", "modLoader": 1},
                {"gameVersion": "1.20.1", "modLoader": 0},
            ],
            "screenshots": [{"thumbnailUrl": "https://media.forgecdn.net/a.png"}],
            "links": {"websiteUrl": "https://example.test"},
        }
        item = mapper.cf_item(raw, "mod")
        self.assertEqual(item.downloads, 647_400_000)
        self.assertEqual(item.follows, 5000)
        self.assertEqual(item.author, "mezz")
        self.assertEqual(item.loaders, ["Forge", "NeoForge"])
        self.assertEqual(item.game_versions, ["1.20.1"])

    def test_file_hash_and_game_version_split(self) -> None:
        raw = {
            "id": 424242,
            "fileName": "jei-1.20.1-forge.jar",
            "fileLength": 2048,
            "releaseType": 2,
            "fileDate": "2026-01-02T03:04:05Z",
            "gameVersions": ["1.20.1", "1.20", "Forge", "Java 17"],
            "hashes": [
                {"algo": 2, "value": "md5"},
                {"algo": 1, "value": "abcdef123456"},
            ],
        }
        parsed = mapper.cf_file("238222", raw)
        self.assertEqual(parsed.sha1, "abcdef123456")
        self.assertEqual(parsed.release_type, "beta")
        self.assertEqual(parsed.game_versions, ["1.20.1", "1.20"])
        self.assertEqual(parsed.loaders, ["Forge"])
        self.assertIn("2026", parsed.published)


class FacetAndPaginationTests(unittest.TestCase):
    def test_facets_shape(self) -> None:
        self.assertEqual(build_modrinth_facets("mod"), [["project_type:mod"]])
        self.assertEqual(
            build_modrinth_facets("resourcepack"), [["project_type:resourcepack"]]
        )

    def test_page_offset(self) -> None:
        self.assertEqual(mapper.page_offset(0, 20), 0)
        self.assertEqual(mapper.page_offset(3, 20), 60)
        self.assertEqual(mapper.page_offset(-2, 20), 0)
        self.assertEqual(mapper.page_offset(2, 0), 0)

    def test_total_pages(self) -> None:
        self.assertEqual(mapper.total_pages(0, 20), 1)
        self.assertEqual(mapper.total_pages(21, 20), 2)
        self.assertEqual(mapper.total_pages(110, 20), 6)

    def test_cf_limit(self) -> None:
        self.assertEqual(mapper.cf_page_limit(25000, 20), 500)
        self.assertEqual(mapper.cf_page_limit(5, 20), 1)


class FakeProvider:
    def __init__(self, resolved: str | None = None, raise_error: bool = False) -> None:
        self.resolved = resolved
        self.raise_error = raise_error

    def resolve_download_url(self, file, token=None):  # noqa: ANN001
        if self.raise_error:
            from mc_launcher.resources.http import ApiError

            raise ApiError(404)
        return self.resolved


class DownloadFallbackTests(unittest.TestCase):
    def test_mirror_then_official(self) -> None:
        file = ResourceFile(
            filename="a.jar",
            url="https://edge.forgecdn.net/files/a.jar",
            fallback_urls=["https://mediafilez.forgecdn.net/files/a.jar"],
        )
        urls = _candidate_urls(FakeProvider(resolved="https://mirror/a.jar"), file, None)
        self.assertEqual(
            urls,
            [
                "https://mirror/a.jar",
                "https://mediafilez.forgecdn.net/files/a.jar",
                "https://edge.forgecdn.net/files/a.jar",
            ],
        )

    def test_resolve_failure_still_uses_candidates(self) -> None:
        file = ResourceFile(filename="a.jar", url="https://cdn/a.jar")
        urls = _candidate_urls(FakeProvider(raise_error=True), file, None)
        self.assertEqual(urls, ["https://cdn/a.jar"])


class FormattingTests(unittest.TestCase):
    def test_count_format(self) -> None:
        self.assertEqual(mapper.format_count(1_200_000), "1.2M")
        self.assertEqual(mapper.format_count(340_000), "340K")
        self.assertEqual(mapper.format_count(999), "999")
        self.assertEqual(mapper.format_count(1_000_000), "1M")

    def test_facets_helper_is_json_ready(self) -> None:
        import json

        payload = json.dumps(build_modrinth_facets("datapack"))
        self.assertEqual(payload, '[["project_type:datapack"]]')


class SanitizeTests(unittest.TestCase):
    def test_script_and_event_handlers_removed(self) -> None:
        dirty = '<p onclick="x()">Hi</p><script>alert(1)</script><a href="javascript:x">a</a>'
        clean = mapper.sanitize_html(dirty)
        self.assertNotIn("<script", clean.lower())
        self.assertNotIn("onclick", clean.lower())
        self.assertNotIn("javascript:", clean.lower())
        self.assertIn("Hi", clean)


class ModpackInspectionTests(unittest.TestCase):
    def _write_zip(self, members: dict[str, str]) -> "Path":
        import tempfile
        import zipfile
        from pathlib import Path

        directory = Path(tempfile.mkdtemp(prefix="pack-"))
        path = directory / "pack.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return path

    def test_curseforge_manifest(self) -> None:
        import json

        from mc_launcher.resources.modpack import inspect_modpack

        path = self._write_zip(
            {
                "manifest.json": json.dumps(
                    {
                        "name": "RLCraft",
                        "version": "2.9.3",
                        "author": "Shivaxi",
                        "minecraft": {
                            "version": "1.12.2",
                            "modLoaders": [{"id": "forge-14.23.5.2860", "primary": True}],
                        },
                        "files": [{"projectID": 1, "fileID": 2}],
                    }
                )
            }
        )
        info = inspect_modpack(path)
        self.assertEqual(info["kind"], "curseforge")
        self.assertEqual(info["mc_version"], "1.12.2")
        self.assertEqual(info["loader_id"], "forge")
        self.assertEqual(info["loader_version"], "14.23.5.2860")
        self.assertEqual(info["file_count"], 1)

    def test_modrinth_index(self) -> None:
        import json

        from mc_launcher.resources.modpack import inspect_modpack

        path = self._write_zip(
            {
                "modrinth.index.json": json.dumps(
                    {
                        "name": "Fabulously Optimized",
                        "versionId": "6.0.0",
                        "files": [{"path": "mods/a.jar", "downloads": ["https://cdn/x.jar"]}],
                        "dependencies": {
                            "minecraft": "1.21.4",
                            "fabric-loader": "0.16.9",
                        },
                    }
                )
            }
        )
        info = inspect_modpack(path)
        self.assertEqual(info["kind"], "mrpack")
        self.assertEqual(info["mc_version"], "1.21.4")
        self.assertEqual(info["loader_id"], "fabric")
        self.assertEqual(info["loader_version"], "0.16.9")


if __name__ == "__main__":
    unittest.main()
