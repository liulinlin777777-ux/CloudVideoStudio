"""Independent semantic acceptance of a real two-video/two-audio render."""
import argparse
import array
import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from cloud_video_studio.project import Project
from cloud_video_studio.media import generate_sources, make_demo_project, render_project
from cloud_video_studio.plugins import run_plugin, PluginError


def command(argv, timeout=120):
    return subprocess.run(argv, check=True, capture_output=True, timeout=timeout).stdout


def probe(executable, source):
    return json.loads(command([executable,'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(source)]))


def tone(samples, rate, start, frequency, length=0.05):
    chunk=samples[int(start*rate):int((start+length)*rate)]
    n=len(chunk)
    c=sum(x*math.cos(2*math.pi*frequency*i/rate) for i,x in enumerate(chunk))
    s=sum(x*math.sin(2*math.pi*frequency*i/rate) for i,x in enumerate(chunk))
    return 2*math.hypot(c,s)/n/32768


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',required=True)
    parser.add_argument('--ffmpeg',default='ffmpeg')
    parser.add_argument('--ffprobe',default='ffprobe')
    args=parser.parse_args();root=Path(args.output_dir).resolve();root.mkdir(parents=True,exist_ok=True)
    report={'schema':'CVS_MEDIA_ACCEPTANCE_V1','status':'FAIL','checks':{},'gpu':'NOT_USED','cost_external':0}
    def verify(name, condition):
        report['checks'][name]=bool(condition)
        if not condition:raise AssertionError(name)
    try:
        report['ffmpeg']=command([args.ffmpeg,'-version']).decode().splitlines()[0]
        report['ffprobe']=command([args.ffprobe,'-version']).decode().splitlines()[0]
        begin=time.monotonic();generate_sources(root,ffmpeg=args.ffmpeg)
        project=make_demo_project();project.save(root/'project.cvs.json');loaded=Project.load(root/'project.cvs.json')
        verify('project_roundtrip',project.to_dict()==loaded.to_dict())
        kinds=[t.kind for t in loaded.sequences[0].tracks]
        verify('two_video_two_audio_tracks',kinds.count('video')>=2 and kinds.count('audio')>=2)
        source_hashes={a.uri:hashlib.sha256((root/a.uri).read_bytes()).hexdigest() for a in loaded.assets}
        render=render_project(loaded,root,root/'render.mp4',ffmpeg=args.ffmpeg)
        report['render_resources']={k:render[k] for k in ['wall_seconds','cpu_seconds','output_bytes']}
        target=root/'render.mp4';metadata=probe(args.ffprobe,target);report['probe']=metadata
        video=next(s for s in metadata['streams'] if s['codec_type']=='video')
        audio=next(s for s in metadata['streams'] if s['codec_type']=='audio')
        verify('resolution',video['width']==640 and video['height']==360)
        verify('rational_fps',video['r_frame_rate']=='30000/1001')
        verify('decoded_frames',int(video.get('nb_read_frames',0))==120)
        verify('duration',abs(float(metadata['format']['duration'])-4.004)<0.06)
        verify('stream_start_sync',abs(float(video.get('start_time',0))-float(audio.get('start_time',0)))<0.04)
        verify('stream_end_sync',abs(float(video['duration'])-float(audio['duration']))<0.06)
        command([args.ffmpeg,'-nostdin','-v','error','-i',str(target),'-f','null','-'])
        verify('full_decode',True)
        colors={}
        def frame(n):
            rgb=command([args.ffmpeg,'-v','error','-i',str(target),'-vf',f'select=eq(n\\,{n})','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'])
            verify(f'frame_{n}_decoded',len(rgb)==640*360*3)
            def patch(x,y):
                pixels=[rgb[((yy*640+xx)*3):((yy*640+xx)*3+3)] for yy in range(y-3,y+4) for xx in range(x-3,x+4)]
                return [sum(p[c] for p in pixels)/len(pixels) for c in range(3)]
            return patch(320,180),patch(540,290)
        for n in [15,29,30,31,40,60,100]:colors[n]=frame(n)
        report['frame_rgb']={str(k):v for k,v in colors.items()}
        red=colors[15][0];middle=colors[60][0];blue=colors[100][0];green=colors[40][1]
        verify('trim_red_segment',red[0]>200 and red[2]<30)
        verify('crossfade_midpoint',middle[0]>70 and middle[2]>70)
        verify('blue_segment',blue[2]>200 and blue[0]<30)
        verify('pip_green',green[1]>80 and green[1]>2*max(green[0],green[2]))
        verify('pip_absent_before',colors[29][1][0]>200)
        for n in [15,60,100]:
            command([args.ffmpeg,'-v','error','-i',str(target),'-vf',f'select=eq(n\\,{n})','-frames:v','1',str(root/f'frame-{n:03}.png')])
        pcm=command([args.ffmpeg,'-v','error','-i',str(target),'-vn','-ac','1','-ar','48000','-f','s16le','-'])
        samples=array.array('h');samples.frombytes(pcm);rate=48000
        a440=tone(samples,rate,0.5,440);before=tone(samples,rate,0.5,880);during=tone(samples,rate,1.2,880);after=tone(samples,rate,3.3,880)
        report['audio_measurements']={'440Hz':a440,'880Hz_before':before,'880Hz_during':during,'880Hz_after':after}
        verify('both_audio_tracks_mixed',a440>0.02 and during>0.01 and before<during*0.15 and after<during*0.15)
        verify('audio_not_clipped',max(abs(x) for x in samples)<30000)
        onset=next((n/100+0.025 for n in range(80,120) if tone(samples,rate,n/100,880)>during*0.5),None)
        green_frame=next((n for n in [29,30,31,40] if colors[n][1][1]>80 and colors[n][1][1]>2*max(colors[n][1][0],colors[n][1][2])),None)
        visual_time=green_frame*1001/30000 if green_frame is not None else None
        verify('audio_visual_event_sync',onset is not None and visual_time is not None and abs(onset-visual_time)<0.08)
        report['audio']={'tone440_amplitude':a440,'tone880_before':before,'tone880_during':during,'tone880_after':after,'onset_seconds':onset,'visual_onset_seconds':visual_time}
        proxy=run_plugin('builtin.proxy',target,root/'proxy.mp4',ffmpeg=args.ffmpeg,ffprobe=args.ffprobe)
        try:
            run_plugin('builtin.proxy',target,root/'timeout-proxy.mp4',ffmpeg=args.ffmpeg,ffprobe=args.ffprobe,timeout=0.000001)
            raise AssertionError('plugin_timeout_not_enforced')
        except PluginError as error:
            verify('real_plugin_timeout',error.code=='TIMEOUT')
        invalid=root/'invalid.mp4';invalid.write_bytes(b'public synthetic invalid input')
        try:
            run_plugin('builtin.proxy',invalid,root/'invalid-proxy.mp4',ffmpeg=args.ffmpeg,ffprobe=args.ffprobe)
            raise AssertionError('invalid_media_accepted')
        except PluginError as error:
            verify('real_plugin_error',error.code=='PROCESS_ERROR')
        verify('real_plugin',proxy['plugin_id']=='builtin.proxy' and (root/'proxy.mp4').stat().st_size>1000)
        p=probe(args.ffprobe,root/'proxy.mp4');pv=next(s for s in p['streams'] if s['codec_type']=='video')
        verify('proxy_resolution',pv['width']==320 and pv['height']==180)
        command([args.ffmpeg,'-v','error','-i',str(root/'proxy.mp4'),'-f','null','-'])
        report['plugin']={k:proxy[k] for k in ['plugin_id','version','resources','wall_seconds','cpu_seconds','output_bytes']}
        verify('sources_preserved',source_hashes=={a.uri:hashlib.sha256((root/a.uri).read_bytes()).hexdigest() for a in loaded.assets})
        report['source_hashes']=source_hashes
        report['render_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        report['wall_total_seconds']=time.monotonic()-begin
        report['disk_bytes']=sum(f.stat().st_size for f in root.iterdir() if f.is_file())
        # Strip absolute FFprobe filenames; no private paths in public evidence.
        report['probe']['format']['filename']='render.mp4'
        report['status']='PASS'
    except Exception as error:
        report['first_broken_step']=type(error).__name__+':'+str(error).splitlines()[0]
        raise
    finally:
        (root/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'checks':len(report['checks']),'wall_seconds':report['wall_total_seconds']}))

if __name__=='__main__':main()
