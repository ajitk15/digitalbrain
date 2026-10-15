import array
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from PIL import Image

HERE=Path(__file__).resolve().parent
movie=HERE/'graph-generation-growing-brain.mp4'
ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
probe=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-show_format','-show_chapters','-of','json',str(movie)],text=True))
video=next(s for s in probe['streams'] if s['codec_type']=='video')
audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
assert abs(float(probe['format']['duration'])-240)<.05
assert (video['width'],video['height'])==(1920,1080)
assert video['codec_name']=='h264' and audio['codec_name']=='aac'
assert int(video['nb_frames'])==5760 and video['avg_frame_rate']=='24/1'
assert len(probe['chapters'])==9
assert any(s['codec_type']=='subtitle' for s in probe['streams'])
with wave.open(str(HERE/'narration.wav')) as wav:
    assert wav.getnframes()/wav.getframerate()==240
    samples=array.array('h',wav.readframes(wav.getnframes()))
assert max(samples)>1000 and min(samples)<-1000
result=subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(movie),'-f','null','-'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
assert result.returncode==0 and not result.stderr.strip(),result.stderr
scenes=json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
sheet=Image.new('RGB',(1920,2700),'white');offset=0
for i,s in enumerate(scenes):
    output=HERE/f'video-frame-{i:02d}.png'
    subprocess.run([ffmpeg,'-nostdin','-y','-v','error','-ss',str(offset+s['duration']*.82),'-i',str(movie),'-frames:v','1',str(output)],check=True)
    shot=Image.open(output).resize((960,540));sheet.paste(shot,((i%2)*960,(i//2)*540));offset+=s['duration']
sheet.save(HERE/'video-review.png')
# Additional encoded frames cover the initial structure reveal and both database proposals.
for name,seconds in [('structure-reveal',60),('policy-proposal',99),('claims-proposal',116),('rejected-evidence',151)]:
    subprocess.run([ffmpeg,'-nostdin','-y','-v','error','-ss',str(seconds),'-i',str(movie),'-frames:v','1',str(HERE/f'{name}.png')],check=True)
metrics=json.loads((HERE/'speech-metrics.json').read_text())
assert all(m['language']=='en-IN' and m['voice']=='Microsoft Heera' and m['tempo']<1.28 for m in metrics)
record=dict(duration_seconds=240,resolution=[1920,1080],frame_rate=24,frames=5760,chapters=9,video_codec='H.264',audio_codec='AAC',voice='Microsoft Heera',voice_locale='en-IN',voice_type='local synthesized narration',captions='Burned in, selectable track and SRT',full_file_decode='passed',audio_present='passed',visual_review='pending',sha256=hashlib.sha256(movie.read_bytes()).hexdigest(),bytes=movie.stat().st_size,scope='Actual synthetic graph functions with prepared AI proposals. Upload/conversion, embedding provider call, storage and publication are illustrated. Graphify themes use actual synthetic grouping. Not a live application screen recording.')
(HERE/'verification.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record,indent=2))
