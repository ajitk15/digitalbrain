import importlib.util,json,textwrap,subprocess,shutil
from pathlib import Path
HERE=Path(__file__).resolve().parent
SCENES=json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
FFMPEG=shutil.which('ffmpeg');FFPROBE=shutil.which('ffprobe')
def run(args,**kw):subprocess.run(args,check=True,**kw)
def duration(path):return float(subprocess.check_output([FFPROBE,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(path)]))
def clock(sec,ass=False):
    unit=100 if ass else 1000;n=round(sec*unit);h,n=divmod(n,3600*unit);m,n=divmod(n,60*unit);s,f=divmod(n,unit)
    return f'{h}:{m:02d}:{s:02d}.{f:02d}' if ass else f'{h:02d}:{m:02d}:{s:02d},{f:03d}'

def main():
    subtitle=[];ass=[];metrics=[];offset=0
    for i,s in enumerate(SCENES):
        raw=HERE/f'narration-{i:02d}.wav';seconds=duration(raw)
        tempo=max(1,seconds/(s['duration']-.7));assert tempo<1.28,(i,seconds,tempo)
        run([FFMPEG,'-nostdin','-y','-v','error','-i',str(raw),'-af',f'adelay=200,atempo={tempo},apad,atrim=0:{s["duration"]}','-ar','48000','-ac','1',str(HERE/f'audio-{i:02d}.wav')])
        words=s['narration'].split();spoken=seconds/tempo;used=0
        for k in range(0,len(words),10):
            chunk=words[k:k+10];length=spoken*len(chunk)/len(words);start=offset+.2+used;end=start+length;caption=' '.join(chunk)
            subtitle.append(f'{len(subtitle)+1}\n{clock(start)} --> {clock(end)}\n{caption}\n')
            ass.append(f'Dialogue: 0,{clock(start,True)},{clock(end,True)},Default,,0,0,0,,'+r'\N'.join(textwrap.wrap(caption,75)))
            used+=length
        metrics.append(dict(scene=i+1,voice='Microsoft Heera',language='en-IN',raw_seconds=seconds,tempo=tempo,chapter_seconds=s['duration']))
        offset+=s['duration']
    (HERE/'captions.srt').write_text('\n'.join(subtitle),encoding='utf-8')
    header='''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Segoe UI,25,&H00FFFFFF,&H00FFFFFF,&H004D3217,&H004D3217,0,0,0,0,100,100,0,0,1,0,0,2,50,50,16,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    (HERE/'captions.ass').write_text(header+'\n'.join(ass),encoding='utf-8')
    (HERE/'speech-metrics.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
    (HERE/'audio.txt').write_text('\n'.join(f"file '{(HERE/f'audio-{i:02d}.wav').as_posix()}'" for i in range(9)),encoding='utf-8')
    run([FFMPEG,'-nostdin','-y','-v','error','-f','concat','-safe','0','-i',str(HERE/'audio.txt'),'-c:a','pcm_s16le',str(HERE/'narration.wav')])
    metadata=[';FFMETADATA1','title=Digital Brain - Graph generation','comment=Indian English narrated teaching demo. Prepared AI proposals; real graph functions.'];offset=0
    for s in SCENES:
        metadata+=['[CHAPTER]','TIMEBASE=1/1000',f'START={offset*1000}',f'END={(offset+s["duration"])*1000}',f'title={s["title"]}'];offset+=s['duration']
    (HERE/'chapters.ffmeta').write_text('\n'.join(metadata),encoding='utf-8')

if __name__=='__main__':main()
