"""P3-03 local FFprobe, FFmpeg scene and keyframe acceptance tests."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cloud_video_studio.assets import AssetStore
from cloud_video_studio.media_intelligence import (
    MediaAnalysisError,
    MediaIntelligence,
    get_analysis,
    keyframe_path,
    search_assets,
)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/FFprobe are required")
class MediaIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "three-scene-clip.mp4"
        command = [
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=96x64:r=10:d=1",
            "-f", "lavfi", "-i", "color=c=white:s=96x64:r=10:d=1",
            "-f", "lavfi", "-i", "color=c=black:s=96x64:r=10:d=1",
            "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0,format=yuv420p[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "10",
            str(self.source),
        ]
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=30, check=False)
        if result.returncode:
            self.temporary.cleanup()
            self.skipTest("FFmpeg could not generate the local scene-boundary fixture")
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.store = AssetStore(self.root / "store", max_file_bytes=16 * 1024 * 1024,
                                max_total_bytes=32 * 1024 * 1024)
        self.asset = self.store.import_file(self.source)
        self.analyzer = MediaIntelligence(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_ffmpeg_scene_detection_keyframes_and_cache(self):
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20, max_scenes=8)
        self.assertEqual(result["metadata"]["container"].split(",")[0], "mov")
        self.assertEqual((result["metadata"]["width"], result["metadata"]["height"]), (96, 64))
        self.assertAlmostEqual(result["metadata"]["duration_seconds"], 3.0, delta=0.15)
        self.assertEqual(result["metadata"]["streams"][0]["index"], 0)
        self.assertEqual(len(result["scenes"]), 3)
        self.assertAlmostEqual(result["scenes"][1]["start_seconds"], 1.0, delta=0.15)
        self.assertAlmostEqual(result["scenes"][2]["start_seconds"], 2.0, delta=0.15)
        self.assertEqual(len(result["keyframes"]), 3)
        self.assertTrue(all(keyframe_path(self.store, self.asset["id"], index).is_file()
                            for index in range(3)))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)
        cached = get_analysis(self.store, self.asset["id"])
        self.assertEqual(cached, result)
        repeated = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20, max_scenes=8)
        self.assertEqual(repeated["analysis_id"], result["analysis_id"])
        self.assertEqual(search_assets(self.store, "three-scene-clip")[0]["id"], self.asset["id"])
        self.assertEqual(search_assets(self.store, "h264")[0]["id"], self.asset["id"])
        self.assertEqual(search_assets(self.store, "not-present"), [])

    def test_scene_count_is_bounded_and_reports_truncation(self):
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20, max_scenes=1)
        self.assertGreaterEqual(result["scene_detection"]["candidate_count"], 2)
        self.assertTrue(result["scene_detection"]["truncated"])
        self.assertEqual(len(result["scenes"]), 2)
        self.assertEqual(len(result["keyframes"]), 2)

    def test_rejects_invalid_limits_wrong_asset_kind_and_tampered_media(self):
        for kwargs in ({"scene_threshold": True}, {"scene_threshold": float("nan")},
                       {"scene_threshold": 1.0}, {"max_scenes": True}, {"max_scenes": 25}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MediaAnalysisError):
                self.analyzer.analyze(self.asset["id"], **kwargs)
        with self.assertRaisesRegex(MediaAnalysisError, "invalid asset id"):
            self.analyzer.analyze("../outside")
        caption = self.root / "caption.srt"
        caption.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
        audio_asset = self.store.import_file(caption)
        with self.assertRaisesRegex(ValueError, "asset must be video"):
            self.analyzer.analyze(audio_asset["id"])
        managed = self.store.root / self.asset["path"]
        managed.write_bytes(managed.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "failed its content hash"):
            self.analyzer.analyze(self.asset["id"])

    def test_cache_is_invalidated_after_relink_to_different_video(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        alternate = self.root / "alternate.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=yellow:s=96x64:r=10:d=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(alternate),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True)
        linked = self.store.relink(self.asset["id"], alternate, filename="alternate.mp4")
        self.assertNotEqual(linked["sha256"], first["source_sha256"])
        self.assertIsNone(get_analysis(self.store, self.asset["id"]))

    def test_search_and_cache_paths_are_bounded(self):
        with self.assertRaisesRegex(MediaAnalysisError, "invalid search query"):
            search_assets(self.store, "x" * 121)
        with self.assertRaisesRegex(MediaAnalysisError, "invalid keyframe index"):
            keyframe_path(self.store, self.asset["id"], -1)
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        cache = self.store.root / "derivatives" / f"{self.asset['id']}-analysis.json"
        payload = json.loads(cache.read_text(encoding="utf-8"))
        self.assertEqual(payload["source_sha256"], self.asset["sha256"])
        self.assertEqual(len(result["keyframes"]), len(result["scenes"]))


if __name__ == "__main__":
    unittest.main()
