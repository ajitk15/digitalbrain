"""Render an evidence-grounded, 120-second narrated student walkthrough."""

import json
import math
import shutil
import subprocess
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
SCENES = json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
LIVE = json.loads((HERE/'live-example.json').read_text(encoding='utf-8'))
FFMPEG, FFPROBE = shutil.which('ffmpeg'), shutil.which('ffprobe')
assert FFMPEG and FFPROBE
W, H = 1920, 1080
INK, MUTED, BLUE, TEAL = '#19314c', '#60718a', '#315cd8', '#117f70'
BG, LINE, ORANGE, RED = '#f4f7fc', '#dce4f0', '#b56c10', '#b53042'
PHASES = ['Read', 'Find', 'Connect', 'Check', 'Save', 'Review', 'Publish']


def font(size, bold=False, mono=False):
    name = 'consola.ttf' if mono else 'segoeuib.ttf' if bold else 'segoeui.ttf'
    return ImageFont.truetype(str(Path('C:/Windows/Fonts')/name), size)


def text(draw, xy, value, size=34, fill=INK, bold=False, width=None, mono=False):
    f = font(size, bold, mono)
    if width:
        lines = []
        for paragraph in value.split('\n'):
            line = ''
            for word in paragraph.split():
                trial = f'{line} {word}'.strip()
                if draw.textlength(trial, font=f) > width and line:
                    lines.append(line); line = word
                else:
                    line = trial
            lines.append(line)
        value = '\n'.join(lines)
    draw.multiline_text(xy, value, font=f, fill=fill, spacing=int(size*.23))


def box(d, rect, title, detail='', fill='white', accent=BLUE):
    x,y,x2,y2 = rect
    d.rounded_rectangle(rect, radius=24, fill=fill, outline=LINE, width=2)
    d.rounded_rectangle((x,y,x+8,y2), radius=4, fill=accent)
    text(d,(x+28,y+23),title,32,accent,True,width=x2-x-55)
    if detail: text(d,(x+28,y+80),detail,31,width=x2-x-55)


def arrow(d, a, b, color=BLUE, width=6):
    d.line((a,b),fill=color,width=width)
    theta=math.atan2(b[1]-a[1],b[0]-a[0])
    head=[b,(b[0]-23*math.cos(theta-.45),b[1]-23*math.sin(theta-.45)),
          (b[0]-23*math.cos(theta+.45),b[1]-23*math.sin(theta+.45))]
    d.polygon(head,fill=color)


def node(d, center, label, kind, color=BLUE, radius=110):
    x,y=center
    d.ellipse((x-radius,y-radius,x+radius,y+radius),fill='white',outline=color,width=5)
    lines=label.split('\n')
    for index,line in enumerate(lines):
        f=font(36,True)
        tw=d.textlength(line,font=f)
        d.text((x-tw/2,y-35+(index-(len(lines)-1)/2)*43),line,font=f,fill=INK)
    f=font(23)
    tw=d.textlength(kind,font=f)
    d.text((x-tw/2,y+radius*.42),kind,font=f,fill=color)


def pill(d,rect,value,color=BLUE,fill='#eaf0ff'):
    d.rounded_rectangle(rect,radius=16,fill=fill)
    text(d,(rect[0]+20,rect[1]+12),value,28,color,True)


def frame(index, reveal):
    scene=SCENES[index]
    im=Image.new('RGB',(W,H),BG); d=ImageDraw.Draw(im)
    text(d,(80,28),'DIGITAL BRAIN  /  STUDENT WALKTHROUGH',27,BLUE,True)
    text(d,(80,82),scene['title'],58,INK,True)
    text(d,(1740,45),f'{index+1:02d} / 10',26,MUTED)
    for k,label in enumerate(PHASES):
        x=80+k*252
        phase=5 if scene['visual']=='draft' and reveal>=1 else scene['stage']
        selected=k==phase
        d.rounded_rectangle((x,179,x+236,240),radius=18,
            fill=BLUE if selected else '#e6ecf5')
        text(d,(x+24,188),f'{k+1}  {label}',31,'white' if selected else MUTED,True)
    # A dedicated caption area keeps spoken text out of the diagrams.
    d.rectangle((0,925,W,H),fill='#142a45')
    text(d,(80,884),'One example · real builder + validator · controlled AI proposals',24,MUTED)
    view=scene['visual']
    if view=='intro':
        text(d,(190,313),'A short note becomes useful connections.',49,INK,True)
        box(d,(170,445,650,740),'NOTE','The library app uses a book list.',accent=BLUE)
        if reveal>=1: arrow(d,(675,590),(830,590))
        node(d,(1025,590),'Library\napp','ITEM',TEAL)
        if reveal>=2:
            arrow(d,(1140,590),(1430,590),TEAL)
            text(d,(1238,525),'uses',38,TEAL,True)
            node(d,(1555,590),'Book\nlist','ITEM',TEAL)
    elif view=='source':
        box(d,(100,300,920,798),'NOTE 1 · Library note',
            'Book list\n\nThe library app uses a book list.',accent=BLUE)
        if reveal>=1:
            box(d,(1000,300,1820,798),'NOTE 2 · Help guide',
                'Help\n\nThe help guide explains the library app.',accent=TEAL)
        if reveal>=2:
            pill(d,(135,668,880,735),'Written evidence, not an invented fact',TEAL,'#e6f5ef')
            pill(d,(1035,668,1780,735),'Same app mentioned in both sources',TEAL,'#e6f5ef')
    elif view=='structure':
        box(d,(120,320,770,525),'DOCUMENT NODE','Library note')
        box(d,(1150,320,1800,525),'SECTION NODE','Book list',accent=TEAL)
        if reveal>=1:
            arrow(d,(795,425),(1120,425))
            text(d,(830,361),'contains section',30,BLUE,True)
        box(d,(120,605,1800,790),'WHAT THE BUILDER KNOWS',
            'It found a heading inside a document.\nIt has not yet inferred that the app uses the list.',accent=ORANGE)
    elif view=='proposal':
        node(d,(445,470),'Library\napp','SYSTEM ITEM',TEAL)
        node(d,(1450,470),'Book\nlist','DATA STORE',TEAL)
        if reveal>=1:
            arrow(d,(570,470),(1320,470),ORANGE)
            pill(d,(825,376,1100,440),'uses · proposed',ORANGE,'#fff0da')
        if reveal>=2:
            box(d,(170,650,1745,810),'PROPOSAL = ITEMS + CONNECTION + EVIDENCE',
                'Subject: library app   ·   Link: uses   ·   Object: book list\n'
                'Source: Library note   ·   Quote: The library app uses a book list.',accent=ORANGE)
    elif view=='validation':
        checks=[('Quote exists in source','PASS',TEAL),('Named items appear in quote','PASS',TEAL),
                ('Types and link are allowed','PASS',TEAL)]
        for k,(label,state,color) in enumerate(checks):
            y=300+k*135
            box(d,(120,y,1240,y+108),label,accent=color)
            if reveal>=k: pill(d,(1020,y+20,1200,y+82),state,color,'#e5f5ed')
        if reveal>=1:
            box(d,(1330,300,1810,790),'REJECTED EXAMPLE',
                'The library app uses a toy list.\n\nReason:\nquote not in source',accent=RED,fill='#fff5f6')
        text(d,(140,752),'Evidence checks filter errors; human review still matters.',30,MUTED)
    elif view=='merge':
        box(d,(130,300,720,510),'NOTE 1','library app')
        box(d,(130,615,720,825),'NOTE 2','library app',accent=TEAL)
        node(d,(1040,560),'Library\napp','ONE SHARED ITEM',TEAL,radius=150)
        if reveal>=1:
            arrow(d,(745,420),(885,490),TEAL); arrow(d,(745,720),(885,630),TEAL)
        if reveal>=2:
            box(d,(1310,310,1810,815),'KEEP WITH EACH LINK',
                'Source identifier\n\nExact quote\n\nContent fingerprint',accent=BLUE)
    elif view=='live':
        node(d,(620,515),'Library\napp','SHARED ITEM',TEAL)
        node(d,(1400,515),'Book\nlist','ITEM',TEAL)
        if reveal>=1:
            arrow(d,(748,515),(1267,515),TEAL)
            pill(d,(930,428,1095,490),'uses',TEAL,'#e6f5ef')
        if reveal>=2:
            node(d,(310,745),'Help\nguide','RUNBOOK',BLUE,radius=88)
            arrow(d,(380,677),(523,603),BLUE)
            text(d,(357,608),'explains',30,BLUE,True)
        v=LIVE['verification']
        box(d,(1110,683,1795,855),'ACTUAL RUN: CHECKS PASSED',
            f"{v['semantic_links']} meaning links · {v['shared_library_app_nodes']} shared app · invented quote rejected",accent=TEAL)
        text(d,(105,290),'Builder output, not a fabricated model response',32,TEAL,True)
        text(d,(105,838),'Source words shown; saved links use standardized labels.',23,MUTED,width=950)
    elif view=='draft':
        box(d,(120,320,710,798),'DRAFT VERSION 1',
            'Nodes + relationships\n\nQuotes + source digests\n\nDjango graph JSON')
        if reveal>=1:
            arrow(d,(735,550),(850,550))
            box(d,(880,320,1810,520),'REVIEW QUALITY',
                'Coverage · missing links · isolated items · evidence',accent=TEAL)
        if reveal>=2:
            box(d,(880,587,1810,798),'GRAPHIFY: ORGANIZE THE GRAPH',
                'NetworkX + Leiden clustering\nGroups existing items; creates no source facts.',accent=ORANGE)
    elif view=='publish':
        box(d,(125,350,600,650),'REVIEWED DRAFT','A person checks it first.',accent=ORANGE)
        if reveal>=1:
            arrow(d,(625,500),(800,500),TEAL)
            box(d,(830,350,1300,650),'PUBLISHED VERSION','Available for graph answers.',accent=TEAL)
        if reveal>=2:
            box(d,(1380,350,1810,810),'ACCESS + EVIDENCE',
                'Allowed viewer\n\nCurrent source\n\nMatching quote\n\nRestricted sources excluded',accent=BLUE)
        text(d,(135,740),'Publication is explained here; no production graph is published.',29,MUTED)
    elif view=='summary':
        tech=['MarkItDown','Custom Python','OpenAI / Claude','Quote + ontology','Django JSON','Graphify + people','Human publish']
        for k,label in enumerate(PHASES):
            row,col=(0,k) if k<4 else (1,k-4)
            x=100+col*440; y=310+row*267
            color=TEAL if k<=reveal*3 else BLUE
            box(d,(x,y,x+400,y+195),f'{k+1}. {label}',tech[k],accent=color)
            if col>0: arrow(d,(x-28,y+100),(x-5,y+100),color,width=4)
        box(d,(1420,577,1820,772),'THE RESULT','Useful connections\n+ traceable evidence',accent=TEAL,fill='#eaf6f0')
    return im


def run(command):
    subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)


def duration(path):
    return float(subprocess.check_output([FFPROBE,'-v','error','-show_entries',
        'format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],text=True))


def clock(seconds, ass=False):
    hours=int(seconds//3600);minutes=int(seconds%3600//60);sec=seconds%60
    return f'{hours}:{minutes:02d}:{sec:05.2f}' if ass else f'{hours:02d}:{minutes:02d}:{sec:06.3f}'.replace('.',',')


def main():
    assert sum(scene['duration'] for scene in SCENES)==120
    frames=HERE/'frames';frames.mkdir(exist_ok=True)
    frame_list=[];srt=[];ass=[];speech_metrics=[];offset=0
    transcript=['# Graph generation in two minutes','',
        'Synthetic sources; actual builder, validator and Graphify run. AI proposals are controlled.', '']
    for index,scene in enumerate(SCENES):
        for reveal in range(3):
            image_path=frames/f'{index:02d}-{reveal}.png'
            frame(index,reveal).save(image_path)
            frame_list.extend([f"file '{image_path.as_posix()}'",f"duration {scene['duration']/3:.6f}"])
        raw=HERE/f'narration-{index:02d}.wav'; actual=duration(raw)
        speed=max(1,actual/(scene['duration']-.6))
        assert speed<1.35, f'Narration too fast in scene {index}: {speed}'
        run([FFMPEG,'-nostdin','-y','-v','error','-i',str(raw),'-af',
             f'adelay=200,atempo={speed:.6f},apad,atrim=0:{scene["duration"]}',
             '-ar','48000','-ac','1',str(HERE/f'audio-{index:02d}.wav')])
        speech_metrics.append({'scene':index+1,'raw_seconds':actual,'tempo':round(speed,4),
                               'chapter_seconds':scene['duration']})
        words=scene['narration'].split(); chunks=[words[k:k+14] for k in range(0,len(words),14)]
        spoken=min(actual/speed,scene['duration']-.4);start=offset+.2
        used=0
        for chunk in chunks:
            length=spoken*len(chunk)/len(words)
            caption=' '.join(chunk)
            srt.append(f'{len(srt)+1}\n{clock(start+used)} --> {clock(start+used+length)}\n{caption}\n')
            wrapped='\\N'.join(textwrap.wrap(caption,width=75))
            ass.append(f'Dialogue: 0,{clock(start+used,True)},{clock(start+used+length,True)},Default,,0,0,0,,{wrapped}')
            used+=length
        transcript.extend([f"## {int(offset)//60:02d}:{int(offset)%60:02d} · {scene['title']}",
                           scene['narration'],''])
        offset+=scene['duration']
    frame_list.append(f"file '{image_path.as_posix()}'")
    (HERE/'frames.txt').write_text('\n'.join(frame_list),encoding='utf-8')
    (HERE/'captions.srt').write_text('\n'.join(srt),encoding='utf-8')
    header='''[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Segoe UI,36,&H00FFFFFF,&H00FFFFFF,&H00281914,&H00281914,0,0,0,0,100,100,0,0,1,0,0,2,140,140,38,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    (HERE/'captions.ass').write_text(header+'\n'.join(ass),encoding='utf-8')
    (HERE/'transcript.md').write_text('\n'.join(transcript),encoding='utf-8')
    audio_list='\n'.join(f"file '{(HERE/f'audio-{k:02d}.wav').as_posix()}'" for k in range(10))
    (HERE/'audio.txt').write_text(audio_list,encoding='utf-8')
    run([FFMPEG,'-nostdin','-y','-v','error','-f','concat','-safe','0','-i',str(HERE/'audio.txt'),
         '-c:a','pcm_s16le',str(HERE/'narration.wav')])
    (HERE/'speech-metrics.json').write_text(json.dumps(speech_metrics,indent=2),encoding='utf-8')
    chapter_metadata=[';FFMETADATA1','title=Digital Brain - Graph generation in two minutes',
        'comment=Teaching demo: real builder and validator; controlled AI proposals.']
    start=0
    for scene in SCENES:
        end=start+scene['duration']*1000
        chapter_metadata.extend(['[CHAPTER]','TIMEBASE=1/1000',f'START={start}',
                                 f'END={end}',f'title={scene["title"]}'])
        start=end
    (HERE/'chapters.ffmeta').write_text('\n'.join(chapter_metadata),encoding='utf-8')
    # Run in the artifact directory so ASS paths need no Windows-drive escaping.
    command=[FFMPEG,'-nostdin','-y','-v','error','-f','concat','-safe','0','-i','frames.txt',
        '-i','narration.wav','-i','captions.srt','-i','chapters.ffmeta',
        '-map','0:v','-map','1:a','-map','2:s','-map_metadata','3','-map_chapters','3',
        '-vf','fps=24,ass=captions.ass','-t','120','-c:v','libx264','-preset','fast',
        '-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-c:s','mov_text',
        '-metadata:s:s:0','language=eng','-disposition:s:0','0',
        '-movflags','+faststart','graph-generation-2-minutes.mp4']
    print('Frames, captions and narration ready. Encoding 1920x1080 / 24fps / 120 seconds.',flush=True)
    subprocess.run(command,cwd=HERE,check=True,stdout=subprocess.DEVNULL,
                   stderr=(HERE/'encoding.log').open('w',encoding='utf-8'))
    frame(0,2).save(HERE/'poster.png')
    thumbs=Image.new('RGB',(1440,5*270),'white')
    for k in range(10):
        shot=frame(k,2);shot.thumbnail((720,405))
        # Crop only the unused caption band for the review contact sheet.
        shot=shot.crop((0,0,720,270))
        thumbs.paste(shot,((k%2)*720,(k//2)*270))
    thumbs.save(HERE/'scene-contact-sheet.png')
    print('MP4 encoded successfully.',flush=True)


if __name__=='__main__':
    main()
