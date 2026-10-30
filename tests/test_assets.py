"""P2-A storage safety and compatibility tests."""

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from cloud_video_studio.assets import AssetStore
from cloud_video_studio.project import Asset, Project


def _import_in_process(store_root: str, source: str) -> None:
    AssetStore(store_root).import_file(source)


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
            reloaded = AssetStore(root / "store")
            self.assertEqual(reloaded.list_assets(), store.list_assets())
            self.assertEqual(json.loads(json.dumps(asset["metadata"])), reloaded.list_assets()[0]["metadata"])

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

    def test_rejects_symlinked_source_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            actual.mkdir()
            source = actual / "caption.srt"
            self.write_srt(source)
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                AssetStore(root / "store").import_file(alias / source.name)

    def test_rejects_symlinked_ancestor_outside_working_directory_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual = root / "actual"
            (actual / "deep").mkdir(parents=True)
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            unrelated_anchor = root / "unrelated"
            with patch.object(Path, "cwd", return_value=unrelated_anchor):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    AssetStore(alias / "deep" / "new" / "store")

    def test_copy_digest_tracks_the_bytes_actually_stored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "captions.srt"
            self.write_srt(source, "Original")
            store = AssetStore(root / "store")
            copy_checked = store._copy_checked

            def mutate_then_copy(path, target, expected_size):
                self.write_srt(source, "Modified")
                return copy_checked(path, target, expected_size)

            with patch.object(store, "_copy_checked", side_effect=mutate_then_copy):
                asset = store.import_file(source)
            stored = store.root / asset["path"]
            self.assertEqual(asset["sha256"], hashlib.sha256(stored.read_bytes()).hexdigest())
            self.assertEqual(asset["metadata"], store._inspect(stored, "subtitle"))

    def test_cross_process_imports_preserve_every_manifest_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store_root = root / "store"
            sources = []
            for index in range(8):
                source = root / f"caption-{index}.srt"
                self.write_srt(source, f"Caption {index}")
                sources.append(source)
            context = multiprocessing.get_context("spawn")
            workers = [context.Process(target=_import_in_process, args=(str(store_root), str(source)))
                       for source in sources]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(len(AssetStore(store_root).list_assets()), len(sources))

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

    def test_probe_metadata_is_json_native_and_validates_audio_stream_details(self):
        video_probe = {
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "1"},
            "streams": [{
                "index": 0, "codec_type": "video", "codec_name": "h264", "profile": "High",
                "width": 64, "height": 64, "avg_frame_rate": "30000/1001",
                "r_frame_rate": "25/1",
            }],
        }
        audio_probe = {
            "format": {"format_name": "mp3", "duration": "1"},
            "streams": [{
                "index": "1", "codec_type": "audio", "codec_name": "mp3",
                "sample_rate": "44100", "channels": 2,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            store = AssetStore(Path(directory) / "store")
            with patch("cloud_video_studio.assets._ffprobe", return_value=video_probe):
                video = store._inspect(Path("sample.mp4"), "video")
            with patch("cloud_video_studio.assets._ffprobe", return_value=audio_probe):
                audio = store._inspect(Path("sample.mp3"), "audio")
            for invalid in ({**audio_probe["streams"][0], "sample_rate": "0"},
                            {**audio_probe["streams"][0], "channels": "unknown"}):
                with patch("cloud_video_studio.assets._ffprobe", return_value={"format": audio_probe["format"], "streams": [invalid]}):
                    with self.assertRaises(ValueError):
                        store._inspect(Path("sample.mp3"), "audio")
        self.assertEqual(json.loads(json.dumps(video))["streams"][0]["fps"], [30000, 1001])
        self.assertEqual(audio["sample_rate"], 44100)
        self.assertEqual(audio["channels"], 2)
        self.assertEqual(audio["streams"][0]["index"], 1)

    def test_github_actions_requires_ffmpeg_and_ffprobe_for_real_media_coverage(self):
        if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
            self.assertIsNotNone(shutil.which("ffmpeg"), "GitHub Actions must install FFmpeg")
            self.assertIsNotNone(shutil.which("ffprobe"), "GitHub Actions must install FFprobe")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe are required")
    def test_real_multiformat_media_import_is_not_skipped_in_github_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixtures = [
                ("wav", ["-f", "lavfi", "-i", "sine=frequency=1000:duration=0.2", "-c:a", "pcm_s16le"]),
                ("mp3", ["-f", "lavfi", "-i", "sine=frequency=1000:duration=0.2", "-c:a", "libmp3lame"]),
                ("png", ["-f", "lavfi", "-i", "color=c=red:s=32x32:d=0.1", "-frames:v", "1"]),
                ("jpg", ["-f", "lavfi", "-i", "color=c=blue:s=32x32:d=0.1", "-frames:v", "1"]),
            ]
            sources = []
            for extension, inputs in fixtures:
                source = root / f"sample.{extension}"
                generated = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-y", *inputs, str(source)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    timeout=30, check=False,
                )
                if generated.returncode:
                    message = f"FFmpeg could not create {extension} fixture"
                    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                        self.fail(message)
                    self.skipTest(message)
                sources.append(source)
            store = AssetStore(root / "store")
            imported = [store.import_file(source) for source in sources]
            self.assertEqual([item["kind"] for item in imported], ["audio", "audio", "image", "image"])
            reloaded = AssetStore(root / "store").list_assets()
            self.assertEqual(reloaded, imported)
            for record in reloaded:
                json.dumps(record["metadata"])

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

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe are required")
    def test_real_media_import_thumbnail_proxy_and_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "synthetic.mp4"
            generated = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                 "color=c=blue:s=64x64:d=0.5:r=25", "-an", "-c:v", "libx264", "-pix_fmt",
                 "yuv420p", "-t", "0.5", str(source)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=30, check=False,
            )
            if generated.returncode:
                if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                    self.fail("GitHub Actions FFmpeg cannot encode the synthetic H.264 fixture")
                self.skipTest("installed FFmpeg cannot encode the synthetic H.264 fixture")
            original = source.read_bytes()
            original_hash = hashlib.sha256(original).hexdigest()
            real_ffprobe = shutil.which("ffprobe")
            self.assertIsNotNone(real_ffprobe)
            custom_ffprobe = root / "custom-ffprobe"
            custom_ffprobe.write_text(
                f"#!/bin/sh\nexec {shlex.quote(real_ffprobe)} \"$@\"\n", encoding="utf-8",
            )
            custom_ffprobe.chmod(0o755)
            isolated_path = root / "empty-path"
            isolated_path.mkdir()
            store = AssetStore(root / "store", max_file_bytes=2 * 1024 * 1024,
                               max_total_bytes=4 * 1024 * 1024, ffprobe=str(custom_ffprobe))
            with patch.dict(os.environ, {"PATH": str(isolated_path)}):
                asset = store.import_file(source)
            self.assertEqual(asset["sha256"], original_hash)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            self.assertEqual(AssetStore(root / "store").list_assets()[0], asset)
            thumbnail = store.create_thumbnail(asset["id"])
            self.assertGreater(thumbnail["size_bytes"], 0)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_hash)
            proxy = store.create_proxy(asset["id"], timeout=30)
            self.assertGreater(proxy["size_bytes"], 0)
            decoded = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-i", str(store.root / proxy["path"]),
                 "-f", "null", "-"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=30, check=False,
            )
            self.assertEqual(decoded.returncode, 0, decoded.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
