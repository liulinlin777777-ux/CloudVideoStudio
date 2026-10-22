"""Frozen independent contract tests for P1 slice 2; no FFmpeg needed here."""
import copy, unittest
from cloud_video_studio.media import make_demo_project, compile_timeline
from cloud_video_studio.timeline_edits import move_clip, trim_clip

class BoundedCompilerTests(unittest.TestCase):
 def test_two_exact_rates_and_legacy(self):
  for rate in [(30000,1001),(25,1)]:
   p=make_demo_project();s=p.sequences[0];s.fps_num,s.fps_den=rate
   c=compile_timeline(p);self.assertEqual(c['fps'],f'{rate[0]}/{rate[1]}');self.assertEqual(c['duration_frames'],120)
 def test_gap_and_initial_black_move_trim(self):
  p=make_demo_project();p=move_clip(p,'red-clip',10);p=trim_clip(p,'red-clip',20,45);p=move_clip(p,'blue-clip',70);p=trim_clip(p,'blue-clip',15,60)
  p.sequences[0].tracks[0].clips[1].effects=[]
  before=copy.deepcopy(p.to_dict());c=compile_timeline(p)
  self.assertEqual(c['duration_frames'],130);self.assertEqual(p.to_dict(),before)
  self.assertNotEqual(c['filtergraph'],compile_timeline(make_demo_project())['filtergraph'])
 def test_rebased_crossfade_preserves_overlap(self):
  p=move_clip(make_demo_project(),'red-clip',10);p=move_clip(p,'blue-clip',55)
  self.assertEqual(compile_timeline(p)['duration_frames'],130)
 def test_order_is_timeline_order_not_list_or_asset_identity(self):
  p=make_demo_project();expected=compile_timeline(p);p.sequences[0].tracks[0].clips.reverse();p.assets.reverse()
  self.assertEqual(compile_timeline(p),expected)
  rename={a.id:'renamed-'+a.id for a in p.assets}
  for a in p.assets:a.id=rename[a.id]
  for t in p.sequences[0].tracks:
   for c in t.clips:c.asset_id=rename[c.asset_id]
  self.assertEqual(set(compile_timeline(p)['inputs']),set(rename.values()))
 def test_duration_and_variable_pip_and_audio(self):
  p=make_demo_project();s=p.sequences[0];pip=s.tracks[1].clips[0];pip.start_frame=10;pip.duration_frames=35
  a=s.tracks[3].clips[0];a.start_frame=80;a.duration_frames=65;a.effects[0].parameters['gain']=0
  self.assertEqual(compile_timeline(p)['duration_frames'],145)
 def test_abutting_clips_without_crossfade(self):
  p=move_clip(make_demo_project(),'blue-clip',75);p.sequences[0].tracks[0].clips[1].effects=[]
  self.assertEqual(compile_timeline(p)['duration_frames'],150)
 def test_invalid_semantics_rejected(self):
  def gap_effect(p):p.sequences[0].tracks[0].clips[1].start_frame=80
  def missing_overlap_effect(p):p.sequences[0].tracks[0].clips[1].effects=[]
  def wrong_overlap(p):p.sequences[0].tracks[0].clips[1].effects[0].parameters['duration_frames']=20
  def same_start(p):p.sequences[0].tracks[0].clips[1].start_frame=0
  def source_bounds(p):p.sequences[0].tracks[0].clips[0].source_in_frame=100
  def huge_duration(p):p.sequences[0].tracks[2].clips[0].start_frame=301
  def extra_track(p):p.sequences[0].tracks.append(copy.deepcopy(p.sequences[0].tracks[0]))
  def no_pip(p):p.sequences[0].tracks[1].clips[0].effects=[]
  def bad_gain(p):p.sequences[0].tracks[2].clips[0].effects[0].parameters['gain']=float('inf')
  def unsupported_fps(p):p.sequences[0].fps_num=60;p.sequences[0].fps_den=1
  def bad_rect(p):p.sequences[0].tracks[1].clips[0].effects[0].parameters['x']=639
  for mutation in [gap_effect,missing_overlap_effect,wrong_overlap,same_start,source_bounds,huge_duration,extra_track,no_pip,bad_gain,unsupported_fps,bad_rect]:
   with self.subTest(mutation=mutation.__name__):
    p=make_demo_project();mutation(p)
    with self.assertRaises(ValueError):compile_timeline(p)
