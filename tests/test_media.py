import unittest,tempfile,subprocess
from pathlib import Path
from unittest.mock import patch
from cloud_video_studio.media import make_demo_project,compile_timeline,render_project
from cloud_video_studio.plugins import run_plugin,PluginError,PLUGIN

class MediaTests(unittest.TestCase):
 def test_real_topology_and_rational_graph(self):
  p=make_demo_project();p.validate();s=p.sequences[0]
  self.assertEqual([t.kind for t in s.tracks],['video','video','audio','audio'])
  c=compile_timeline(p);self.assertEqual(c['duration_frames'],120);self.assertEqual(c['fps'],'30000/1001')
  self.assertEqual(c['inputs'],['red','blue','green','tone440','tone880'])
  for f in ['trim=','xfade=','overlay=','atrim=','adelay=','amix='] :self.assertIn(f,c['filtergraph'])
 def test_trim_is_compiled_from_model(self):
  p=make_demo_project();p.sequences[0].tracks[0].clips[0].source_in_frame=20
  self.assertIn('start_frame=20',compile_timeline(p)['filtergraph'])
 def test_unknown_effect_not_interpreted_as_filter(self):
  from cloud_video_studio.project import Effect
  p=make_demo_project();p.sequences[0].tracks[0].clips[0].effects.append(Effect('movie',{'url':'https://example.com'}))
  with self.assertRaises(ValueError):compile_timeline(p)
 def test_nonfinite_volume_rejected(self):
  p=make_demo_project();p.sequences[0].tracks[2].clips[0].effects[0].parameters['gain']=float('nan')
  with self.assertRaises(ValueError):compile_timeline(p)
 def test_render_refuses_source_overwrite_and_escape(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p=make_demo_project()
   for asset in p.assets:(root/asset.uri).write_bytes(b'sentinel')
   with patch('cloud_video_studio.media.subprocess.run') as runner:
    for output in [root/p.assets[0].uri,root/'..'/'escape.mp4']:
     with self.assertRaises(ValueError):render_project(p,root,output)
    p.assets[0].uri='../escape.mp4'
    with self.assertRaises(ValueError):render_project(p,root,root/'out.mp4')
    runner.assert_not_called()
 def test_plugin_manifest_and_unknown_plugin(self):
  self.assertEqual(PLUGIN['id'],'builtin.proxy');self.assertEqual(PLUGIN['version'],'1.0.0')
  self.assertIn('video.proxy',PLUGIN['capabilities'])
  with self.assertRaises(PluginError):run_plugin('remote.untrusted',Path('a'),Path('b'))
 def test_plugin_timeout_is_classified(self):
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'in.mp4';source.write_bytes(b'sentinel')
   with patch('cloud_video_studio.plugins.subprocess.run',side_effect=subprocess.TimeoutExpired('ffmpeg',0.01)):
    with self.assertRaisesRegex(PluginError,'TIMEOUT'):run_plugin('builtin.proxy',source,Path(d)/'out.mp4',timeout=0.01)
   self.assertEqual(source.read_bytes(),b'sentinel')
 def test_plugin_rejects_input_output_alias(self):
  with tempfile.TemporaryDirectory() as d:
   source=Path(d)/'in.mp4';source.write_bytes(b'sentinel')
   with self.assertRaises(PluginError):run_plugin('builtin.proxy',source,source)

if __name__=='__main__':unittest.main()

class IndependentRegressionTests(unittest.TestCase):
 def test_frame_edit_and_sample_delay(self):
  for n in [0,1,36,60]:
   p=make_demo_project();p.sequences[0].tracks[1].clips[0].start_frame=n;p.sequences[0].tracks[3].clips[0].start_frame=n
   g=compile_timeline(p)['filtergraph']
   self.assertIn('asetpts=N/SR/TB',g);self.assertIn('S',g)
 def test_volume_resource_bounds(self):
  p=make_demo_project();p.sequences[0].tracks[2].clips[0].effects[0].parameters['gain']=1.5
  with self.assertRaises(ValueError):compile_timeline(p)
  for timeout,threads in [(float('nan'),2),(float('inf'),2),(31,2),(30,3)]:
   with self.assertRaises(PluginError):run_plugin('builtin.proxy',Path('a'),Path('b'),timeout=timeout,threads=threads)
