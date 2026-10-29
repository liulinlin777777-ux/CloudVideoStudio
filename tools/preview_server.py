"""Loopback-only P0/P1 synthetic preview; no deployment, credentials or uploads."""
import argparse
import json
import os
import re
import tempfile
import threading
import uuid
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cloud_video_studio.assets import AssetStore
from cloud_video_studio.project import Project
from cloud_video_studio.creative_brief import (
    MAX_ARTIFACT_BYTES, BriefRevisionConflict, artifact_path, artifact_sha256, read_brief, save_brief,
)
from cloud_video_studio.timeline_edits import (
    add_clip, add_track, delete_clip, move_clip, move_clip_to_track, reorder_clip, split_clip, trim_clip,
)
from cloud_video_studio.media import compile_timeline, make_demo_project, render_project

p = argparse.ArgumentParser()
p.add_argument('--media-root', required=True)
p.add_argument('--port', type=int, default=8774)
p.add_argument('--ffmpeg', default='ffmpeg')
a = p.parse_args()
root = Path(a.media_root).resolve()
html = Path(__file__).resolve().parents[1] / 'web/index.html'
lock = threading.Lock()
asset_lock = threading.RLock()
MAX_BODY = 65536
MAX_BRIEF_BODY = MAX_ARTIFACT_BYTES + 4096
MAX_UPLOAD = 512 * 1024 * 1024
upload_slots = threading.BoundedSemaphore(2)
asset_store = AssetStore(root / 'assets', max_file_bytes=MAX_UPLOAD, max_total_bytes=2 * 1024 * 1024 * 1024)


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

    def browser_origin(self):
        return self.headers.get('Origin') in {f'http://127.0.0.1:{a.port}', f'http://localhost:{a.port}'}

    def read_upload(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError):
            raise ValueError('invalid content length')
        if not 0 < length <= MAX_UPLOAD:
            raise OverflowError('upload size')
        raw_name = unquote(self.headers.get('X-File-Name', ''))
        # Only a filename is accepted. Never interpret a browser supplied path.
        if not raw_name or raw_name in {'.', '..'} or '/' in raw_name or '\\' in raw_name or Path(raw_name).name != raw_name:
            raise ValueError('invalid upload filename')
        if any(ord(char) < 32 or ord(char) == 127 for char in raw_name):
            raise ValueError('invalid upload filename')
        suffix = Path(raw_name).suffix.lower()
        fd, temporary = tempfile.mkstemp(prefix='.asset-upload-', suffix=suffix, dir=root)
        count = 0
        try:
            with os.fdopen(fd, 'wb') as stream:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError('incomplete upload body')
                    stream.write(chunk)
                    count += len(chunk)
                    remaining -= len(chunk)
            return Path(temporary), raw_name
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

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
            if self.path == '/creative-brief':
                project = current()
                brief = read_brief(artifact_path(root, project.id), project.id)
                return self.reply(200, {'brief': brief, 'sha256': artifact_sha256(brief)})
            if self.path == '/assets':
                with asset_lock:
                    return self.reply(200, {'schema_version': 2, 'assets': asset_store.list_assets()})
            if self.path.startswith('/assets/') and self.path.endswith('/thumbnail'):
                asset_id = self.path[len('/assets/'):-len('/thumbnail')].strip('/')
                if len(asset_id) != 32 or any(ch not in '0123456789abcdef' for ch in asset_id):
                    return self.reply(404, {'error': 'NOT_FOUND'})
                with asset_lock:
                    derivative = asset_store.create_thumbnail(asset_id)
                    path = asset_store.root / derivative['path']
                    return self.reply(200, path.read_bytes(), 'image/jpeg')
            if self.path.startswith('/assets/') and self.path.endswith('/proxy'):
                asset_id = self.path[len('/assets/'):-len('/proxy')].strip('/')
                if len(asset_id) != 32 or any(ch not in '0123456789abcdef' for ch in asset_id):
                    return self.reply(404, {'error': 'NOT_FOUND'})
                with asset_lock:
                    derivative = asset_store.create_proxy(asset_id, ffmpeg=a.ffmpeg)
                    path = asset_store.root / derivative['path']
                    return self.reply(200, path.read_bytes(), 'video/mp4')
            if self.path.startswith('/media/') and '/' not in self.path[7:]:
                return self.media(self.path[7:])
            return self.reply(404, {'error': 'NOT_FOUND'})
        except Exception as error:
            return self.reply(400, {'error': type(error).__name__})

    def read_json(self, max_bytes=MAX_BODY):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError):
            raise ValueError('invalid content length')
        if not 0 < length <= max_bytes:
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
        relink_match = re.fullmatch(r'/assets/([0-9a-f]{32})/relink', self.path)
        if self.path == '/assets/import' or relink_match:
            if not self.browser_origin():
                return self.reply(403, {'error': 'ORIGIN_DENIED'})
            if not upload_slots.acquire(blocking=False):
                return self.reply(503, {'error': 'UPLOAD_BUSY'})
            temporary = None
            try:
                temporary, filename = self.read_upload()
                suffix = Path(filename).suffix.lower()
                if suffix not in {'.mp4', '.mov', '.mp3', '.wav', '.jpg', '.jpeg', '.png', '.srt'}:
                    return self.reply(415, {'error': 'UNSUPPORTED_FILE_TYPE'})
                with asset_lock:
                    asset = (asset_store.relink(relink_match.group(1), temporary, filename=filename)
                             if relink_match else asset_store.import_file(temporary, filename=filename))
                return self.reply(201, asset)
            except OverflowError:
                return self.reply(413, {'error': 'UPLOAD_SIZE'})
            except Exception as error:
                return self.reply(400, {'error': str(error)[:240]})
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                upload_slots.release()
        try:
            data = self.read_json(MAX_BRIEF_BODY if self.path == '/creative-brief' else MAX_BODY)
        except OverflowError:
            return self.reply(413, {'error': 'BODY_SIZE'})
        except Exception as error:
            return self.reply(400, {'error': type(error).__name__})
        try:
            if self.path == '/creative-brief':
                if not self.browser_origin():
                    return self.reply(403, {'error': 'ORIGIN_DENIED'})
                if not isinstance(data, dict) or set(data) != {'action', 'brief', 'expected_sha256'}:
                    raise ValueError('invalid creative brief request')
                if data['action'] not in {'save', 'approve'}:
                    raise ValueError('unknown creative brief action')
                project = current()
                try:
                    brief = save_brief(
                        artifact_path(root, project.id), data['brief'], project.id,
                        approve=data['action'] == 'approve', expected_sha256=data['expected_sha256'],
                    )
                except BriefRevisionConflict as error:
                    return self.reply(409, {
                        'error': 'BRIEF_REVISION_CONFLICT', 'current_sha256': error.current_sha256,
                    })
                return self.reply(200, {'status': 'approved' if data['action'] == 'approve' else 'saved',
                                        'brief': brief, 'sha256': artifact_sha256(brief)})
            if self.path == '/edit':
                if not isinstance(data, dict):
                    raise ValueError('request must be an object')
                operation = data.get('operation')
                if operation == 'move':
                    allowed = {'project', 'operation', 'clip_id', 'start_frame', 'track_id', 'snap_frames'}
                    if not {'project', 'operation', 'clip_id', 'start_frame'} <= set(data) or set(data) - allowed:
                        raise ValueError('invalid move fields')
                    if not isinstance(data['clip_id'], str):
                        raise ValueError('clip_id must be a string')
                    project = decode_project(data['project'])
                    if 'track_id' in data or 'snap_frames' in data:
                        track_id = data.get('track_id')
                        if track_id is None:
                            track_id = next(
                                track.id for sequence in project.sequences for track in sequence.tracks
                                if any(clip.id == data['clip_id'] for clip in track.clips)
                            )
                        if not isinstance(track_id, str):
                            raise ValueError('track_id must be a string when supplied')
                        edited = move_clip_to_track(
                            project, data['clip_id'], data['start_frame'], track_id,
                            snap_frames=data.get('snap_frames', 0),
                        )
                    else:
                        edited = move_clip(project, data['clip_id'], data['start_frame'])
                elif operation == 'trim':
                    if set(data) != {'project', 'operation', 'clip_id', 'source_in_frame', 'duration_frames'}:
                        raise ValueError('invalid trim fields')
                    if not isinstance(data['clip_id'], str):
                        raise ValueError('clip_id must be a string')
                    project = decode_project(data['project'])
                    edited = trim_clip(project, data['clip_id'], data['source_in_frame'], data['duration_frames'])
                elif operation == 'split':
                    if set(data) not in (
                        {'project', 'operation', 'clip_id', 'at_frame'},
                        {'project', 'operation', 'clip_id', 'at_frame', 'new_clip_id'},
                    ) or not isinstance(data.get('clip_id'), str):
                        raise ValueError('invalid split fields')
                    project = decode_project(data['project'])
                    edited = split_clip(project, data['clip_id'], data['at_frame'], data.get('new_clip_id'))
                elif operation == 'delete':
                    if (set(data) != {'project', 'operation', 'clip_id', 'p2_version'}
                            or type(data.get('p2_version')) is not int or data['p2_version'] != 2
                            or not isinstance(data.get('clip_id'), str)):
                        raise ValueError('invalid delete fields')
                    project = decode_project(data['project'])
                    edited = delete_clip(project, data['clip_id'])
                elif operation == 'add':
                    if set(data) != {'project', 'operation', 'asset_id', 'track_id', 'start_frame', 'clip_id'}:
                        raise ValueError('invalid add fields')
                    if not all(isinstance(data[name], str) for name in ('asset_id', 'track_id', 'clip_id')):
                        raise ValueError('asset_id, track_id and clip_id must be strings')
                    project = decode_project(data['project'])
                    edited = add_clip(
                        project, data['asset_id'], data['track_id'], data['start_frame'], data['clip_id'],
                    )
                elif operation == 'add_track':
                    if set(data) != {'project', 'operation', 'kind', 'track_id'}:
                        raise ValueError('invalid add_track fields')
                    if not isinstance(data['kind'], str) or not isinstance(data['track_id'], str):
                        raise ValueError('kind and track_id must be strings')
                    project = decode_project(data['project'])
                    edited = add_track(project, data['kind'], data['track_id'])
                elif operation == 'reorder':
                    if set(data) not in (
                        {'project', 'operation', 'clip_id'},
                        {'project', 'operation', 'clip_id', 'before_clip_id'},
                    ) or not isinstance(data.get('clip_id'), str):
                        raise ValueError('invalid reorder fields')
                    project = decode_project(data['project'])
                    before = data.get('before_clip_id')
                    if before is not None and not isinstance(before, str):
                        raise ValueError('before_clip_id must be a string')
                    edited = reorder_clip(project, data['clip_id'], before)
                else:
                    raise ValueError('unknown operation')
                compile_timeline(edited)
                response = {'status': 'edited', 'project': edited.to_dict()}
                if operation == 'add':
                    response['clip_id'] = data['clip_id']
                if operation == 'add_track':
                    response['track_id'] = data['track_id']
                return self.reply(200, response)
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
            if self.path == '/creative-brief' and isinstance(error, ValueError):
                return self.reply(400, {'error': str(error)[:240]})
            return self.reply(400, {'error': type(error).__name__})


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args, max_handlers=8, **kwargs):
        self.handler_slots = threading.BoundedSemaphore(max_handlers)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.handler_slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.handler_slots.release()


server = BoundedThreadingHTTPServer(('127.0.0.1', a.port), Handler)
print(f'P0_PREVIEW=http://127.0.0.1:{a.port}', flush=True)
server.serve_forever()
