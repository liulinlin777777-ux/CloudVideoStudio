"""Loopback-only P0/P1 synthetic preview; no deployment, credentials or uploads."""
import argparse
import json
import os
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_video_studio.project import Project
from cloud_video_studio.timeline_edits import move_clip, trim_clip
from cloud_video_studio.media import compile_timeline, make_demo_project, render_project

p = argparse.ArgumentParser()
p.add_argument('--media-root', required=True)
p.add_argument('--port', type=int, default=8774)
p.add_argument('--ffmpeg', default='ffmpeg')
a = p.parse_args()
root = Path(a.media_root).resolve()
html = Path(__file__).resolve().parents[1] / 'web/index.html'
lock = threading.Lock()
MAX_BODY = 65536


def saved_path():
    return root / 'preview-project.json'


def current():
    path = saved_path()
    if path.is_symlink():
        raise ValueError('saved project must not be a symlink')
    return Project.load(path) if path.exists() else make_demo_project()


def decode_project(data):
    if not isinstance(data, dict):
        raise ValueError('project must be an object')
    project = Project.from_dict(data)
    compile_timeline(project)
    return project


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def safe(self):
        return self.headers.get('Host') in {f'127.0.0.1:{a.port}', f'localhost:{a.port}'} and self.headers.get('Origin') in {
            None, f'http://127.0.0.1:{a.port}', f'http://localhost:{a.port}'
        }

    def media(self, name):
        allowed = name == 'render.mp4' or (
            name.startswith('preview-') and name.endswith('.mp4') and len(name) == 44
            and all(c in '0123456789abcdef' for c in name[8:-4])
        )
        if not allowed:
            return self.reply(404, {'error': 'NOT_FOUND'})
        file = root / name
        if file.is_symlink():
            return self.reply(404, {'error': 'NOT_FOUND'})
        try:
            if not file.is_file():
                return self.reply(404, {'error': 'NOT_FOUND'})
            return self.reply(200, file.read_bytes(), 'video/mp4')
        except OSError:
            return self.reply(404, {'error': 'NOT_FOUND'})

    def do_GET(self):
        if not self.safe():
            return self.reply(403, {'error': 'ORIGIN_DENIED'})
        try:
            if self.path == '/':
                return self.reply(200, html.read_bytes(), 'text/html; charset=utf-8')
            if self.path == '/project':
                project = current()
                compile_timeline(project)
                return self.reply(200, project.to_dict())
            if self.path.startswith('/media/') and '/' not in self.path[7:]:
                return self.media(self.path[7:])
            return self.reply(404, {'error': 'NOT_FOUND'})
        except Exception as error:
            return self.reply(400, {'error': type(error).__name__})

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError):
            raise ValueError('invalid content length')
        if not 0 < length <= MAX_BODY:
            raise OverflowError('body size')
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValueError('incomplete request body')
        return json.loads(raw)

    def save_project(self, project):
        destination = saved_path()
        if destination.is_symlink():
            raise ValueError('saved project must not be a symlink')
        # Preserve Project.save's source alias protection before atomic replacement.
        for asset in project.assets:
            source = Path(asset.uri)
            if not source.is_absolute():
                source = root / source
            try:
                source_stat = source.stat()
                destination_stat = destination.stat()
                if (source_stat.st_dev, source_stat.st_ino) == (destination_stat.st_dev, destination_stat.st_ino):
                    raise ValueError('project file cannot overwrite a hardlink to a referenced asset')
            except FileNotFoundError:
                pass
            if source.resolve() == destination.resolve():
                raise ValueError('project file cannot overwrite a referenced asset')
        payload = json.dumps(project.to_dict(), ensure_ascii=False, indent=2) + '\n'
        fd, temporary = tempfile.mkstemp(prefix='.preview-project-', dir=root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.is_symlink():
                raise ValueError('saved project must not be a symlink')
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def do_POST(self):
        if not self.safe():
            return self.reply(403, {'error': 'ORIGIN_DENIED'})
        try:
            data = self.read_json()
        except OverflowError:
            return self.reply(413, {'error': 'BODY_SIZE'})
        except Exception as error:
            return self.reply(400, {'error': type(error).__name__})
        try:
            if self.path == '/edit':
                if not isinstance(data, dict):
                    raise ValueError('request must be an object')
                operation = data.get('operation')
                if operation == 'move':
                    if set(data) != {'project', 'operation', 'clip_id', 'start_frame'}:
                        raise ValueError('invalid move fields')
                    if not isinstance(data['clip_id'], str):
                        raise ValueError('clip_id must be a string')
                    project = decode_project(data['project'])
                    edited = move_clip(project, data['clip_id'], data['start_frame'])
                elif operation == 'trim':
                    if set(data) != {'project', 'operation', 'clip_id', 'source_in_frame', 'duration_frames'}:
                        raise ValueError('invalid trim fields')
                    if not isinstance(data['clip_id'], str):
                        raise ValueError('clip_id must be a string')
                    project = decode_project(data['project'])
                    edited = trim_clip(project, data['clip_id'], data['source_in_frame'], data['duration_frames'])
                else:
                    raise ValueError('unknown operation')
                compile_timeline(edited)
                return self.reply(200, {'status': 'edited', 'project': edited.to_dict()})
            if self.path not in {'/project', '/render'}:
                return self.reply(404, {'error': 'NOT_FOUND'})
            project = decode_project(data)
            if self.path == '/project':
                self.save_project(project)
                return self.reply(200, {'status': 'saved', 'project': project.to_dict()})
            if not lock.acquire(blocking=False):
                return self.reply(409, {'error': 'RENDER_BUSY'})
            try:
                name = 'preview-' + uuid.uuid4().hex + '.mp4'
                compiled = compile_timeline(project)
                receipt = render_project(project, root, root / name, ffmpeg=a.ffmpeg, timeout=120)
                return self.reply(200, {
                    'status': receipt['status'], 'url': '/media/' + name,
                    'wall_seconds': receipt['wall_seconds'], 'cpu_seconds': receipt['cpu_seconds'],
                    'output_bytes': receipt['output_bytes'], 'fps': compiled['fps'],
                    'duration_frames': compiled['duration_frames'],
                })
            finally:
                lock.release()
        except Exception as error:
            return self.reply(400, {'error': type(error).__name__})


server = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
print(f'P0_PREVIEW=http://127.0.0.1:{a.port}', flush=True)
server.serve_forever()
