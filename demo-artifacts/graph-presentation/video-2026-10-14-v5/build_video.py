"""Fixed chapter rail with a continuously growing graph workspace."""
import importlib.util, math, shutil, subprocess, functools
from pathlib import Path
from PIL import Image, ImageDraw
HERE=Path(__file__).resolve().parent
PREVIOUS=HERE.parent/'video-2026-10-14-v2'
spec=importlib.util.spec_from_file_location('flow',HERE.parent/'video-2026-10-14-v4/build_flow_video.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
b=v.b;txt=b.txt;font=b.font;icon=v.icon
SCENES=b.SCENES
BLUE=b.BLUE;GREEN=b.GREEN;PURPLE=b.PURPLE;AMBER=b.AMBER;RED=b.RED;INK=b.INK;MUTED=b.MUTED
BG='#f5f8fc';LINE='#d9e4ef'
TITLES=['Upload documents','Read the content','Build normal graph','Enrich with AI','Check the evidence','Merge shared items','Prepare search','Organise the draft','Review & publish']
TECH=['Django / Python','MarkItDown','Custom Python builder','OpenAI / Claude · optional','Python / ontology rules','Name + type matching','OpenAI embeddings · optional','Graphify / NetworkX / Leiden','Django / human review']
ICONS=['file','convert','graph','ai','check','graph','vector','database','lock']
SHORT=['Policy System','Claims Processing','Claims Guide']
def clamp(x):return max(0,min(1,x))
def ease(x):
    x=clamp(x);return x*x*(3-2*x)
def lerp(a,z,p):return a+(z-a)*p

def node(d,x,y,r,label,color,kind='graph',amount=1):
    if amount<=0:return
    r=r*(.5+.5*ease(amount))
    d.ellipse((x-r-5,y-r-5,x+r+5,y+r+5),fill='#e7edf5')
    d.ellipse((x-r,y-r,x+r,y+r),fill='white',outline=color,width=3)
    if amount<.65:return
    if r<=30:
        if '\n' in label:label=''.join(part[0] for part in label.split())
        f=font(15,True);w=d.textlength(label,font=f)
        d.text((x-w/2,y-10),label,font=f,fill=INK)
        return
    icon(d,x-12,y-r+11,kind,color,24 if r>50 else 18)
    lines=label.split('\n');size=20 if r>50 else 16
    while size>12 and max(d.textlength(line,font=font(size,True)) for line in lines)>r*1.65:size-=1
    f=font(size,True)
    for j,line in enumerate(lines):
        w=d.textlength(line,font=f);d.text((x-w/2,y-10+j*(size+4)),line,font=f,fill=INK)

def edge(d,a,z,ra,rz,label,color,amount=1,phase=0,dashed=False):
    if amount<=0:return
    dx=z[0]-a[0];dy=z[1]-a[1];l=math.hypot(dx,dy)
    start=(a[0]+dx/l*(ra+4),a[1]+dy/l*(ra+4));end=(z[0]-dx/l*(rz+5),z[1]-dy/l*(rz+5))
    b.arrow(d,start,end,color,progress=ease(amount),dashed=dashed,width=3)
    if amount>=1:
        if label:
            x=(start[0]+end[0])/2;y=(start[1]+end[1])/2-25;w=d.textlength(label,font=font(20,True))
            d.rounded_rectangle((x-w/2-7,y-2,x+w/2+7,y+26),6,fill=BG)
            txt(d,(x-w/2,y),label,20,color,True)
        q=(phase%3)/3;x=lerp(start[0],end[0],q);y=lerp(start[1],end[1],q)
        d.ellipse((x-4,y-4,x+4,y+4),fill=color)

@functools.lru_cache(None)
def background(i):
    im=Image.new('RGB',(1920,1080),BG);d=ImageDraw.Draw(im)
    d.rectangle((0,0,438,950),fill='#142e48')
    txt(d,(32,32),'DIGITAL BRAIN',29,'white',True)
    txt(d,(32,78),'FROM DOCUMENTS\nTO CONNECTED KNOWLEDGE',19,'#a9bfd3')
    for j,title in enumerate(TITLES):
        y=169+j*79;active=j==i;done=j<i
        if active:d.rounded_rectangle((19,y-7,417,y+62),14,fill='#244c71')
        col='#75ded0' if done else '#a9ceff' if active else '#8fa6bd'
        icon(d,35,y+7,'check' if done else ICONS[j],col,28)
        txt(d,(82,y),f'{j+1:02}  {title}',24,'white' if active else col,True)
        if active:txt(d,(83,y+34),'NOW EXPLAINING',14,'#a9ceff',True)
    txt(d,(32,900),'Fictional insurance example',19,'#a9bfd3')
    txt(d,(492,28),TITLES[i],43,INK,True)
    txt(d,(493,89),TECH[i],24,BLUE,True)
    txt(d,(1530,43),'ONE GROWING GRAPH',19,MUTED,True)
    d.line((492,132,1865,132),fill=LINE,width=2)
    d.rounded_rectangle((490,790,1870,928),18,fill='white',outline=LINE,width=2)
    d.rectangle((0,950,1920,1080),fill='#17324d')
    return im

def frame(i,t):
    im=background(i).copy();d=ImageDraw.Draw(im)
    elapsed=sum(s['duration'] for s in SCENES[:i])+t
    # The same source cards remain anchored above the growing graph.
    for k in range(3):
        start=[2,6,9][k];reveal=1 if i else ease((t-start)/1.2)
        if reveal<=0:continue
        x=492+k*463;y=160+int((1-reveal)*45)
        d.rounded_rectangle((x,y,x+444,y+153),16,fill='white',outline=b.SOURCE[k],width=2)
        icon(d,x+15,y+15,'file',b.SOURCE[k],27)
        txt(d,(x+55,y+13),f'S{k+1}  {SHORT[k]}',24,b.SOURCE[k],True)
        if i>=1:
            txt(d,(x+17,y+54),'## Overview',19,MUTED,True)
            txt(d,(x+17,y+84),b.QUOTES[k],21,INK,width=410)
        else:
            txt(d,(x+18,y+61),['Policy details','Claim records','Portal instructions'][k],26,INK,True)
            txt(d,(x+18,y+109),'PDF document' if k==0 else 'Word document',20,MUTED)
    if i==0:
        icon(d,1139,417,'file',BLUE,70)
        txt(d,(835,525),'Three documents. One connected story.',35,INK,True)
        txt(d,(841,585),'Every fact will keep a path back to its source.',25,MUTED)
        txt(d,(520,818),'START WITH EVIDENCE',22,BLUE,True)
        txt(d,(520,859),'Upload the documents into the application. No graph exists yet.',29,INK)
    if i==1:
        for k in range(3):
            x=714+k*463;q=clamp((t-3-k*2)/1.5)
            b.arrow(d,(x,325),(x,435),BLUE,progress=q,width=3)
            if q>.5:
                icon(d,x-20,463,'convert',BLUE,40)
                txt(d,(x-139,528),'Readable source text',25,INK,True)
                txt(d,(x-139,568),f'Source ID: S{k+1}',21,b.SOURCE[k],True)
                txt(d,(x-139,604),'Content fingerprint: SHA-256',19,MUTED)
        txt(d,(520,818),'FILES → TEXT WITH SOURCE IDENTITY',22,BLUE,True)
        txt(d,(520,859),'MarkItDown reads the content; Django retains its source and fingerprint.',28,INK)
    if i>=2:
        shrink=ease(t/4) if i==3 else 1 if i>3 else 0
        for k in range(3):
            x=714+k*463;y=lerp(418,358,shrink);sy=lerp(636,427,shrink);r=lerp(53,24,shrink);sr=lerp(45,22,shrink)
            born=(t-(2+k*2))/1.3 if i==2 else 1
            if born>0:
                node(d,x,y,r,['Policy\nSystem','Claims\nProcessing','Claims\nGuide'][k] if shrink<.8 else f'S{k+1}',b.SOURCE[k],'file',clamp(born))
                for h in range(2):
                    when=(11 if h==0 else 16)+(0 if k==0 else 10+k*2)
                    a=clamp((t-when)/1.5) if i==2 else 1
                    xx=x+(-91 if h==0 else 91)
                    if a>0:
                        edge(d,(x,y),(xx,sy),r,sr,'',b.SOURCE[k],a,t)
                        node(d,xx,sy,sr,['Overview','Sentence'][h] if shrink<.8 else ['H','T'][h],b.SOURCE[k],'file',a)
        if i==2:
            txt(d,(905,725),'Each arrow means: contains section',23,BLUE,True)
            txt(d,(520,818),'NORMAL GRAPH = DOCUMENT STRUCTURE',22,BLUE,True)
            txt(d,(520,858),'Documents connect to headings and sentences. Business links come next.',28,INK)
        else:
            txt(d,(505,455),'STRUCTURAL LAYER RETAINED  ·  H = heading, T = text',16,MUTED,True)
    if i>=3:
        # AI proposals add business meaning under the retained structure.
        portal=(1110,602);policy=(1630,535);claims=(1630,705);guide=(720,665)
        a=clamp((t-9)/1.5) if i==3 else 1
        e=clamp((t-14)/1.8) if i==3 else 1
        c=clamp((t-28)/1.8) if i==3 else 1
        g=clamp((t-34)/1.8) if i==3 else 1
        col=AMBER if i==3 or (i==4 and t<12) else GREEN
        if i==7 and t>5:
            d.rounded_rectangle((635,490,1750,778),48,fill='#e7f4ef',outline='#9bd1bc',width=2)
            txt(d,(670,748),'Illustrative theme boundary · Graphify groups existing connections',17,GREEN)
        if a:
            node(d,*portal,61,'Claims\nPortal',BLUE,'portal',a)
            node(d,*policy,57,'Policy\nDatabase',PURPLE,'database',a)
        if e:edge(d,portal,policy,61,57,'uses',col,e,t)
        if c:
            node(d,*claims,57,'Claims\nDatabase',PURPLE,'database',c)
            edge(d,portal,claims,61,57,'uses',col,c,t)
        if g:
            node(d,*guide,56,'Claims\nGuide',GREEN,'guide',g)
            edge(d,guide,portal,56,61,'explains',col,g,t)
        if i==3:
            src=2 if t>=34 else 1 if t>=28 else 0
            x=492+src*463
            d.rounded_rectangle((x-3,157,x+447,316),18,outline=AMBER,width=4)
            txt(d,(520,809),'AI PROPOSAL  ·  SUBJECT → RELATIONSHIP → OBJECT',22,AMBER,True)
            txt(d,(520,847),b.QUOTES[src],28,INK)
            txt(d,(520,891),'Prepared teaching proposals · source ID + exact quote travel with each proposed relationship',19,MUTED)
        elif i==4:
            txt(d,(520,807),'EVIDENCE GATE',22,GREEN,True)
            checks=['Source exists','Exact quote found','Names + types allowed']
            for k,vv in enumerate(checks):
                if t>k*4:
                    icon(d,520+k*430,847,'check',GREEN,26);txt(d,(559+k*430,846),vv,24,INK,True)
            if 13<t<22:
                txt(d,(520,889),'REJECTED: Payment Database — its claimed source quote does not exist.',21,RED,True)
                d.line((1300,638,1500,638),fill=RED,width=3)
                txt(d,(1330,609),'unsupported',18,RED)
            elif t>=22:txt(d,(520,889),'Evidence checks filter unsupported claims; a person still reviews meaning.',21,MUTED)
        elif i==5:
            if t<8:
                q=ease(t/8)
                for k,pos in enumerate([(920,505),(950,730)]):
                    x=lerp(pos[0],portal[0],q);y=lerp(pos[1],portal[1],q)
                    if q<.95:node(d,x,y,35,'Portal',b.SOURCE[k],'portal')
            txt(d,(520,809),'ONE SHARED ENTITY',22,GREEN,True)
            txt(d,(520,847),'Matching name + type → one Claims Portal, four business nodes, three links.',27,INK)
            txt(d,(520,891),'S1 evidence: '+b.QUOTES[0],21,MUTED)
        elif i==6:
            txt(d,(520,809),'OPTIONAL SEARCH LAYER  ·  DOES NOT CREATE GRAPH LINKS',22,PURPLE,True)
            txt(d,(520,847),'Relationship text + passages → OpenAI embeddings → stored vectors',27,INK)
            for k,h in enumerate([19,39,25,46,30,17,40,23]):
                x=525+k*27;d.rounded_rectangle((x,918-h*clamp((t-4)/3),x+15,919),3,fill=PURPLE)
            txt(d,(770,888),'Numerical meaning for later retrieval · unchanged text can reuse its vector',21,MUTED)
        elif i==7:
            txt(d,(520,809),'SAVE THE DRAFT, THEN ORGANISE IT',22,GREEN,True)
            txt(d,(520,847),'Django revision → Graphify → NetworkX + Leiden → themes for review',27,INK)
            txt(d,(520,891),'Grouping helps explore the graph. It adds no new underlying facts.',21,MUTED)
        elif i==8:
            txt(d,(520,809),'REVIEW → PUBLISH',22,GREEN,True)
            txt(d,(520,847),'Connected knowledge, with evidence behind every accepted business link.',28,INK)
            txt(d,(520,891),'Restricted sources excluded · retrieval rechecks user access and current evidence',21,MUTED)
            if t>4:
                icon(d,1780,550,'check',GREEN,52);txt(d,(1760,619),'Reviewed',19,GREEN,True)
    d.rectangle((0,1072,int(1920*elapsed/240),1080),fill='#54c7bb')
    return im

def main():
    for name in ['narration.wav','captions.srt','captions.ass','chapters.ffmeta','storyboard.json','transcript.md','speech-metrics.json']:
        shutil.copy2(PREVIOUS/name,HERE/name)
    for i,s in enumerate(SCENES):frame(i,s['duration']*.82).save(HERE/f'preview-{i:02d}.png')
    cmd=[b.FFMPEG,'-nostdin','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r','12','-i','pipe:0','-i','narration.wav','-i','captions.srt','-i','chapters.ffmeta','-map','0:v','-map','1:a','-map','2:s','-map_metadata','3','-map_chapters','3','-vf','fps=24,ass=captions.ass','-t','240','-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-c:s','mov_text','-disposition:s:0','0','-movflags','+faststart','graph-generation-growing-brain.mp4']
    with (HERE/'encoding.log').open('w') as log:
        proc=subprocess.Popen(cmd,cwd=HERE,stdin=subprocess.PIPE,stderr=log)
        try:
            for i,s in enumerate(SCENES):
                print(f'Flow focus {i+1}/9: {s["title"]}',flush=True)
                for n in range(s['duration']*12):proc.stdin.write(frame(i,n/12).tobytes())
            proc.stdin.close();assert proc.wait()==0
        except BaseException:proc.kill();proc.wait();raise
    frame(5,18).save(HERE/'poster.png')
    print('Continuous-flow video encoded.',flush=True)

if __name__=='__main__':main()
