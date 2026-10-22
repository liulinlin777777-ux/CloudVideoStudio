"""Independent public-synthetic P1 media acceptance, reusing P0 probe helpers."""
import argparse, array, hashlib, json, math, resource, time
from pathlib import Path
from media_smoke import command, probe, tone
from cloud_video_studio.media import make_demo_project, render_project
from cloud_video_studio.timeline_edits import move_clip, trim_clip

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--output-dir',required=True);args=ap.parse_args()
 root=Path(args.output_dir);root.mkdir(parents=True,exist_ok=False)
 report={'schema':'CVS_P1_SLICE2_MEDIA_V1','status':'FAIL','cases':[],'paidResources':0};begin=time.monotonic()
 try:
  for num,den in [(30000,1001),(25,1)]:
   for mode in ['gap','overlap']:
    directory=root/f'{num}-{den}-{mode}';directory.mkdir();fps=num/den
    for color in ['red','blue','green']:
     command(['ffmpeg','-nostdin','-n','-v','error','-f','lavfi','-i',f'color=c={color}:s=640x360:r={num}/{den}', '-frames:v','150','-an','-c:v','libx264','-threads','2','-pix_fmt','yuv420p',str(directory/f'{color}.mp4')],30)
    for freq in [440,880]:
     command(['ffmpeg','-nostdin','-n','-v','error','-f','lavfi','-i',f'sine=frequency={freq}:sample_rate=48000:duration={150/fps}','-c:a','pcm_s16le',str(directory/f'tone{freq}.wav')],30)
    p=make_demo_project();s=p.sequences[0];s.fps_num=num;s.fps_den=den
    p=move_clip(p,'red-clip',10)
    if mode=='gap':
     p=trim_clip(p,'red-clip',20,45);p=move_clip(p,'blue-clip',70);p=trim_clip(p,'blue-clip',15,60);p.sequences[0].tracks[0].clips[1].effects=[]
    else:p=move_clip(p,'blue-clip',55)
    p=move_clip(p,'green-clip',35);p=trim_clip(p,'green-clip',30,40)
    p=move_clip(p,'tone440-clip',10);p=trim_clip(p,'tone440-clip',15,100)
    p=move_clip(p,'tone880-clip',35);p=trim_clip(p,'tone880-clip',15,40)
    p.save(directory/'project.cvs.json');before={a.uri:hashlib.sha256((directory/a.uri).read_bytes()).hexdigest() for a in p.assets}
    usage=render_project(p,directory,'render.mp4');target=directory/'render.mp4';meta=probe('ffprobe',target)
    v=next(s for s in meta['streams'] if s['codec_type']=='video');a=next(s for s in meta['streams'] if s['codec_type']=='audio')
    assert v['r_frame_rate']==f'{num}/{den}',v['r_frame_rate']
    assert int(v['nb_read_frames'])==130,v['nb_read_frames']
    assert abs(float(meta['format']['duration'])-130/fps)<0.08
    assert abs(float(v['duration'])-float(a['duration']))<0.08
    command(['ffmpeg','-nostdin','-v','error','-i',str(target),'-f','null','-'],30)
    def frame(n,x=320,y=180):
     rgb=command(['ffmpeg','-v','error','-i',str(target),'-vf',f'select=eq(n\\,{n})','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','-'],30)
     assert len(rgb)==640*360*3
     pixels=[rgb[(yy*640+xx)*3:(yy*640+xx)*3+3] for yy in range(y-2,y+3) for xx in range(x-2,x+3)]
     return [sum(px[c] for px in pixels)/len(pixels) for c in range(3)]
    black=frame(5);red=frame(25);blue=frame(110);green=frame(40,540,290)
    assert max(black)<20,black
    assert red[0]>200 and red[2]<30,red
    assert blue[2]>200 and blue[0]<30,blue
    assert green[1]>80 and green[1]>2*max(green[0],green[2]),green
    assert frame(34,540,290)[0]>200
    assert frame(76,540,290)[1]<50
    middle=frame(60 if mode=='gap' else 70)
    if mode=='gap':assert max(middle)<20,middle
    else:assert middle[0]>70 and middle[2]>70,middle
    pcm=command(['ffmpeg','-v','error','-i',str(target),'-vn','-ac','1','-ar','48000','-f','s16le','-'],30);samples=array.array('h');samples.frombytes(pcm)
    early=tone(samples,48000,25/fps,880);during=tone(samples,48000,45/fps,880);late=tone(samples,48000,90/fps,880)
    assert during>0.01 and early<during*0.15 and late<during*0.15,(early,during,late)
    assert tone(samples,48000,25/fps,440)>0.02
    onset=next((n/100+0.025 for n in range(int(30/fps*100),int(40/fps*100)) if tone(samples,48000,n/100,880)>during*0.5),None)
    assert onset is not None and abs(onset-35/fps)<0.08,onset
    assert before=={a.uri:hashlib.sha256((directory/a.uri).read_bytes()).hexdigest() for a in p.assets}
    case={'rate':f'{num}/{den}','mode':mode,'status':'PASS','frames':130,'fullDecode':True,'audioOnset':onset,'expectedOnset':35/fps,'rgb':{'initial':black,'red':red,'middle':middle,'blue':blue,'pip':green},'resources':{k:usage[k] for k in ['wall_seconds','cpu_seconds','output_bytes']},'sourceHashes':before}
    report['cases'].append(case)
  report.update(status='PASS',wallSeconds=time.monotonic()-begin,peakChildrenRss=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,diskBytes=sum(p.stat().st_size for p in root.rglob('*') if p.is_file()))
 finally:(root/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({'status':report['status'],'cases':len(report['cases'])}))
if __name__=='__main__':main()
