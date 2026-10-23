"""Frozen independent HTTP contract for loopback browser editing (public synthetic)."""
import copy,hashlib,http.client,json,os,socket,subprocess,sys,tempfile,time,unittest
from pathlib import Path
from cloud_video_studio.media import make_demo_project,generate_sources

class PreviewContract(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
  with socket.socket() as s:s.bind(('127.0.0.1',0));cls.port=s.getsockname()[1]
  cls.process=subprocess.Popen([sys.executable,'tools/preview_server.py','--media-root',str(cls.root),'--port',str(cls.port)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  for _ in range(100):
   try:
    conn=http.client.HTTPConnection('127.0.0.1',cls.port,timeout=1);conn.request('GET','/project');res=conn.getresponse();res.read();conn.close()
    if res.status==200:break
   except OSError:time.sleep(.05)
  else:raise AssertionError('Preview failed to start')
 @classmethod
 def tearDownClass(cls):
  cls.process.terminate();cls.process.wait(timeout=5);cls.process.stdout.close();cls.process.stderr.close();cls.tmp.cleanup()
 def request(self,path,body=None,host=None,origin=None,raw=None):
  c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10);headers={'Host':host or f'127.0.0.1:{self.port}'}
  if origin:headers['Origin']=origin
  if body is not None or raw is not None:headers['Content-Type']='application/json'
  data=raw if raw is not None else json.dumps(body) if body is not None else None
  c.request('POST' if data is not None else 'GET',path,data,headers);r=c.getresponse();content=r.read();c.close()
  return r.status,json.loads(content) if r.getheader('Content-Type','').startswith('application/json') else content
 def project(self):return make_demo_project().to_dict()
 def edit(self,p,operation,clip_id,**fields):return self.request('/edit',{'project':p,'operation':operation,'clip_id':clip_id,**fields})
 def test_move_one_clip_and_preserve_every_other_field(self):
  p=self.project();before=copy.deepcopy(p);code,body=self.edit(p,'move','green-clip',start_frame=35);self.assertEqual(code,200,body)
  expected=copy.deepcopy(p);expected['sequences'][0]['tracks'][1]['clips'][0]['start_frame']=35
  self.assertEqual(body['project'],expected);self.assertEqual(p,before)
 def test_trim_and_source_bounds(self):
  p=self.project();code,b=self.edit(p,'trim','green-clip',source_in_frame=30,duration_frames=40);self.assertEqual(code,200,b)
  expected=copy.deepcopy(p);expected['sequences'][0]['tracks'][1]['clips'][0]['duration_frames']=40
  self.assertEqual(b['project'],expected)
  self.assertEqual(self.edit(p,'trim','green-clip',source_in_frame=140,duration_frames=40)[0],400)
 def test_strict_integer_edits(self):
  for value in [True,-1,1.5,'35',None]:self.assertEqual(self.edit(self.project(),'move','green-clip',start_frame=value)[0],400)
  for value in [True,0,-1,1.5]:self.assertEqual(self.edit(self.project(),'trim','green-clip',source_in_frame=30,duration_frames=value)[0],400)
 def test_unknown_operation_id_and_extra_keys_fail(self):
  self.assertEqual(self.edit(self.project(),'delete','green-clip')[0],400)
  self.assertEqual(self.edit(self.project(),'move','no-such-clip',start_frame=35)[0],400)
  self.assertEqual(self.edit(self.project(),'move','green-clip',start_frame=35,extra=True)[0],400)
 def test_save_reload_and_invalid_write_preserves_saved_bytes(self):
  p=self.project();code,b=self.edit(p,'move','green-clip',start_frame=35);self.assertEqual(code,200,b);edited=b['project']
  self.assertEqual(self.request('/project',edited)[0],200)
  saved=(self.root/'preview-project.json').read_bytes();self.assertEqual(self.request('/project')[1],edited)
  invalid=copy.deepcopy(edited);invalid['sequences'][0]['tracks'][0]['clips'][1]['start_frame']=50
  self.assertEqual(self.request('/project',invalid)[0],400);self.assertEqual((self.root/'preview-project.json').read_bytes(),saved)
 def test_host_origin_body_and_path_guards(self):
  self.assertEqual(self.request('/project',host='evil.example')[0],403)
  self.assertEqual(self.request('/edit',{},origin='https://evil.example')[0],403)
  self.assertEqual(self.request('/edit',raw='x'*65537)[0],413)
  self.assertEqual(self.request('/edit',raw='{')[0],400)
  self.assertEqual(self.request('/media/../preview-project.json')[0],404)
  self.assertEqual(self.request('/media/red.mp4')[0],404)
 def test_media_symlink_is_not_served(self):
  outside=self.root.parent/(self.root.name+'-outside.txt');outside.write_text('private-sentinel');name='preview-'+'a'*32+'.mp4';link=self.root/name
  try:
   link.symlink_to(outside);self.assertIn(self.request('/media/'+name)[0],[403,404])
  finally:link.unlink();outside.unlink()
 def test_saved_project_symlink_is_not_followed_or_overwritten(self):
  saved=self.root/'preview-project.json';old=saved.read_bytes() if saved.exists() else None
  if saved.exists():saved.unlink()
  outside=self.root.parent/(self.root.name+'-outside.json');outside.write_text('sentinel')
  try:
   saved.symlink_to(outside);self.assertEqual(self.request('/project',self.project())[0],400);self.assertEqual(outside.read_text(),'sentinel');self.assertNotEqual(self.request('/project')[0],200)
  finally:
   saved.unlink();outside.unlink()
   if old is not None:saved.write_bytes(old)
 def test_rational_rate_25_roundtrip(self):
  p=self.project();p['sequences'][0]['fps_num']=25;p['sequences'][0]['fps_den']=1
  code,b=self.edit(p,'move','tone880-clip',start_frame=35);self.assertEqual(code,200,b)
  self.assertEqual(b['project']['sequences'][0]['fps_num'],25)

if __name__=="__main__":unittest.main()
