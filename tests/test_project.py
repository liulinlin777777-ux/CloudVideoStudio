import unittest, tempfile, hashlib
from pathlib import Path
from fractions import Fraction
from cloud_video_studio.project import Project, Sequence, Track, Clip, Asset, Effect, RenderJob

class ProjectTests(unittest.TestCase):
 def sample(self,uri='synthetic:video'):
  a=Asset(id='a',uri=uri,duration_frames=300)
  effect=Effect(kind='opacity',parameters={'value':0.5})
  clip=Clip(id='c',asset_id='a',start_frame=0,duration_frames=120,source_in_frame=30,effects=[effect])
  seq=Sequence(id='s',width=640,height=360,fps_num=30000,fps_den=1001,tracks=[Track(id='v1',kind='video',clips=[clip]),Track(id='a1',kind='audio',clips=[])])
  return Project(id='p',assets=[a],sequences=[seq],render_jobs=[RenderJob(id='j',sequence_id='s',output_uri='output.mp4')])
 def test_round_trip_and_nested_types(self):
  p=self.sample();p.validate();q=Project.from_dict(p.to_dict())
  self.assertEqual(q.to_dict(),p.to_dict());self.assertIsInstance(q.sequences[0].tracks[0].clips[0].effects[0],Effect)
  self.assertIsInstance(q.assets[0],Asset);self.assertIsInstance(q.render_jobs[0],RenderJob)
 def test_save_load_preserves_original_asset(self):
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'source.mov';source.write_bytes(b'original-source-sentinel')
   original=hashlib.sha256(source.read_bytes()).hexdigest();p=self.sample(str(source))
   target=Path(d)/'project.json';p.save(target);q=Project.load(target)
   self.assertEqual(p.to_dict(),q.to_dict());self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original)
 def test_rational_frame_rate_is_exact(self):
  s=self.sample().sequences[0]
  self.assertEqual(s.frame_time(30000),Fraction(1001,1));self.assertEqual(s.frame_time(1),Fraction(1001,30000))
  self.assertEqual(Project.from_dict(self.sample().to_dict()).sequences[0].fps_num,30000)
 def test_unsupported_version_and_unknown_fields_rejected(self):
  for key,value in [('schema_version',2),('unexpected','untrusted')]:
   x=self.sample().to_dict();x[key]=value
   with self.assertRaises((ValueError,TypeError)):Project.from_dict(x)
 def test_missing_asset_and_sequence_rejected(self):
  p=self.sample();p.assets=[]
  with self.assertRaises(ValueError):p.validate()
  p=self.sample();p.render_jobs[0].sequence_id='missing'
  with self.assertRaises(ValueError):p.validate()
 def test_duplicate_ids_rejected(self):
  p=self.sample();p.assets.append(p.assets[0])
  with self.assertRaises(ValueError):p.validate()
 def test_frame_bounds_and_rate_rejected(self):
  for value in [0,-1,1.5,True]:
   p=self.sample();p.sequences[0].fps_den=value
   with self.assertRaises(ValueError):p.validate()
  for field,value in [('source_in_frame',-1),('duration_frames',0),('source_in_frame',250),('start_frame',-1)]:
   p=self.sample();setattr(p.sequences[0].tracks[0].clips[0],field,value)
   with self.assertRaises(ValueError):p.validate()
 def test_track_kind_rejected(self):
  p=self.sample();p.sequences[0].tracks[0].kind='shell'
  with self.assertRaises(ValueError):p.validate()

if __name__=='__main__':unittest.main()

class SourceProtectionTests(unittest.TestCase):
 def test_save_cannot_overwrite_asset_by_absolute_or_relative_path(self):
  for relative in [False,True]:
   with tempfile.TemporaryDirectory() as d:
    source=Path(d)/'source.mov';source.write_bytes(b'source-sentinel')
    p=ProjectTests().sample(source.name if relative else str(source))
    with self.assertRaises(ValueError):p.save(source)
    self.assertEqual(source.read_bytes(),b'source-sentinel')
 def test_save_cannot_overwrite_asset_through_hardlink(self):
  import os
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'source.mov';source.write_bytes(b'source-sentinel');alias=Path(d)/'alias.json';os.link(source,alias)
   p=ProjectTests().sample(str(source))
   with self.assertRaises(ValueError):p.save(alias)
   self.assertEqual(source.read_bytes(),b'source-sentinel')
