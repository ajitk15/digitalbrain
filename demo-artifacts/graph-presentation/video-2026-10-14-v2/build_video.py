"""Render a four-minute animated, evidence-grounded student explanation."""
import functools
import hashlib
import json
import math
import shutil
import subprocess
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE=Path(__file__).resolve().parent
SCENES=json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
LIVE=json.loads((HERE.parent/'insurance-revalidation.json').read_text(encoding='utf-8'))
W,H,FPS=1920,1080,12
FFMPEG,FFPROBE=shutil.which('ffmpeg'),shutil.which('ffprobe')
INK='#18334e'; MUTED='#536b80'; BLUE='#245cc9'; GREEN='#14836c'; PURPLE='#754ac5'; AMBER='#aa6900'; RED='#b33547'
BG='#f4f7fb'; BORDER='#d8e2ec'; SOURCE=[BLUE,PURPLE,GREEN]
NAMES=['Policy System.pdf','Claims Processing.docx','Claims Guide.docx']
QUOTES=[s['content'].split('\n\n')[1] for s in LIVE['sources']]
PHASES=['Upload','Convert','Structure','Extract','Check','Merge','Embed','Save / Group','Publish']

@functools.lru_cache(None)
def font(size,bold=False,mono=False):
    return ImageFont.truetype('C:/Windows/Fonts/'+('consola.ttf' if mono else 'segoeuib.ttf' if bold else 'segoeui.ttf'),size)

def txt(d,xy,value,size=30,color=INK,bold=False,width=None,mono=False):
    f=font(size,bold,mono); lines=[]
    for para in str(value).split('\n'):
        line=''
        for word in para.split():
            trial=(line+' '+word).strip()
            if width and d.textlength(trial,font=f)>width and line: lines.append(line);line=word
            else: line=trial
        lines.append(line)
    d.multiline_text(xy,'\n'.join(lines),font=f,fill=color,spacing=8)

def card(d,r,title,detail='',accent=BLUE,fill='white',size=30):
    x,y,x2,y2=r
    d.rounded_rectangle(r,24,fill=fill,outline=BORDER,width=2)
    d.rounded_rectangle((x,y,x+7,y2),4,fill=accent)
    txt(d,(x+24,y+18),title,size,accent,True,width=x2-x-46)
    if detail: txt(d,(x+24,y+67),detail,size-2,width=x2-x-46)

def label(d,r,value,color=BLUE,fill='#e9f0fd',size=24):
    d.rounded_rectangle(r,14,fill=fill)
    txt(d,(r[0]+16,r[1]+10),value,size,color,True,width=r[2]-r[0]-30)

def arrow(d,a,b,color=BLUE,progress=1,dashed=False,width=5):
    if progress<=0:return
    bx=a[0]+(b[0]-a[0])*min(progress,1);by=a[1]+(b[1]-a[1])*min(progress,1)
    if dashed:
        length=math.hypot(bx-a[0],by-a[1]);n=max(1,int(length/20))
        for j in range(0,n,2):
            t=j/n;u=min((j+1)/n,1)
            d.line((a[0]+(bx-a[0])*t,a[1]+(by-a[1])*t,a[0]+(bx-a[0])*u,a[1]+(by-a[1])*u),fill=color,width=width)
    else:d.line((a,(bx,by)),fill=color,width=width)
    angle=math.atan2(by-a[1],bx-a[0]);d.polygon([(bx,by),(bx-18*math.cos(angle-.5),by-18*math.sin(angle-.5)),(bx-18*math.cos(angle+.5),by-18*math.sin(angle+.5))],fill=color)

def edge(d,a,b,name,color=GREEN,dashed=False):
    arrow(d,a,b,color,dashed=dashed)
    cx=(a[0]+b[0])/2;cy=(a[1]+b[1])/2
    tw=d.textlength(name,font=font(27,True));d.rounded_rectangle((cx-tw/2-13,cy-22,cx+tw/2+13,cy+23),9,fill=BG)
    txt(d,(cx-tw/2,cy-20),name,27,color,True)

def business(d,p=1,yshift=0):
    cy=490+yshift
    if p>.2:edge(d,(575,cy),(735,cy),'explains')
    if p>.4:edge(d,(1115,cy-20),(1360,cy-125),'uses')
    if p>.6:edge(d,(1115,cy+20),(1360,cy+125),'uses')
    card(d,(210,cy-60,575,cy+70),'Claims Guide','Runbook / instructions',GREEN,size=31)
    card(d,(735,cy-60,1115,cy+70),'Claims Portal','Shared Component',BLUE,size=31)
    if p>.4:card(d,(1360,cy-190,1760,cy-60),'Policy Database','Component / policy details',PURPLE,size=31)
    if p>.6:card(d,(1360,cy+60,1760,cy+190),'Claims Database','Component / claim records',PURPLE,size=31)

def sourcecard(d,k,r,short=False):
    card(d,r,f'S{k+1}  {NAMES[k]}',('Overview\n\n'+QUOTES[k]) if not short else QUOTES[k],SOURCE[k],size=30)

def render(i,t):
    s=SCENES[i];p=t/s['duration'];im=Image.new('RGB',(W,H),BG);d=ImageDraw.Draw(im)
    txt(d,(70,28),'DIGITAL BRAIN  /  DOCUMENTS TO KNOWLEDGE',24,BLUE,True)
    txt(d,(70,73),s['title'],51,INK,True)
    txt(d,(1700,45),f'{i+1:02d} / 09',25,MUTED,True)
    for j,name in enumerate(PHASES):
        x=70+j*199;d.rounded_rectangle((x,158,x+185,213),14,fill=BLUE if j==i else '#e5ebf2')
        txt(d,(x+12,169),name,23,'white' if j==i else MUTED,True)
    d.line((70,248,1850,248),fill=BORDER,width=2)
    # Stage transformations are spacious; technology and captions stay below.
    if i==0:
        for k in range(3):
            x=85+k*610;y=305+int(max(0,1-p*7+k*.45)*65)
            sourcecard(d,k,(x,y,x+565,y+350))
            if p>.35+k*.13:label(d,(x,y+375,x+565,y+430),'Uploaded to this application',SOURCE[k],size=25)
        label(d,(360,790,1550,845),'Fictional example documents · no customer or medical records',GREEN,fill='#e5f4ed',size=27)
    elif i==1:
        k=min(2,int(p*3));sourcecard(d,k,(80,305,680,730))
        card(d,(815,405,1150,635),'MarkItDown','File content → text',BLUE,size=32)
        arrow(d,(690,520),(795,520),progress=min(1,p*8))
        arrow(d,(1160,520),(1250,520),progress=min(1,max(0,p*8-.5)))
        card(d,(1270,305,1840,730),'Converted source',f'## Overview\n\n{QUOTES[k]}',SOURCE[k],size=30)
        label(d,(1270,750,1840,802),f'Source S{k+1}  +  content fingerprint',SOURCE[k],size=25)
        txt(d,(95,790),'Access + file integrity checked\nScanning and OCR: when configured',27,MUTED,width=1050)
    elif i==2:
        if p<.64:
            sourcecard(d,0,(80,305,670,800))
            label(d,(80,820,670,867),'INPUT: converted source text',BLUE,size=24)
            card(d,(770,445,1070,560),'Policy System.pdf','Document node',MUTED,size=28)
            if p>.12:
                arrow(d,(1070,470),(1260,360),MUTED,progress=min(1,(p-.12)*9))
                card(d,(1260,300,1840,415),'Overview','Heading → section node',MUTED,size=30)
                txt(d,(1065,345),'contains\nsection',24,MUTED,width=170)
            if p>.29:
                arrow(d,(1070,535),(1260,650),MUTED,progress=min(1,(p-.29)*9))
                card(d,(1260,530,1840,785),'Sentence → section node',QUOTES[0],MUTED,size=30)
                txt(d,(1065,655),'contains\nsection',24,MUTED,width=170)
            label(d,(775,808,1840,860),'Structure describes where the text belongs',MUTED,fill='#e5ebf2',size=27)
        else:
            for k in range(3):
                x=85+k*610
                card(d,(x,300,x+550,402),NAMES[k],'Document node',SOURCE[k],size=27)
                arrow(d,(x+140,410),(x+140,488),MUTED)
                card(d,(x,490,x+550,597),'Overview','Heading / section node',MUTED,size=28)
                # The sentence section attaches to its document, not to the heading.
                d.line((x+550,351,x+580,351,x+580,723,x+550,723),fill=MUTED,width=4)
                arrow(d,(x+580,723),(x+550,723),MUTED)
                card(d,(x,630,x+550,797),'Sentence / section node',QUOTES[k],MUTED,size=26)
                txt(d,(x+170,435),'contains section',23,MUTED)
            label(d,(310,815,1610,865),'3 documents + 6 sections = 9 nodes · 6 structural edges',MUTED,fill='#e5ebf2',size=28)
    elif i==3:
        k=0 if p<.62 else 1 if p<.76 else 2
        label(d,(80,280,1840,335),f'SOURCE S{k+1}  /  {NAMES[k]}',SOURCE[k],size=27)
        card(d,(80,355,1840,463),'Read the exact sentence','',SOURCE[k],size=29)
        x=104
        for j,word in enumerate(QUOTES[k].split()):
            color=BLUE if j in (1,2) else AMBER if j==3 else PURPLE if (k<2 and j in (5,6)) or (k==2 and j in (5,6)) else INK
            txt(d,(x,420),word,29,color,True if color!=INK else False)
            x+=d.textlength(word+' ',font=font(29,color!=INK))
        subject='Claims Guide' if k==2 else 'Claims Portal';obj=['Policy Database','Claims Database','Claims Portal'][k];rel='explains' if k==2 else 'uses'
        card(d,(100,535,610,695),'SUBJECT',subject,BLUE,size=34)
        card(d,(745,535,1180,695),'RELATIONSHIP',rel,AMBER,size=34)
        card(d,(1310,535,1820,695),'OBJECT',obj,PURPLE,size=34)
        arrow(d,(625,615),(730,615),AMBER,dashed=True);arrow(d,(1195,615),(1295,615),AMBER,dashed=True)
        txt(d,(125,720),'Type: '+('Runbook' if k==2 else 'Component'),27,BLUE)
        txt(d,(1335,720),'Type: Component',27,PURPLE)
        label(d,(100,785,1180,845),'Proposal includes source ID + exact supporting quote',AMBER,fill='#fff1d9',size=27)
        label(d,(1250,785,1820,845),'Prepared AI proposal · not live',AMBER,fill='#fff1d9',size=25)
    elif i==4:
        card(d,(80,310,705,620),'PROPOSED CONNECTION','Claims Portal → uses → Policy Database\n\n'+QUOTES[0],BLUE,size=30)
        checks=['1  Source exists','2  Exact quote is present','3  Named items appear','4  Types + relation allowed']
        for k,c in enumerate(checks):
            label(d,(815,300+k*85,1260,370+k*85),c,GREEN if p>(k+1)*.105 else MUTED,fill='#e5f4ed' if p>(k+1)*.105 else '#e5ebf2',size=25)
        arrow(d,(710,460),(800,460),BLUE)
        if p>.45:
            arrow(d,(1270,460),(1360,460),GREEN)
            card(d,(1380,350,1830,565),'ACCEPTED','Evidence checks passed',GREEN,fill='#e5f4ed',size=32)
        if p>.55:
            card(d,(80,675,1840,845),'REJECTED: invented quotation','“The claims portal uses a payment database.”    →    Quote not in source',RED,fill='#fff0f2',size=31)
        else:txt(d,(130,760),'Checks filter errors; human review still checks the meaning.',30,MUTED)
    elif i==5:
        if p<.35:
            for k in range(3):card(d,(90+k*610,315,650+k*610,530),f'S{k+1}: Claims Portal','Same name + same type',SOURCE[k],size=32)
            label(d,(390,660,1530,735),'Merge matching references into one shared entity',BLUE,size=31)
        else:
            business(d,min(1,(p-.35)*3.8))
            card(d,(80,740,1840,857),'EVIDENCE FOR THE POLICY CONNECTION',f'S1 · {QUOTES[0]}',BLUE,size=28)
        label(d,(520,270,1340,320),'Business view: 4 nodes · 3 connections',GREEN,fill='#e5f4ed',size=27)
    elif i==6:
        card(d,(80,335,650,710),'Text prepared for search','Graph relationships\n\nDocument passages\n\nUnchanged text reuses vectors',BLUE,size=32)
        arrow(d,(670,500),(790,500),BLUE)
        card(d,(810,375,1190,655),'OpenAI embeddings','Configured embedding model\n\nOPTIONAL',PURPLE,size=30)
        arrow(d,(1210,500),(1325,500),PURPLE,progress=min(1,p*4))
        card(d,(1345,335,1840,710),'Stored numerical vector','[0.12, −0.08, 0.31, …]\n\n512 numbers per vector\n\nDjango Embedding storage',PURPLE,size=30)
        label(d,(280,775,1650,845),'For later meaning-based search · not proof of a graph connection',GREEN,fill='#e5f4ed',size=28)
    elif i==7:
        card(d,(80,310,680,815),'NUMBERED DRAFT','Django GraphRevision\n\nGraph + evidence + quality\n\nHuman review follows\n\nSaving illustrated in this demo',BLUE,size=33)
        arrow(d,(700,545),(835,545),PURPLE)
        card(d,(855,310,1840,815),'GRAPHIFY: derived themes','NetworkX graph → Leiden clustering\n\nExisting nodes grouped for review',PURPLE,size=31)
        for k,row in enumerate(LIVE['graphify_themes']['rows']):
            y=500+k*90;txt(d,(885,y),row['label'],28,INK,True)
            d.rounded_rectangle((1310,y+4,1310+row['nodes']*55,y+36),8,fill=SOURCE[k]);txt(d,(1630,y),str(row['nodes'])+' nodes',25,MUTED)
        txt(d,(885,785),'Actual synthetic grouping · includes structural nodes',24,MUTED)
    else:
        for k,(title,detail) in enumerate([('Review','Check sources + meaning'),('Publish','Human action'),('Graph answers','Published version only')]):
            x=90+k*610;card(d,(x,300,x+550,455),title,detail,GREEN if k==1 else BLUE,size=32)
            if k<2:arrow(d,(x+560,380),(x+590,380),GREEN)
        edge(d,(575,610),(735,610),'explains')
        edge(d,(1115,590),(1360,545),'uses')
        edge(d,(1115,630),(1360,745),'uses')
        card(d,(210,550,575,670),'Claims Guide','Runbook / instructions',GREEN,size=29)
        card(d,(735,550,1115,670),'Claims Portal','Shared Component',BLUE,size=29)
        card(d,(1360,490,1760,600),'Policy Database','Component / policy details',PURPLE,size=29)
        card(d,(1360,690,1760,800),'Claims Database','Component / claim records',PURPLE,size=29)
        label(d,(140,820,1780,872),'Restricted sources excluded · answers recheck access and current evidence',GREEN,fill='#e5f4ed',size=28)
    txt(d,(70,890),'TECHNOLOGY  '+s['tech'],26,BLUE,True,width=1780)
    d.rectangle((0,940,W,H),fill='#17324d')
    d.rectangle((0,1068,int(W*p),1080),fill='#54c7bb')
    return im

def run(args,**kw):subprocess.run(args,check=True,**kw)
def duration(path):return float(subprocess.check_output([FFPROBE,'-v','error','-show_entries','format=duration','-of','csv=p=0',str(path)]))
def clock(sec,ass=False):
    unit=100 if ass else 1000;n=round(sec*unit);h,n=divmod(n,3600*unit);m,n=divmod(n,60*unit);s,f=divmod(n,unit)
    return f'{h}:{m:02d}:{s:02d}.{f:02d}' if ass else f'{h:02d}:{m:02d}:{s:02d},{f:03d}'

def main():
    assert FFMPEG and FFPROBE
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
Style: Default,Segoe UI,36,&H00FFFFFF,&H00FFFFFF,&H004D3217,&H004D3217,0,0,0,0,100,100,0,0,1,0,0,2,130,130,45,1
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
    cmd=[FFMPEG,'-nostdin','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r',str(FPS),'-i','pipe:0','-i','narration.wav','-i','captions.srt','-i','chapters.ffmeta','-map','0:v','-map','1:a','-map','2:s','-map_metadata','3','-map_chapters','3','-vf','fps=24,ass=captions.ass','-t','240','-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-c:s','mov_text','-metadata:s:a:0','language=eng','-metadata:s:a:0','title=Indian English - Microsoft Heera','-metadata:s:s:0','language=eng','-disposition:s:0','0','-movflags','+faststart','graph-generation-4-minutes.mp4']
    with (HERE/'encoding.log').open('w') as log:
        process=subprocess.Popen(cmd,cwd=HERE,stdin=subprocess.PIPE,stderr=log)
        try:
            for i,s in enumerate(SCENES):
                print(f'Rendering scene {i+1}/9: {s["title"]}',flush=True)
                for n in range(s['duration']*FPS):process.stdin.write(render(i,n/FPS).tobytes())
                render(i,s['duration']*.82).save(HERE/f'scene-{i:02d}.png')
            process.stdin.close();assert process.wait()==0
        except BaseException:
            process.kill();process.wait();raise
    render(5,18).save(HERE/'poster.png')
    print('Encoded four-minute 1080p video with Indian English narration.',flush=True)

if __name__=='__main__':main()
