"""CarePath teaching animation: compact files, cumulative graph, visible vectors."""
import importlib.util, json, math, subprocess, shutil
from pathlib import Path
from PIL import Image,ImageDraw
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('old',HERE.parent/'video-2026-10-14-v5/build_video.py')
v=importlib.util.module_from_spec(spec);spec.loader.exec_module(v)
b=v.b;txt=b.txt;font=b.font;icon=v.icon
BLUE=b.BLUE;PURPLE=b.PURPLE;GREEN=b.GREEN;INK=b.INK;MUTED=b.MUTED;AMBER=b.AMBER
BG='#f5f8fc';LINE='#d5e2ef'
SCENES=json.loads((HERE/'storyboard.json').read_text(encoding='utf-8'))
ICONS=['file','convert','graph','ai','file','check','vector','graph','lock']
POS={'doc':(570,430),'heading':(780,335),'line':(740,620),'story':(1030,420),'fr':(1240,620),'code':(1600,440),'test':(1600,790),'arch':(1790,340),'matrix':(1790,850)}
R={'doc':43,'heading':43,'line':43,'story':56,'fr':64,'code':59,'test':59,'arch':42,'matrix':42}
LABEL={'doc':'User\nstories','heading':'Story\nheading','line':'Lifecycle\ntext','story':'US-08','fr':'FR-08','code':'Referral\nstate code','test':'Lifecycle\ntests','arch':'Architecture','matrix':'Traceability'}
COL={'doc':BLUE,'heading':BLUE,'line':BLUE,'story':BLUE,'fr':PURPLE,'code':GREEN,'test':'#ba6a26','arch':GREEN,'matrix':'#ba6a26'}
def clamp(x):return max(0,min(1,x))
def ease(x):x=clamp(x);return x*x*(3-2*x)
def point(k,z):
 x,y=POS[k];return (x+(480+(x-480)*.43-x)*z,y+(350+(y-350)*.75-y)*z)
def node(d,k,a=1,z=0):
 if a<=0:return
 x,y=point(k,z);rr=R[k]*(1-.2*z)*(.55+.45*ease(a));color=COL[k]
 d.ellipse((x-rr-5,y-rr-5,x+rr+5,y+rr+5),fill='#e5edf7')
 d.ellipse((x-rr,y-rr,x+rr,y+rr),fill='white',outline=color,width=3)
 if a<.65:return
 if k in ['doc','arch','matrix']:
  icon(d,x-13,y-25,'file',color,27)
  label={'doc':'DOCX','arch':'HTML','matrix':'XLSX'}[k]
  w=d.textlength(label,font=font(14,True));txt(d,(x-w/2,y+9),label,14,color,True)
 else:
  size=19 if z<.5 else 15;lines=LABEL[k].split('\n')
  for j,line in enumerate(lines):
   w=d.textlength(line,font=font(size,True));txt(d,(x-w/2,y-len(lines)*12+j*24),line,size,color,True)
 if z<.5 and k in ['story','fr','code','test']:
  s={'story':'User story','fr':'Requirement','code':'Code module','test':'Test code'}[k]
  w=d.textlength(s,font=font(17));txt(d,(x-w/2,y+R[k]+12),s,17,MUTED)
def edge(d,a,zkey,label,color,amount=1,z=0,dashed=False,t=0):
 if amount<=0:return
 aa=point(a,z);zz=point(zkey,z);dx=zz[0]-aa[0];dy=zz[1]-aa[1];ll=math.hypot(dx,dy)
 r1=R[a]*(1-.2*z)+4;r2=R[zkey]*(1-.2*z)+5
 start=(aa[0]+dx/ll*r1,aa[1]+dy/ll*r1);end=(zz[0]-dx/ll*r2,zz[1]-dy/ll*r2)
 b.arrow(d,start,end,color,progress=ease(amount),dashed=dashed,width=2 if dashed else 3)
 if amount>=1 and z<.5 and label:
  x=(start[0]+end[0])/2;y=(start[1]+end[1])/2-22;size=18;w=d.textlength(label,font=font(size,True))
  d.rounded_rectangle((x-w/2-5,y-1,x+w/2+5,y+25),6,fill=BG);txt(d,(x-w/2,y),label,size,color,True)
 if amount>=1 and not dashed:
  p=(t%4)/4;x=start[0]+(end[0]-start[0])*p;y=start[1]+(end[1]-start[1])*p
  d.ellipse((x-4,y-4,x+4,y+4),fill=color)
def box(d,r,title,detail,color=BLUE):
 d.rounded_rectangle(r,16,fill='white',outline=LINE,width=2)
 txt(d,(r[0]+20,r[1]+13),title,24,color,True,width=r[2]-r[0]-40)
 txt(d,(r[0]+20,r[1]+51),detail,22,INK,width=r[2]-r[0]-40)
def frame(i,t):
 p=t/SCENES[i]['duration'];im=Image.new('RGB',(1920,1080),BG);d=ImageDraw.Draw(im)
 d.rectangle((0,0,420,1080),fill='#142e48')
 txt(d,(28,28),'CAREPATH → DIGITAL BRAIN',24,'white',True)
 txt(d,(28,74),'ONE FILE. A GROWING GRAPH.',19,'#a9bfd3',True)
 for j,s in enumerate(SCENES):
  y=157+j*84;col='#78d9c9' if j<i else '#9fb6cc'
  if j==i:d.rounded_rectangle((16,y-10,405,y+64),14,fill='#284f72');col='white'
  icon(d,30,y+8,'check' if j<i else ICONS[j],col,27)
  txt(d,(75,y+3),f'{j+1:02}  '+s['title'],23,col,True)
  if j==i:txt(d,(76,y+36),'NOW EXPLAINING',13,'#a9ceff',True)
 txt(d,(28,963),'Actual CarePath demo documents',18,'#a9bfd3')
 txt(d,(28,995),'Focused graph view',18,'#a9bfd3')
 txt(d,(470,24),SCENES[i]['title'],42,INK,True)
 txt(d,(472,80),SCENES[i]['tech'],23,BLUE,True)
 # Source chips remain compact, and new files arrive only as introduced.
 count=1 if i<4 else 1+(t>1)+(t>20) if i==4 else 3
 names=['user-stories.docx','architecture.html','traceability-matrix.xlsx'];colors=[BLUE,GREEN,'#ba6a26']
 for k in range(count):
  x=470+k*472
  d.rounded_rectangle((x,133,x+449,204),12,fill='white',outline=colors[k],width=2)
  icon(d,x+13,152,'file',colors[k],28);txt(d,(x+57,144),names[k],23,colors[k],True)
  txt(d,(x+57,176),f'S{k+1}  •  source evidence',16,MUTED)
 # Only 45 pixels reserved for transcript; more room for the graph.
 d.rectangle((420,1034,1920,1080),fill='#17324d')
 elapsed=sum(s['duration'] for s in SCENES[:i])+t
 d.rectangle((420,1076,420+int(1500*elapsed/240),1080),fill='#54c7bb')
 msg='';detail=''
 if i==0:
  q=ease(t/2);x=1130;y=530+(1-q)*80
  icon(d,x-45,y-80,'file',BLUE,90)
  txt(d,(850,590),'Start with one Word document',36,INK,True)
  txt(d,(792,656),'Example: tracking a patient referral in CarePath',28,MUTED)
  msg='One real source file';detail='Follow user story US-08 and requirement FR-08 through the graph.'
 if i==1:
  icon(d,590,475,'file',BLUE,80)
  b.arrow(d,(710,515),(960,515),BLUE,progress=clamp((t-3)/2),width=4)
  if t>3:
   box(d,(990,346,1820,690),'Converted text: user-stories.docx','US-08 — Track a referral (FR-08)',BLUE)
   txt(d,(1013,478),'Permitted moves:',25,INK,True)
   txt(d,(1013,525),'draft → submitted → accepted',29,PURPLE,True)
   txt(d,(1013,573),'→ scheduled → completed',29,PURPLE,True)
   txt(d,(1013,635),'Source ID: S1    •    SHA-256 content fingerprint',21,MUTED)
  msg='MarkItDown turns file content into text';detail='The heading and lifecycle sentence retain their source identity.'
 if i>=2:
  shrink=ease(t/3) if i==6 else 1 if i>6 else 0
  # Retain each layer. Search later adds its own index next to this graph.
  structural=[('doc',0),('heading',7),('line',13)]
  for k,birth in structural:node(d,k,clamp((t-birth)/1.5) if i==2 else 1,shrink)
  for k,birth in [('heading',8),('line',14)]:edge(d,'doc',k,'contains',BLUE,clamp((t-birth)/2) if i==2 else 1,shrink,t=t)
  if i==2:
   msg='Normal graph: document → sections';detail='Arrows mean contains section. Selected nodes shown; tables also create records and values.'
  if i>=3:
   a=clamp((t-8)/2) if i==3 else 1;e=clamp((t-15)/2) if i==3 else 1
   node(d,'story',a,shrink);node(d,'fr',a,shrink)
   edge(d,'story','fr','references',AMBER if i<5 else GREEN,e,shrink,t=t)
   edge(d,'doc','story','evidence',BLUE,e,shrink,True,t)
   if i==3:
    txt(d,(515,245),'US-08',30,BLUE,True);txt(d,(650,245),'— Track a referral (',30,INK);txt(d,(937,245),'FR-08',30,PURPLE,True);txt(d,(1042,245),')',30,INK)
    msg='AI adds typed business meaning';detail='Illustrated AI proposal: US-08 references FR-08, carrying its source ID and exact quotation.'
   if i>=4:
    ac=clamp((t-8)/2) if i==4 else 1;ae=clamp((t-13)/2) if i==4 else 1
    tc=clamp((t-25)/2) if i==4 else 1;te=clamp((t-29)/2) if i==4 else 1
    node(d,'arch',clamp((t-1)/1.5) if i==4 else 1,shrink)
    node(d,'code',ac,shrink)
    edge(d,'fr','code','implemented in',AMBER if i<5 else GREEN,ae,shrink,t=t)
    edge(d,'arch','code','evidence',GREEN,ae,shrink,True,t)
    node(d,'matrix',clamp((t-20)/1.5) if i==4 else 1,shrink)
    node(d,'test',tc,shrink)
    edge(d,'test','fr','verifies',AMBER if i<5 else GREEN,te,shrink,t=t)
    edge(d,'matrix','test','evidence','#ba6a26',te,shrink,True,t)
    if i==4:
     q='Architecture: FR-08 → domain/referral_state.py' if t<20 else 'Traceability: TestLifecycleMap → verifies → FR-08'
     txt(d,(510,245),q,28,INK,True)
     msg='Each new file expands the same connected graph';detail='Code and test labels are shortened here; their original identifiers remain in the source.'
   if i==5:
    gates=['Source + exact quote','Names + allowed types','One shared FR-08']
    for k,s in enumerate(gates):
     if t>k*5:
      xx=490+k*465;icon(d,xx,245,'check',GREEN,27);txt(d,(xx+39,246),s,23,GREEN,True)
    if t>12:
     x,y=point('fr',0);rr=75+3*math.sin(t*2);d.ellipse((x-rr,y-rr,x+rr,y+rr),outline=GREEN,width=2)
    msg='Accepted links retain source evidence';detail='Matching names and types join one requirement. Human review still checks meaning.'
   if i>=6:
    txt(d,(484,263),'CONNECTED KNOWLEDGE',21,PURPLE,True)
    if i==6:
     box(d,(1130,245,1870,919),'07  SEARCH PREPARATION','Text → numerical vectors → stored search index',PURPLE)
     box(d,(1160,341,1835,433),'Relationship text','TestLifecycleMap verifies FR-08',BLUE)
     if t>5:
      b.arrow(d,(1488,442),(1488,489),PURPLE,progress=clamp((t-5)/1.5),width=3)
      txt(d,(1185,495),'OpenAI embedding',25,PURPLE,True)
      for k,h in enumerate([.2,.8,.45,.65,.9,.3,.55,.7,.25,.6,.4,.85]):
       xx=1180+k*48;hh=56*h*ease((t-6)/2);d.rounded_rectangle((xx,601-hh,xx+29,602),3,fill=PURPLE)
      txt(d,(1182,615),'[0.12, −0.34, 0.81, …]  →  stored vectors',24,PURPLE,True)
      txt(d,(1182,652),'512 dimensions • values illustrated',18,MUTED)
     if t>17:
      box(d,(1160,701,1835,799),'Later question','How do we test referral tracking?',BLUE)
     if t>23:
      txt(d,(1180,821),'Question vector → similar evidence',24,GREEN,True)
      txt(d,(1180,861),'Follow graph links → FR-08 + its tests',24,GREEN,True)
      x,y=point('fr',1);d.ellipse((x-64,y-64,x+64,y+64),outline=GREEN,width=4)
      x,y=point('test',1);d.ellipse((x-58,y-58,x+58,y+58),outline=GREEN,width=4)
     msg='Search gets a visible companion index';detail='Vectors support meaning-based retrieval. They do not create or prove graph facts.'
    if i==7:
     box(d,(1130,300,1860,851),'ORGANISE WHAT EXISTS','Graphify → NetworkX → Leiden themes',PURPLE)
     for k,(name,c) in enumerate([('Referral requirements',BLUE),('Implementation evidence',GREEN),('Test coverage','#ba6a26')]):
      yy=445+k*112;icon(d,1170,yy,'graph',c,36);txt(d,(1230,yy+1),name,28,c,True)
     txt(d,(1160,810),'Illustrative theme labels • draft saved in Django',21,MUTED)
     msg='The graph gains organisation and a saved revision';detail='Graphify groups existing connections for review; it does not invent new facts.'
    if i==8:
     box(d,(1130,300,1860,850),'REVIEWED KNOWLEDGE','One requirement connects three source files.',GREEN)
     for k,s in enumerate(['User story: US-08','Implementation: referral_state.py','Test: TestLifecycleMap']):
      yy=450+k*88;icon(d,1170,yy,'check',GREEN,30);txt(d,(1223,yy),s,27,INK,True)
     txt(d,(1170,740),'Evidence + source access + human review',24,GREEN,True)
     msg='Publish connected knowledge with traceable evidence';detail='Restricted sources are excluded. Retrieval rechecks current evidence and user access.'
 # Compact contextual explanation, separate from the one-line subtitle.
 d.rounded_rectangle((470,940,1870,1023),12,fill='white',outline=LINE,width=2)
 txt(d,(490,948),msg,24,INK,True)
 txt(d,(490,984),detail,20,MUTED,width=1360)
 return im

def main():
 for i,s in enumerate(SCENES):frame(i,s['duration']*.82).save(HERE/f'preview-{i:02d}.png')
 cmd=[b.FFMPEG,'-nostdin','-y','-v','error','-f','rawvideo','-pix_fmt','rgb24','-s','1920x1080','-r','12','-i','pipe:0','-i','narration.wav','-i','captions.srt','-i','chapters.ffmeta','-map','0:v','-map','1:a','-map','2:s','-map_metadata','3','-map_chapters','3','-vf','fps=24,ass=captions.ass','-t','240','-c:v','libx264','-preset','fast','-crf','19','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-c:s','mov_text','-disposition:s:0','0','-movflags','+faststart','carepath-growing-knowledge-graph.mp4']
 with (HERE/'encoding.log').open('w') as log:
  proc=subprocess.Popen(cmd,cwd=HERE,stdin=subprocess.PIPE,stderr=log)
  try:
   for i,s in enumerate(SCENES):
    print(f'Rendering {i+1}/9: {s["title"]}',flush=True)
    for n in range(s['duration']*12):proc.stdin.write(frame(i,n/12).tobytes())
   proc.stdin.close();assert proc.wait()==0
  except BaseException:proc.kill();proc.wait();raise
 frame(5,20).save(HERE/'poster.png')
 print('CarePath video complete',flush=True)
if __name__=='__main__':main()
