"""Continuous left-to-right teaching canvas. Layout never changes between chapters."""
import functools
import importlib.util
import json
import math
import shutil
import subprocess
from pathlib import Path
from PIL import Image, ImageDraw

HERE=Path(__file__).resolve().parent
PREVIOUS=HERE.parent/'video-2026-10-14-v2'
spec=importlib.util.spec_from_file_location('previous',PREVIOUS/'build_video.py')
b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
txt=b.txt;font=b.font
INK=b.INK;BLUE=b.BLUE;GREEN=b.GREEN;PURPLE=b.PURPLE;AMBER=b.AMBER;RED=b.RED;MUTED=b.MUTED
BG='#f7f9fc';LINE='#d4dfeb';GRAY='#8b9caf'
SCENES=b.SCENES
POSITIONS=[50,360,670,980,1290,1600]
STAGES=[0,1,2,3,4,4,4,5,5]
QUOTES=b.QUOTES

def icon(d,x,y,kind,color=BLUE,s=30):
    # A consistent vector icon family drawn locally, with text labels beside it.
    def line(points,w=2):d.line([(x+a*s,y+c*s) for a,c in points],fill=color,width=w)
    def rect(r):d.rounded_rectangle((x+r[0]*s,y+r[1]*s,x+r[2]*s,y+r[3]*s),3,outline=color,width=2)
    if kind in ('file','guide'):
        rect((.12,.04,.82,.95));line([(.28,.31),(.66,.31)]);line([(.28,.49),(.66,.49)]);line([(.28,.67),(.56,.67)])
        if kind=='guide':line([(.2,.04),(.2,.95)])
    elif kind=='convert':
        line([(.03,.3),(.92,.3),(.72,.1)]);line([(.92,.3),(.72,.5)]);line([(.94,.75),(.06,.75),(.25,.55)]);line([(.06,.75),(.25,.95)])
    elif kind=='graph':
        line([(.2,.25),(.78,.2),(.5,.8),(.2,.25)])
        for a,c in [(.2,.25),(.78,.2),(.5,.8)]:d.ellipse((x+(a-.12)*s,y+(c-.12)*s,x+(a+.12)*s,y+(c+.12)*s),fill=BG,outline=color,width=2)
    elif kind=='ai':
        line([(.5,.02),(.62,.36),(.98,.5),(.62,.64),(.5,.98),(.36,.64),(.02,.5),(.36,.36),(.5,.02)])
    elif kind in ('check','publish'):
        line([(.5,.04),(.91,.2),(.85,.7),(.5,.97),(.15,.7),(.09,.2),(.5,.04)])
        line([(.27,.49),(.43,.66),(.74,.33)],3)
    elif kind=='database':
        d.ellipse((x+.08*s,y+.05*s,x+.92*s,y+.35*s),outline=color,width=2)
        line([(.08,.2),(.08,.8)]);line([(.92,.2),(.92,.8)])
        d.arc((x+.08*s,y+.62*s,x+.92*s,y+.94*s),0,180,fill=color,width=2)
        d.arc((x+.08*s,y+.37*s,x+.92*s,y+.67*s),0,180,fill=color,width=2)
    elif kind=='portal':
        rect((.03,.1,.97,.79));line([(.07,.28),(.93,.28)]);line([(.5,.8),(.5,.97)]);line([(.28,.97),(.72,.97)])
    elif kind=='vector':
        for j,h in enumerate([.45,.8,.3,.65]):rect((.03+j*.24,1-h,.15+j*.24,.95))
    elif kind=='lock':
        rect((.13,.42,.87,.97));d.arc((x+.27*s,y+.03*s,x+.73*s,y+.73*s),180,360,fill=color,width=3)
    else:icon(d,x,y,'file',color,s)

def box(d,r,title,kind,color=BLUE):
    d.rounded_rectangle(r,18,fill='white',outline=LINE,width=2)
    icon(d,r[0]+16,r[1]+16,kind,color,28)
    txt(d,(r[0]+58,r[1]+13),title,25,color,True,width=r[2]-r[0]-75)

def dashed(d,a,z,color=GRAY):
    length=math.dist(a,z);n=max(1,int(length/12))
    for j in range(0,n,2):
        t=j/n;u=min((j+1)/n,1)
        d.line((a[0]+(z[0]-a[0])*t,a[1]+(z[1]-a[1])*t,a[0]+(z[0]-a[0])*u,a[1]+(z[1]-a[1])*u),fill=color,width=2)

def arrow(d,a,z,color=BLUE,label=None,small=False):
    b.arrow(d,a,z,color,width=3)
    if label:
        cx=(a[0]+z[0])/2;cy=(a[1]+z[1])/2;f=font(17 if small else 23,True);w=d.textlength(label,font=f)
        if small:
            d.text((cx-w/2,cy-26),label,font=f,fill=color)
        else:
            d.rounded_rectangle((cx-w/2-6,cy-15,cx+w/2+6,cy+17),5,fill='white')
            d.text((cx-w/2,cy-14),label,font=f,fill=color)

def mini(d,r,title,kind,color,subtitle=None):
    d.rounded_rectangle(r,12,fill='white',outline=color,width=2)
    icon(d,r[0]+9,r[1]+10,kind,color,23)
    txt(d,(r[0]+40,r[1]+8),title,21,INK,True,width=r[2]-r[0]-47)
    if subtitle:txt(d,(r[0]+12,r[1]+43),subtitle,18,MUTED,width=r[2]-r[0]-20)

@functools.lru_cache(maxsize=24)
def canvas(i,source):
    im=Image.new('RGB',(1920,1080),BG);d=ImageDraw.Draw(im);active=STAGES[i]
    txt(d,(50,24),'How our Digital Brain builds a knowledge graph',43,INK,True)
    txt(d,(50,83),'Follow three insurance documents from left to right. Each connection keeps its source evidence.',24,MUTED)
    # Examples are permanently anchored above and below the main flow.
    box(d,(50,132,610,356),'Our three source documents','file',BLUE)
    names=['Policy System.pdf','Claims Processing.docx','Claims Guide.docx']
    brief=['Portal uses Policy Database','Portal uses Claims Database','Guide explains Claims Portal']
    for k in range(3):
        y=188+k*52;icon(d,70,y,'file',b.SOURCE[k],24)
        txt(d,(108,y-2),f'S{k+1}  {names[k]}',22,b.SOURCE[k],True)
        txt(d,(108,y+24),brief[k],20,MUTED)
    box(d,(670,132,1230,356),'Small graph: document structure','graph',BLUE)
    arrow(d,(894,239),(1010,210),GRAY,'contains',True)
    arrow(d,(894,255),(1010,298),GRAY,'contains',True)
    mini(d,(690,213,894,283),'Policy\nSystem','file',BLUE)
    mini(d,(1010,180,1210,243),'Overview','file',GRAY)
    mini(d,(1010,270,1210,333),'Sentence','file',GRAY)
    txt(d,(693,310),'Document node',18,MUTED)
    txt(d,(1024,333),'Section nodes',16,MUTED)
    box(d,(1290,132,1870,356),'Business relationship graph','graph',GREEN)
    color=GREEN if i>=4 else AMBER
    arrow(d,(1425,246),(1513,246),color,'explains',True)
    arrow(d,(1660,231),(1720,219),color,'uses',True)
    arrow(d,(1660,264),(1720,299),color,'uses',True)
    mini(d,(1305,214,1425,283),'Guide','guide',GREEN)
    mini(d,(1513,214,1660,283),'Claims\nPortal','portal',BLUE)
    mini(d,(1720,188,1853,251),'Policy\nDB','database',PURPLE)
    mini(d,(1720,267,1853,334),'Claims\nDB','database',PURPLE)
    # Dotted leaders join explanations to their corresponding stages.
    for x in (180,800,1430):dashed(d,(x,356),(x,403))
    stages=[('Upload','file','Django / Python'),('Read text','convert','MarkItDown'),('Build structure','graph','Custom Python'),('Extract links','ai','OpenAI / Claude'),('Check + merge','check','Python / ontology'),('Save + publish','database','Django revisions')]
    for j,(title,kind,tech) in enumerate(stages):
        x=POSITIONS[j];col=BLUE if j==active else GREEN if j<active else GRAY
        d.rounded_rectangle((x,406,x+260,549),17,fill='#eaf1ff' if j==active else 'white',outline=col,width=4 if j==active else 2)
        icon(d,x+18,423,kind,col,31);txt(d,(x+62,422),title,24,INK,True,width=186)
        txt(d,(x+17,480),tech,22,col,True,width=230)
        if j==3:txt(d,(x+18,515),'Optional enrichment',18,MUTED)
        elif j==5:txt(d,(x+18,515),'Human review required',18,MUTED)
        if j<5:arrow(d,(x+268,477),(POSITIONS[j+1]-8,477),col)
    for x in (490,1110,1430):dashed(d,(x,550),(x,593))
    box(d,(50,594,610,829),'Example: converted text','convert',BLUE)
    txt(d,(73,651),f'S{source+1}  /  {names[source]}',22,b.SOURCE[source],True)
    txt(d,(73,690),'## Overview',24,INK,True)
    txt(d,(73,731),QUOTES[source],23,INK,width=510)
    box(d,(670,594,1230,829),'Example: a proposed relationship','ai',AMBER)
    subject='Guide' if source==2 else 'Claims Portal';obj=['Policy DB','Claims DB','Claims Portal'][source];rel='explains' if source==2 else 'uses'
    mini(d,(691,657,866,738),subject,'guide' if source==2 else 'portal',BLUE)
    mini(d,(1036,657,1210,738),obj,'portal' if source==2 else 'database',PURPLE)
    arrow(d,(877,699),(1026,699),AMBER,rel)
    txt(d,(693,751),'Subject  →  relationship  →  object',23,INK,True)
    txt(d,(693,783),'+ types + source ID + exact quotation',19,MUTED)
    txt(d,(693,803),'Teaching example: prepared AI proposals',16,MUTED)
    box(d,(1290,594,1870,829),'Evidence checks before acceptance','check',GREEN)
    for j,v in enumerate(['Source exists; exact quote is present','Named items and types are allowed','Matching name + type → one shared node']):
        icon(d,1311,651+j*42,'check',GREEN,22);txt(d,(1349,648+j*42),v,21,INK,width=493)
    txt(d,(1310,786),'Invented Payment Database quote → rejected',20,RED,True)
    # Persistent side branches clarify search preparation and Graphify's role.
    for r,title,detail,kind,col in [((50,852,610,932),'Optional search preparation','OpenAI embeddings → stored numerical vectors','vector',PURPLE),((670,852,1230,932),'Graphify organises existing nodes','NetworkX + Leiden → themes for review','graph',PURPLE),((1290,852,1870,932),'Published, traceable knowledge','4 business nodes / 3 links + source evidence','lock',GREEN)]:
        d.rounded_rectangle(r,13,fill='white',outline=col,width=2)
        icon(d,r[0]+14,r[1]+15,kind,col,27)
        txt(d,(r[0]+55,r[1]+10),title,22,col,True)
        txt(d,(r[0]+18,r[1]+47),detail,20,MUTED)
    # Focus borders change; panels, examples and graph coordinates stay fixed.
    focus={0:(50,132,610,356),1:(50,594,610,829),2:(670,132,1230,356),3:(670,594,1230,829),4:(1290,594,1870,829),5:(1290,132,1870,356),6:(50,852,610,932),7:(670,852,1230,932),8:(1290,852,1870,932)}[i]
    d.rounded_rectangle(focus,17,outline=BLUE,width=4)
    d.rectangle((0,950,1920,1080),fill='#17324d')
    return im

def frame(i,t):
    p=t/SCENES[i]['duration'];source=(0 if p<.62 else 1 if p<.76 else 2) if i==3 else 0
    im=canvas(i,source).copy();d=ImageDraw.Draw(im);active=STAGES[i]
    # A moving marker carries attention along the permanent horizontal flow.
    x=POSITIONS[active]+15+(230*p);d.ellipse((x-6,563,x+6,575),fill=BLUE)
    elapsed=sum(s['duration'] for s in SCENES[:i])+t
    d.rectangle((0,1072,int(1920*elapsed/240),1080),fill='#54c7bb')
    return im

def main():
    for name in ['narration.wav','captions.srt','captions.ass','chapters.ffmeta','storyboard.json','transcript.md','speech-metrics.json']:
        shutil.copy2(PREVIOUS/name,HERE/name)
    for i,s in enumerate(SCENES):frame(i,s['duration']*.82).save(HERE/f'preview-{i:02d}.png')
    cmd=[b.FFMPEG,'-nostdin','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r','12','-i','pipe:0','-i','narration.wav','-i','captions.srt','-i','chapters.ffmeta','-map','0:v','-map','1:a','-map','2:s','-map_metadata','3','-map_chapters','3','-vf','fps=24,ass=captions.ass','-t','240','-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-c:s','mov_text','-disposition:s:0','0','-movflags','+faststart','graph-generation-continuous-flow.mp4']
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
