"""P3-03 local FFprobe, FFmpeg scene and keyframe acceptance tests."""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import cloud_video_studio.assets as assets_module
import cloud_video_studio.media_intelligence as media_module
from cloud_video_studio.assets import AssetStore
from cloud_video_studio.media_intelligence import (
    MediaAnalysisError,
    MediaIntelligence,
    _OutputLimitExceeded,
    _bounded_subprocess,
    _select_scene_candidates,
    get_analysis,
    keyframe_path,
    read_keyframe,
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

    def test_scene_selection_covers_the_full_duration(self):
        detections = [
            {"pts": index, "start_seconds": float(time), "score": score}
            for index, (time, score) in enumerate([
                (2, 0.50), (8, 0.80), (18, 0.70), (28, 0.90),
                (45, 0.60), (62, 0.95), (79, 0.85), (97, 0.75),
            ])
        ]
        selected = _select_scene_candidates(detections, limit=4, duration=100)
        self.assertEqual(len(selected), 4)
        self.assertEqual([item["start_seconds"] for item in selected], [8.0, 28.0, 62.0, 79.0])
        self.assertEqual([item["pts"] for item in selected], sorted(item["pts"] for item in selected))

    def test_real_ffmpeg_scene_detection_keyframes_and_cache(self):
        with patch.object(self.analyzer, "_probe", wraps=self.analyzer._probe) as probe_call, \
                patch.object(self.analyzer, "_run", wraps=self.analyzer._run) as run_call, \
                patch.object(self.analyzer, "_version", wraps=self.analyzer._version) as version_call:
            result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20, max_scenes=8)
        deadlines = [probe_call.call_args.kwargs["deadline"]]
        deadlines.extend(call.kwargs["deadline"] for call in run_call.call_args_list)
        deadlines.extend(call.kwargs["deadline"] for call in version_call.call_args_list)
        self.assertEqual(len(set(deadlines)), 1)
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
        frame = read_keyframe(self.store, self.asset["id"], 1, analysis_id=result["analysis_id"])
        self.assertTrue(frame.startswith(b"\xff\xd8\xff"))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)
        cached = get_analysis(self.store, self.asset["id"])
        self.assertEqual(cached, result)
        repeated = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20, max_scenes=8)
        self.assertEqual(repeated["analysis_id"], result["analysis_id"])
        self.assertEqual(search_assets(self.store, "three-scene-clip")[0]["id"], self.asset["id"])
        self.assertEqual(search_assets(self.store, str(self.asset["size_bytes"]))[0]["id"], self.asset["id"])
        self.assertEqual(search_assets(self.store, "h264")[0]["id"], self.asset["id"])
        self.assertEqual(search_assets(self.store, "not-present"), [])

    def test_corrupt_cache_is_reanalyzed(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        cache = self.store.root / "derivatives" / f"{self.asset['id']}-analysis.json"
        cache.write_text("{", encoding="utf-8")
        second = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertEqual(get_analysis(self.store, self.asset["id"]), second)

    def test_previous_cache_schema_is_reanalyzed(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        cache = self.store.root / "derivatives" / f"{self.asset['id']}-analysis.json"
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["schema_version"] = 2
        cache.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(get_analysis(self.store, self.asset["id"]))
        second = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertEqual(get_analysis(self.store, self.asset["id"]), second)

    def test_missing_keyframe_cache_is_reanalyzed(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        (self.store.root / "derivatives" / first["keyframes"][0]).unlink()
        self.assertIsNone(get_analysis(self.store, self.asset["id"]))
        second = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertTrue(all(keyframe_path(self.store, self.asset["id"], i).is_file()
                            for i in range(len(second["keyframes"]))))

    def test_keyframe_request_rejects_a_stale_analysis_version(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        second = self.analyzer.analyze(self.asset["id"], scene_threshold=0.80)
        self.assertNotEqual(first["analysis_id"], second["analysis_id"])
        with self.assertRaisesRegex(MediaAnalysisError, "analysis changed"):
            read_keyframe(self.store, self.asset["id"], 0, analysis_id=first["analysis_id"])

    def test_analysis_cache_reads_are_inside_the_cross_process_lock(self):
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        original_lock = self.store._process_lock
        original_load = media_module._load_analysis
        locked = False

        @contextmanager
        def tracked_process_lock():
            nonlocal locked
            with original_lock():
                locked = True
                try:
                    yield
                finally:
                    locked = False

        def checked_load(*args, **kwargs):
            self.assertTrue(locked, "analysis cache was read without the shared store lock")
            return original_load(*args, **kwargs)

        with patch.object(self.store, "_process_lock", tracked_process_lock), \
                patch.object(media_module, "_load_analysis", side_effect=checked_load):
            self.assertEqual(get_analysis(self.store, self.asset["id"])["analysis_id"], result["analysis_id"])
            self.assertTrue(read_keyframe(self.store, self.asset["id"], 0,
                                          analysis_id=result["analysis_id"]).startswith(b"\xff\xd8\xff"))
            self.assertEqual(search_assets(self.store, "three-scene-clip")[0]["id"], self.asset["id"])

    def test_source_hash_is_amortized_across_keyframe_set_reads(self):
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.store._media_verified_source_fingerprints.clear()
        original_hash = media_module._hash_source_under_deadline
        with patch.object(media_module, "_hash_source_under_deadline", wraps=original_hash) as hash_call:
            self.assertEqual(get_analysis(self.store, self.asset["id"])["analysis_id"], result["analysis_id"])
            for index in range(25):
                frame = read_keyframe(
                    self.store, self.asset["id"], index % len(result["keyframes"]),
                    analysis_id=result["analysis_id"],
                )
                self.assertTrue(frame.startswith(b"\xff\xd8\xff"))
            self.assertEqual(hash_call.call_count, 1)

    def test_analysis_deadline_is_created_before_managed_file_validation(self):
        captured = {}

        def stop_validation(asset_id, *, deadline):
            captured["asset_id"] = asset_id
            captured["deadline"] = deadline
            raise MediaAnalysisError("test validation stop")

        with patch.object(media_module.time, "monotonic", return_value=50.0), \
                patch.object(self.analyzer, "_verified_asset_under_store_lock", side_effect=stop_validation):
            with self.assertRaisesRegex(MediaAnalysisError, "test validation stop"):
                self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertEqual(captured["asset_id"], self.asset["id"])
        self.assertEqual(captured["deadline"], 50.0 + self.analyzer.timeout)

    def test_managed_source_hash_obeys_analysis_deadline(self):
        with patch.object(media_module.time, "monotonic", return_value=11.0):
            with self.assertRaisesRegex(MediaAnalysisError, "ANALYSIS_DEADLINE_EXCEEDED"):
                media_module._hash_source_under_deadline(self.source, deadline=10.0)

    def test_subprocess_timeout_uses_remaining_analysis_deadline(self):
        clock = [0.0]
        calls = []

        def run(command, **kwargs):
            calls.append(kwargs["timeout"])
            if command[0] == "ffprobe":
                stdout = json.dumps({"format": {}, "streams": [{"codec_type": "video"}]})
            elif command[-1] == "-version":
                stdout = "ffmpeg version test"
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with patch.object(media_module.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(media_module, "_bounded_subprocess", side_effect=run):
            self.analyzer._probe(self.source, deadline=10.0)
            clock[0] = 3.0
            self.analyzer._run(["ffmpeg", "scene"], "scene detection", deadline=10.0)
            clock[0] = 6.0
            self.analyzer._version("ffmpeg", deadline=10.0)
        self.assertEqual(calls, [10.0, 7.0, 4.0])

    def test_subprocess_output_is_bounded_while_being_read(self):
        for fd, channel in ((1, "stdout"), (2, "stderr")):
            command = [sys.executable, "-c", f"import os\nwhile True: os.write({fd}, b'x' * 65536)"]
            with self.subTest(channel=channel), self.assertRaisesRegex(_OutputLimitExceeded, channel):
                _bounded_subprocess(command, timeout=5, stdout_limit=128, stderr_limit=128)

    def test_analysis_duration_bounds_both_ffmpeg_input_passes(self):
        with patch.object(media_module, "_ANALYSIS_SECONDS_LIMIT", 1.5), \
                patch.object(self.analyzer, "_run", wraps=self.analyzer._run) as run_call:
            result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertTrue(result["duration_truncated"])
        commands = [call.args[0] for call in run_call.call_args_list]
        self.assertEqual(len(commands), 2)
        for command in commands:
            input_index = command.index("-i")
            self.assertEqual(command[input_index - 2], "-t")
            self.assertEqual(command[input_index - 1], "1.500000")

    def test_video_scene_times_keep_nonzero_stream_origin(self):
        clip = self.root / "offset-scenes.mp4"
        delayed = self.root / "delayed-video-with-audio.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=96x64:r=10:d=1",
            "-f", "lavfi", "-i", "color=c=white:s=96x64:r=10:d=1",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "ultrafast", str(clip),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            "-itsoffset", "5", "-i", str(clip), "-map", "1:v:0", "-map", "0:a:0",
            "-c:v", "copy", "-c:a", "aac", "-avoid_negative_ts", "disabled", str(delayed),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True)
        delayed_asset = self.store.import_file(delayed)
        with patch.object(media_module, "_ANALYSIS_SECONDS_LIMIT", 1.5):
            result = self.analyzer.analyze(delayed_asset["id"], scene_threshold=0.20)
        self.assertGreaterEqual(result["scenes"][0]["start_seconds"], 4.9)
        self.assertEqual(len(result["scenes"]), 2)
        self.assertAlmostEqual(result["scenes"][1]["start_seconds"], 6.0, delta=0.15)
        self.assertEqual(len(result["keyframes"]), 2)

    def test_reanalysis_reserves_replacement_instead_of_double_counting_frames(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        directory = self.store.root / "derivatives"
        cache = directory / f"{self.asset['id']}-analysis.json"
        old_names = set(first["keyframes"])
        old_frame_bytes = sum((directory / name).stat().st_size for name in old_names)
        old_cache_bytes = cache.stat().st_size
        used_before = sum(path.stat().st_size for path in directory.iterdir()
                          if path.is_file() and not path.is_symlink())
        original_limit = assets_module._DERIVATIVE_TOTAL_LIMIT
        original_check = AssetStore._check_derivative_total
        observed = []

        def exact_replacement_check(store, *, replacing=None, additional_bytes=0):
            observed.append((replacing, additional_bytes))
            self.assertTrue(old_names.issubset(replacing))
            self.assertIn(cache.name, replacing)
            self.assertGreater(additional_bytes, 0)
            assets_module._DERIVATIVE_TOTAL_LIMIT = (
                used_before - old_frame_bytes - old_cache_bytes + additional_bytes
            )
            try:
                return original_check(store, replacing=replacing, additional_bytes=additional_bytes)
            finally:
                assets_module._DERIVATIVE_TOTAL_LIMIT = original_limit

        with patch.object(AssetStore, "_check_derivative_total", exact_replacement_check):
            second = self.analyzer.analyze(self.asset["id"], scene_threshold=0.25)
        self.assertNotEqual(second["analysis_id"], first["analysis_id"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(get_analysis(self.store, self.asset["id"]), second)

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

    def test_cached_analysis_and_keyframes_reject_modified_managed_bytes(self):
        result = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertEqual(search_assets(self.store, "candidate_count")[0]["id"], self.asset["id"])
        managed = self.store.root / self.asset["path"]
        managed.write_bytes(managed.read_bytes() + b"tampered")
        with self.assertRaisesRegex(ValueError, "failed its content hash"):
            get_analysis(self.store, self.asset["id"])
        with self.assertRaisesRegex(ValueError, "failed its content hash"):
            read_keyframe(self.store, self.asset["id"], 0, analysis_id=result["analysis_id"])
        self.assertEqual(search_assets(self.store, "candidate_count"), [])

    def test_same_bytes_relink_refreshes_cached_filename_metadata(self):
        first = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        alternate = self.root / "renamed-source.mp4"
        alternate.write_bytes(self.source.read_bytes())
        linked = self.store.relink(self.asset["id"], alternate, filename="renamed-source.mp4")
        self.assertEqual(linked["sha256"], first["source_sha256"])
        self.assertIsNone(get_analysis(self.store, self.asset["id"]))
        refreshed = self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        self.assertEqual(refreshed["metadata"]["filename"], "renamed-source.mp4")
        self.assertEqual(get_analysis(self.store, self.asset["id"]), refreshed)

    def test_cache_hit_revalidates_after_a_concurrent_relink(self):
        self.analyzer.analyze(self.asset["id"], scene_threshold=0.20)
        alternate = self.root / "concurrent-relink.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=yellow:s=96x64:r=10:d=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(alternate),
        ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30, check=True)
        original_load = media_module._load_analysis
        relinked = False
        relink_errors = []
        relink_thread = None

        class WaitForRelink:
            def __enter__(self):
                relink_thread.join(timeout=5)
                if relink_thread.is_alive():
                    raise AssertionError("concurrent relink did not finish after cache read released the lock")
                if relink_errors:
                    raise relink_errors[0]
                return self

            def __exit__(self, *args):
                return False

        def load_then_relink(store, asset_id, record):
            nonlocal relinked, relink_thread
            cached = original_load(store, asset_id, record)
            if not relinked:
                relinked = True
                def relink():
                    try:
                        store.relink(asset_id, alternate, filename="concurrent-relink.mp4")
                    except BaseException as error:
                        relink_errors.append(error)
                relink_thread = threading.Thread(target=relink, daemon=True)
                relink_thread.start()
            return cached

        with patch.object(media_module, "_load_analysis", side_effect=load_then_relink):
            result = self.analyzer.analyze(
                self.asset["id"], scene_threshold=0.20, publication_lock=WaitForRelink(),
            )
        self.assertTrue(relinked)
        self.assertEqual(result["metadata"]["filename"], "concurrent-relink.mp4")
        self.assertEqual(result["source_sha256"], self.store.list_assets()[0]["sha256"])

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
