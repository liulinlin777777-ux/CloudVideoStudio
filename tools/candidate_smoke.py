"""Check actual OTIO interchange and MLT candidate operation; no GPU."""
import argparse,json,os,subprocess
from pathlib import Path
import opentimelineio as otio

p=argparse.ArgumentParser();p.add_argument('--output-dir',required=True);p.add_argument('--melt',default='melt');p.add_argument('--ffprobe',default='ffprobe');a=p.parse_args()
root=Path(a.output_dir).resolve();root.mkdir(parents=True,exist_ok=True)
rate=30000/1001
clip=otio.schema.Clip(name='synthetic',media_reference=otio.schema.ExternalReference(target_url='synthetic:red'),source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(15,rate),otio.opentime.RationalTime(75,rate)))
timeline=otio.schema.Timeline(name='P0',tracks=[otio.schema.Track(name='V1',children=[clip])])
otio.adapters.write_to_file(timeline,str(root/'candidate.otio'));loaded=otio.adapters.read_from_file(str(root/'candidate.otio'))
assert loaded.tracks[0][0].duration().value==75 and abs(loaded.tracks[0][0].duration().rate-rate)<1e-9
out=root/'mlt-candidate.mp4'
argv=[a.melt,'-profile','atsc_720p_2997','color:red','in=0','out=29','color:blue','in=0','out=29','-consumer',f'avformat:{out}','vcodec=libx264','an=1','real_time=-2','threads=2']
run=subprocess.run(argv,capture_output=True,text=True,timeout=90,env={**os.environ,'QT_QPA_PLATFORM':'offscreen'})
if run.returncode:raise RuntimeError('MLT_RENDER_FAILED:'+run.stderr[-1000:])
probe=json.loads(subprocess.check_output([a.ffprobe,'-v','error','-count_frames','-show_streams','-show_format','-of','json',str(out)]))
v=next(s for s in probe['streams'] if s['codec_type']=='video');assert int(v['nb_read_frames'])==60
version=subprocess.run([a.melt,'--version'],capture_output=True,text=True,timeout=15)
result={'status':'PASS','otio_version':otio.__version__,'otio_frames':75,'otio_rate':rate,'mlt_version':(version.stdout+version.stderr).strip(),'mlt_decoded_frames':60,'mlt_fps':v['r_frame_rate']}
(root/'candidates.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
