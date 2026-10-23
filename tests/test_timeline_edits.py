import unittest
from fractions import Fraction
from cloud_video_studio.project import Project,Asset,Sequence,Track,Clip
from cloud_video_studio.timeline_edits import move_clip,trim_clip

def project():
 return Project('p',[Asset('a','public.mp4',300)],[Sequence('s',640,360,30000,1001,[Track('v','video',[Clip('c','a',0,75,15)]),Track('a1','audio',[Clip('sound','a',0,120,0)])])])
class EditTests(unittest.TestCase):
 def test_move_is_immutable(self):
  p=project();before=p.to_dict();q=move_clip(p,'c',36)
  self.assertEqual(p.to_dict(),before);self.assertEqual(q.sequences[0].tracks[0].clips[0].start_frame,36)
  self.assertEqual(q.sequences[0].frame_time(36),Fraction(3003,2500))
 def test_trim_preserves_placement_and_input(self):
  p=project();before=p.to_dict();q=trim_clip(p,'c',30,60)
  c=q.sequences[0].tracks[0].clips[0]
  self.assertEqual((c.start_frame,c.source_in_frame,c.duration_frames),(0,30,60));self.assertEqual(p.to_dict(),before)
 def test_invalid_move(self):
  for value in [-1,True,1.5,'1']:
   with self.assertRaises(ValueError):move_clip(project(),'c',value)
 def test_invalid_trim(self):
  for start,duration in [(-1,20),(0,0),(True,20),(0,True),(1.5,20),(290,20)]:
   with self.assertRaises(ValueError):trim_clip(project(),'c',start,duration)
 def test_missing_clip(self):
  with self.assertRaises(ValueError):move_clip(project(),'missing',1)
  with self.assertRaises(ValueError):trim_clip(project(),'missing',0,1)
 def test_audio_and_roundtrip(self):
  q=move_clip(project(),'sound',25);q=trim_clip(q,'sound',20,80)
  self.assertEqual(q.to_dict(),Project.from_dict(q.to_dict()).to_dict());self.assertEqual(q.sequences[0].tracks[1].clips[0].start_frame,25)
 def test_effects_do_not_alias(self):
  from cloud_video_studio.project import Effect
  p=project();p.sequences[0].tracks[0].clips[0].effects=[Effect('gain',{'nested':[1]})]
  q=move_clip(p,'c',2);q.sequences[0].tracks[0].clips[0].effects[0].parameters['nested'].append(2)
  self.assertEqual(p.sequences[0].tracks[0].clips[0].effects[0].parameters['nested'],[1])
 def test_invalid_input_project_rejected(self):
  p=project();p.sequences[0].tracks[0].clips[0].asset_id='missing'
  with self.assertRaises(ValueError):move_clip(p,'c',2)
