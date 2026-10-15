"""Probe the finished media, decode it, and extract exact video frames for review."""

import array
import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path

from PIL import Image

HERE=Path(__file__).resolve().parent
ffmpeg,ffprobe=shutil.which('ffmpeg'),shutil.which('ffprobe')
movie=HERE/'graph-generation-2-minutes.mp4'
probe=json.loads(subprocess.check_output([ffprobe,'-v','error','-show_streams','-show_format',
    '-of','json',str(movie)],text=True))
video=next(s for s in probe['streams'] if s['codec_type']=='video')
audio=next(s for s in probe['streams'] if s['codec_type']=='audio')
assert abs(float(probe['format']['duration'])-120)<.05
assert (video['width'],video['height'])==(1920,1080)
assert video['codec_name']=='h264' and audio['codec_name']=='aac'
assert video['nb_frames']=='2880'
assert any(s['codec_type']=='subtitle' for s in probe['streams'])
with wave.open(str(HERE/'narration.wav')) as wav:
    assert wav.getnframes()/wav.getframerate()==120
    samples=array.array('h',wav.readframes(wav.getnframes()))
assert max(samples)>1000 and min(samples)<-1000, 'Silent or unusable narration'
decode=subprocess.run([ffmpeg,'-nostdin','-v','error','-i',str(movie),'-f','null','-'],
    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
assert decode.returncode==0 and not decode.stderr.strip(),decode.stderr
scenes=json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
sheet=Image.new('RGB',(1920,2700),'white');offset=0
for k,scene in enumerate(scenes):
    position=offset+scene['duration']*.78
    output=HERE/f'video-frame-{k:02d}.png'
    subprocess.run([ffmpeg,'-nostdin','-y','-v','error','-ss',str(position),'-i',str(movie),
                    '-frames:v','1',str(output)],check=True)
    image=Image.open(output).resize((960,540))
    sheet.paste(image,((k%2)*960,(k//2)*540))
    offset+=scene['duration']
sheet.save(HERE/'video-review.png')
delivery=json.loads((HERE/'diagram-delivery.json').read_text(encoding='utf-8-sig'))
browser=json.loads((HERE/'diagram-browser.json').read_text(encoding='utf-8-sig'))
assert delivery['ok'] and delivery['validation']['checksPassed']==9
assert browser['ok'] and browser['status']=='pass'
record={'date':'2026-10-14','duration_seconds':120,'resolution':'1920x1080',
    'frames':2880,'frame_rate':24,'video':'H.264','audio':'AAC English narration',
    'captions':'burned-in plus selectable subtitle track and SRT sidecar',
    'full_decode':'passed','audio_present':'passed',
    'video_sha256':hashlib.sha256(movie.read_bytes()).hexdigest(),
    'live_example':json.loads((HERE/'live-example.json').read_text(encoding='utf-8'))['verification'],
    'diagram':{'type':'architecture','validation':'9/9 showcase; zero errors/warnings',
        'specification_sha256':delivery['specification']['sha256'],
        'artifact_sha256':delivery['artifact']['sha256'],'browser_evidence':'passed',
        'visual_review':'pending','correction_rounds':1},'video_visual_review':'pending'}
(HERE/'verification.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print('Verified 120 seconds, 1080p, 2880 frames, narration, captions and clean full-file decode. Frames extracted for visual review.')
