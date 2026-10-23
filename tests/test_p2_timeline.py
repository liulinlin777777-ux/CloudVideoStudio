"""Frozen P2-B contract checks for bounded multi-track projects and edits."""
import copy
import unittest
from fractions import Fraction

from cloud_video_studio.media import compile_timeline
from cloud_video_studio.project import Asset, Clip, Project, Sequence, Track
from cloud_video_studio.timeline_edits import (
    add_clip, add_track, delete_clip, move_clip_to_track, reorder_clip, split_clip,
)


def sample_project():
    assets = [Asset(f"asset-{index}", f"media-{index}.mp4", 1500) for index in range(6)]
    video_one = Track("V1", "video", [
        Clip("one", "asset-0", 0, 100),
        Clip("two", "asset-1", 150, 100),
    ])
    video_two = Track("V2", "video", [Clip("pip", "asset-2", 0, 150)])
    audio_one = Track("A1", "audio", [Clip("audio", "asset-3", 0, 1500)])
    sequence = Sequence("sequence", 640, 360, 25, 1, [video_one, video_two, audio_one])
    return Project("project", assets, [sequence])


class BoundedTimelineTests(unittest.TestCase):
    def test_sixty_second_exact_rate_compiles_without_mutating_project(self):
        project = sample_project()
        before = copy.deepcopy(project.to_dict())
        compiled = compile_timeline(project)
        self.assertEqual(compiled["duration_frames"], 1500)
        self.assertEqual(compiled["fps"], "25/1")
        self.assertEqual(project.to_dict(), before)
        self.assertIn("[vout]", compiled["filtergraph"])

    def test_schema_one_round_trip_and_exact_rational_time(self):
        project = sample_project()
        project.sequences[0].fps_num, project.sequences[0].fps_den = 30000, 1001
        restored = Project.from_dict(project.to_dict())
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.to_dict(), project.to_dict())
        self.assertEqual(restored.sequences[0].frame_time(30000), Fraction(1001, 1))

    def test_clip_count_and_track_count_are_bounded(self):
        project = sample_project()
        project.sequences[0].tracks.extend([
            Track("V3", "video"), Track("V4", "video"), Track("V5", "video"),
        ])
        with self.assertRaises(ValueError):
            project.validate()
        project = sample_project()
        for index in range(18):
            asset = Asset(f"extra-{index}", f"extra-{index}.mp4", 100)
            project.assets.append(asset)
            project.sequences[0].tracks[0].clips.append(Clip(f"extra-clip-{index}", asset.id, 0, 10))
        with self.assertRaises(ValueError):
            project.validate()

    def test_split_preserves_source_ranges_and_input(self):
        project = sample_project()
        before = copy.deepcopy(project.to_dict())
        edited = split_clip(project, "one", 40)
        clips = {clip.id: clip for track in edited.sequences[0].tracks for clip in track.clips}
        self.assertEqual((clips["one"].duration_frames, clips["one"].source_in_frame), (40, 0))
        split = next(clip for clip in clips.values() if clip.id != "one" and clip.asset_id == "asset-0")
        self.assertEqual((split.start_frame, split.source_in_frame, split.duration_frames), (40, 40, 60))
        self.assertEqual(project.to_dict(), before)

    def test_move_snap_reorder_and_delete_are_immutable(self):
        project = sample_project()
        moved = move_clip_to_track(project, "one", 148, "V2", snap_frames=3)
        self.assertEqual((moved.sequences[0].tracks[1].clips[-1].id,
                          moved.sequences[0].tracks[1].clips[-1].start_frame), ("one", 150))
        reordered = reorder_clip(project, "two", "one")
        self.assertEqual(reordered.sequences[0].tracks[0].clips[1].start_frame, 0)
        deleted = delete_clip(project, "one")
        self.assertFalse(any(clip.id == "one" for track in deleted.sequences[0].tracks for clip in track.clips))
        self.assertTrue(any(clip.id == "one" for track in project.sequences[0].tracks for clip in track.clips))

    def test_add_asset_as_clip_uses_existing_asset_reference(self):
        project = sample_project()
        edited = add_clip(project, "asset-4", "V2", 300, "new-clip")
        added = next(clip for clip in edited.sequences[0].tracks[1].clips if clip.id == "new-clip")
        self.assertEqual((added.asset_id, added.start_frame, added.duration_frames), ("asset-4", 300, 1200))
        self.assertFalse(any(clip.id == "new-clip" for track in project.sequences[0].tracks for clip in track.clips))

    def test_tracks_can_be_added_with_four_per_kind_limit(self):
        project = add_track(sample_project(), "video", "V3")
        project = add_track(project, "video", "V4")
        project = add_track(project, "audio", "A2")
        with self.assertRaises(ValueError):
            add_track(project, "video", "V5")
        self.assertEqual([track.id for track in project.sequences[0].tracks[-3:]], ["V3", "V4", "A2"])

    def test_unsupported_frame_rate_and_overlong_timeline_fail(self):
        project = sample_project()
        project.sequences[0].fps_num, project.sequences[0].fps_den = 24, 1
        with self.assertRaises(ValueError):
            compile_timeline(project)
        project = sample_project()
        project.sequences[0].tracks[2].clips[0].duration_frames = 1501
        with self.assertRaises(ValueError):
            compile_timeline(project)


if __name__ == "__main__":
    unittest.main()
