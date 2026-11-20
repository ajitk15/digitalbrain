"""Replace the final scene, preserving original narration, captions and chapters."""
import os
import subprocess
from pathlib import Path
import build_video as b

p=Path(__file__).resolve().parent
command=[b.FFMPEG,'-nostdin','-y','-v','error','-i','graph-generation-4-minutes.mp4','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r','12','-i','pipe:0','-filter_complex','[0:v]trim=duration=218,setpts=PTS-STARTPTS[first];[1:v]fps=24,setpts=PTS+218/TB,ass=captions.ass,setpts=PTS-STARTPTS[last];[first][last]concat=n=2:v=1:a=0[v]','-map','[v]','-map','0:a','-map','0:s','-map_metadata','0','-map_chapters','0','-t','240','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','copy','-c:s','copy','-movflags','+faststart','repaired.mp4']
with (p/'repair.log').open('w') as log:
    process=subprocess.Popen(command,cwd=p,stdin=subprocess.PIPE,stderr=log)
    try:
        for n in range(22*12):process.stdin.write(b.render(8,n/12).tobytes())
        process.stdin.close();assert process.wait()==0
    except BaseException:
        process.kill();process.wait();raise
os.replace(p/'repaired.mp4',p/'graph-generation-4-minutes.mp4')
print('Closing scene replaced; ready for full verification.')
