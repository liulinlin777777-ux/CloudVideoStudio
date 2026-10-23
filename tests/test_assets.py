"""P2-A storage safety and compatibility tests."""

import hashlib
import json
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from cloud_video_studio.assets import AssetStore
from cloud_video_studio.project import Asset, Project


class AssetStoreTests(unittest.TestCase):
    def write_srt(self, path: Path, text: str = "Caption", end: str = "00:00:01,000") -> None:
        path.write_text(f"1\n00:00:00,000 --> {end}\n{text}\n", encoding="utf-8")

    def test_import_preserves_source_hash_and_reloads_versioned_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "captions.srt"
            self.write_srt(source)
            original = source.read_bytes()
            store = AssetStore(root / "store")
            asset = store.import_file(source)
            self.assertEqual(asset["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(asset["kind"], "subtitle")
            self.assertEqual(asset["metadata_version"], 1)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(AssetStore(root / "store").list_assets(), store.list_assets())

    def test_rejects_unsupported_oversized_invalid_and_aliased_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = root / "not-media.bin"
            invalid.write_bytes(b"x" * 40)
            with self.assertRaises(ValueError):
                AssetStore(root / "unsupported").import_file(invalid)
            large = root / "large.srt"
            self.write_srt(large)
            with self.assertRaises(ValueError):
                AssetStore(root / "small-limit", max_file_bytes=16).import_file(large)
            malformed = root / "malformed.srt"
            malformed.write_text("not an SRT", encoding="utf-8")
            with self.assertRaises(ValueError):
                AssetStore(root / "malformed-store").import_file(malformed)
            store = AssetStore(root / "aliases")
            symlink = root / "symlink.srt"
            symlink.symlink_to(large)
            with self.assertRaises(ValueError):
                store.import_file(symlink)
            hardlink = root / "hardlink.srt"
            hardlink.hardlink_to(large)
            with self.assertRaises(ValueError):
                store.import_file(hardlink)

    def test_relink_manifest_failure_restores_managed_bytes_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "original.srt"
            replacement = root / "replacement.srt"
            self.write_srt(source, "Original")
            self.write_srt(replacement, "Replacement", "00:00:02,000")
            store = AssetStore(root / "store")
            asset = store.import_file(source)
            managed = store.root / asset["path"]
            original_bytes = managed.read_bytes()
            manifest_bytes = store.manifest.read_bytes()
            with patch.object(store, "_write_manifest", side_effect=OSError("simulated commit failure")):
                with self.assertRaises(OSError):
                    store.relink(asset["id"], replacement)
            self.assertEqual(managed.read_bytes(), original_bytes)
            self.assertEqual(store.manifest.read_bytes(), manifest_bytes)
            self.assertEqual(store.list_assets()[0]["sha256"], asset["sha256"])

    def test_manifest_v1_and_schema1_project_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "captions.srt"
            self.write_srt(source)
            store = AssetStore(root / "store")
            asset = store.import_file(source)
            manifest = json.loads(store.manifest.read_text(encoding="utf-8"))
            manifest["version"] = 1
            store.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(AssetStore(root / "store").list_assets()[0]["sha256"], asset["sha256"])

            legacy = Project("legacy", assets=[Asset("captions", "captions.srt", 25)])
            old_payload = legacy.to_dict()
            self.assertEqual(old_payload["schema_version"], 1)
            self.assertNotIn("metadata", old_payload["assets"][0])
            legacy.upgrade_asset_metadata()
            legacy.assets[0].metadata = {"version": 2, "sha256": asset["sha256"]}
            restored = Project.from_dict(legacy.to_dict())
            self.assertEqual(restored.schema_version, 2)
            self.assertEqual(restored.assets[0].metadata["sha256"], asset["sha256"])

    def test_schema_two_requires_explicit_asset_metadata_migration(self):
        legacy = Project("legacy", assets=[Asset("captions", "captions.srt", 25)])
        payload = legacy.to_dict()
        payload["schema_version"] = 2
        with self.assertRaises(ValueError):
            Project.from_dict(payload)
        self.assertEqual(legacy.to_dict()["schema_version"], 1)
        legacy.upgrade_asset_metadata()
        self.assertEqual(legacy.assets[0].metadata, {"version": 2})
        self.assertEqual(Project.from_dict(legacy.to_dict()).schema_version, 2)

    def test_video_probe_metadata_keeps_rational_frame_rate(self):
        from cloud_video_studio.assets import _frame_rate

        self.assertEqual(_frame_rate("30000/1001"), (30000, 1001))
        self.assertIsNone(_frame_rate("0/0"))
        with self.assertRaises(ValueError):
            _frame_rate("not-a-rate")

    def test_concurrent_imports_preserve_every_manifest_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_root = root / "store"
            sources = []
            for index in range(12):
                source = root / f"caption-{index}.srt"
                self.write_srt(source, f"Caption {index}")
                sources.append(source)
            stores = [AssetStore(store_root) for _ in range(4)]
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(lambda pair: pair[0].import_file(pair[1]),
                                  ((stores[index % len(stores)], source)
                                   for index, source in enumerate(sources))))
            self.assertEqual(len(AssetStore(store_root).list_assets()), len(sources))


if __name__ == "__main__":
    unittest.main()
