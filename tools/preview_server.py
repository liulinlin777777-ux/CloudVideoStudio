"""Loopback-only P0 synthetic preview; no deployment, credentials or uploads."""
import argparse,json,threading,uuid
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from cloud_video_studio.project import Project
from cloud_video_studio.media import make_demo_project,render_project

p=argparse.ArgumentParser();p.add_argument('--media-root',required=True);p.add_argument('--port',type=int,default=8774);p.add_argument('--ffmpeg',default='ffmpeg');a=p.parse_args()
root=Path(a.media_root).resolve();html=Path(__file__).resolve().parents[1]/'web/index.html';lock=threading.Lock()

def current():
 f=root/'preview-project.json'
 return Project.load(f) if f.exists() else make_demo_project()

class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,code,body,kind='application/json'):
  data=json.dumps(body).encode() if kind=='application/json' else body
  self.send_response(code);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
 def safe(self):
  return self.headers.get('Host') in {f'127.0.0.1:{a.port}',f'localhost:{a.port}'} and self.headers.get('Origin') in {None,f'http://127.0.0.1:{a.port}',f'http://localhost:{a.port}'}
 def do_GET(self):
  if not self.safe():return self.reply(403,{'error':'ORIGIN_DENIED'})
  if self.path=='/':return self.reply(200,html.read_bytes(),'text/html; charset=utf-8')
  if self.path=='/project':return self.reply(200,current().to_dict())
  if self.path.startswith('/media/'):
   name=self.path.removeprefix('/media/')
   if name=='render.mp4' or (name.startswith('preview-') and name.endswith('.mp4') and len(name)==44 and all(c in '0123456789abcdef-' for c in name[8:-4])):
    file=root/name
    if file.is_file():return self.reply(200,file.read_bytes(),'video/mp4')
  return self.reply(404,{'error':'NOT_FOUND'})
 def do_POST(self):
  if not self.safe():return self.reply(403,{'error':'ORIGIN_DENIED'})
  if self.path not in {'/project','/render'}:return self.reply(404,{'error':'NOT_FOUND'})
  try:
   length=int(self.headers.get('Content-Length','0'))
   if not 0<length<=65536:return self.reply(413,{'error':'BODY_SIZE'})
   project=Project.from_dict(json.loads(self.rfile.read(length)))
   if self.path=='/project':
    project.save(root/'preview-project.json');return self.reply(200,{'status':'saved','project':project.to_dict()})
   if not lock.acquire(blocking=False):return self.reply(409,{'error':'RENDER_BUSY'})
   try:
    name='preview-'+uuid.uuid4().hex+'.mp4'
    receipt=render_project(project,root,root/name,ffmpeg=a.ffmpeg)
    return self.reply(200,{'status':receipt['status'],'url':'/media/'+name,'wall_seconds':receipt['wall_seconds'],'output_bytes':receipt['output_bytes']})
   finally:lock.release()
  except Exception as error:return self.reply(400,{'error':type(error).__name__})

server=ThreadingHTTPServer(('127.0.0.1',a.port),Handler)
print(f'P0_PREVIEW=http://127.0.0.1:{a.port}',flush=True)
server.serve_forever()
