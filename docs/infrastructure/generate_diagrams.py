"""Generate editable draw.io pages and matching SVG previews from the same cells."""
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring
from html import escape
import textwrap
import json
from urllib.parse import quote

OUT = Path(__file__).resolve().parent
OUT.mkdir(parents=True, exist_ok=True)
BRANDS = {m['id']: m for m in json.loads((OUT / 'icons.json').read_text(encoding='utf-8'))}
GLYPHS = {
 'server': '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6"/>',
 'network': '<circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="m10.5 7-4 8.5m7-8.5 4 8.5M7.5 18h9"/>',
 'users': '<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-17a3 3 0 0 1 0 6m1 4a5 5 0 0 1 3 4v3"/>',
 'shield': '<path d="m12 2 8 4v6c0 5-8 10-8 10S4 17 4 12V6z"/><path d="m8 12 3 3 5-6"/>',
 'key': '<circle cx="8" cy="15" r="4"/><path d="m11 12 9-9m-4 4 3 3m-6 0 3 3"/>',
 'certificate': '<path d="M14 2H5v20h14V7zM14 2v6h5"/><circle cx="12" cy="13" r="3"/><path d="m10 16-1 4 3-1 3 1-1-4"/>',
 'folder': '<path d="M3 6h6l2 3h10v11H3z"/><path d="M3 6V4h6l2 2h8v3"/>',
 'disk': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 4 18 4 18 0V5M3 12c0 4 18 4 18 0"/>',
 'workers': '<rect x="8" y="8" width="8" height="8" rx="2"/><path d="M9 2v3m6-3v3M9 19v3m6-3v3M2 9h3m-3 6h3m14-6h3m-3 6h3M9 11h6m-6 3h4"/>',
 'rocket': '<path d="M14 3 9 8l-1 8 8-1 5-5V3zM8 10H4l-2 5h6m6 1v6l5-2v-5M3 21l3-3"/><circle cx="16" cy="8" r="2"/>',
 'test': '<path d="M8 2h8M10 2v7L4 19c-1 2 0 3 2 3h12c2 0 3-1 2-3L14 9V2M7 15h10"/><path d="m9 18 2 2 4-4"/>',
 'toggle': '<rect x="2" y="6" width="20" height="12" rx="6"/><circle cx="16" cy="12" r="3"/>',
 'bucket': '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="m3 5 2 15c1 3 13 3 14 0l2-15M5 13c3 3 11 3 14 0"/>',
 'cloud': '<path d="M7 19a5 5 0 0 1-1-10 7 7 0 0 1 13-1 5.5 5.5 0 0 1-1 11z"/>',
 'trace': '<path d="M2 12h4l3-8 6 16 3-8h4"/>',
 'terminal': '<rect x="2" y="3" width="20" height="18" rx="3"/><path d="m6 8 4 4-4 4m7 0h5"/>',
 'plug': '<path d="M8 2v5m8-5v5M5 7h14v4a7 7 0 0 1-14 0zm7 11v4"/>',
 'code': '<path d="m8 5-6 7 6 7m8-14 6 7-6 7M14 3l-4 18"/>',
 'migration': '<ellipse cx="9" cy="5" rx="6" ry="3"/><path d="M3 5v13c0 4 12 4 12 0V5M3 11c0 3 9 4 12 0m2 2h6m-3-3 3 3-3 3"/>',
 'info': '<circle cx="12" cy="12" r="10"/><path d="M12 11v6m0-10v.01"/>',
}

def icon_svg(name, color):
 if name in BRANDS:
  m=BRANDS[name]
  return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {m["viewBox"]} {m["viewBox"]}"><path fill="#{m["hex"]}" d="{m["path"]}"/></svg>'
 return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><g fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{GLYPHS[name]}</g></svg>'

def presentation(c):
 """One shared icon/text layout for native draw.io and preview exports."""
 x,y,w,h=[c[k] for k in ('x','y','w','h')]
 icons=c.get('icons',[]); font=c['font']
 if c['group']:
  return {'icon_size':24,'icon_y':y+12,'icon_x':x+16,'text_x':x+(52 if icons else 16),'text_y':y+14,'align':'left','font':font}
 if h <= 65 and icons:
  return {'icon_size':26,'icon_y':y+(h-26)/2,'icon_x':x+16,'text_x':x+58,'text_y':y+(h-font)/2-1,'align':'left','font':font}
 size=32 if h<=100 else 40
 block=size+9+font+(19 if c['lines'] else 0)
 iy=y+(h-block)/2
 return {'icon_size':size,'icon_y':iy,'icon_x':x+(w-(len(icons)*size+max(0,len(icons)-1)*12))/2,'text_x':x+w/2,'text_y':iy+size+9,'align':'center','font':font}
C = {
 'blue': ('#EFF6FF','#2563EB'), 'green': ('#ECFDF5','#059669'),
 'purple': ('#F5F3FF','#7C3AED'), 'amber': ('#FFFBEB','#D97706'),
 'slate': ('#F8FAFC','#64748B'), 'rose': ('#FFF1F2','#E11D48')
}

class Page:
 def __init__(self, name, slug, w=2150, h=1640):
  self.name,self.slug,self.w,self.h=name,slug,w,h
  self.cells=[]; self.edges=[]
 def box(self,id,x,y,w,h,title,lines=(),kind='blue',parent=None,group=False,font=14):
  self.cells.append(dict(id=id,x=x,y=y,w=w,h=h,title=title,lines=list(lines),kind=kind,parent=parent,group=group,font=font))
  return id
 def edge(self,id,source,target,label='',kind='blue',sp='right',tp='left',via=(),at=None,dashed=False):
  self.edges.append(dict(id=id,source=source,target=target,label=label,kind=kind,sp=sp,tp=tp,via=list(via),at=at,dashed=dashed))
 def node(self,id): return next(c for c in self.cells if c['id']==id)
 def port(self,id,side):
  n=self.node(id); x,y,w,h=n['x'],n['y'],n['w'],n['h']
  return {'left':(x,y+h/2),'right':(x+w,y+h/2),'right_low':(x+w,y+h*.97),'top':(x+w/2,y),'top_web':(x+w*805/1385,y),'top_worker':(x+w*580/1385,y),'top_sandbox':(x+w*1165/1385,y),'bottom':(x+w/2,y+h),'bottom_left':(x+w*.25,y+h),'bottom_right':(x+w*.75,y+h)}[side]
 def path(self,e): return [self.port(e['source'],e['sp']),*e['via'],self.port(e['target'],e['tp'])]
 def midpoint(self,pts):
  lengths=[abs(b[0]-a[0])+abs(b[1]-a[1]) for a,b in zip(pts,pts[1:])]
  target=sum(lengths)/2
  for a,b,l in zip(pts,pts[1:],lengths):
   if target<=l and l: return (a[0]+(b[0]-a[0])*target/l,a[1]+(b[1]-a[1])*target/l)
   target-=l
  return pts[-1]
 def xml(self,mx):
  diagram=SubElement(mx,'diagram',id=self.slug,name=self.name)
  model=SubElement(diagram,'mxGraphModel',dx='1800',dy='1200',grid='1',gridSize='10',guides='1',tooltips='1',connect='1',arrows='1',fold='1',page='1',pageScale='1',pageWidth=str(self.w),pageHeight=str(self.h),math='0',shadow='0')
  root=SubElement(model,'root'); SubElement(root,'mxCell',id='0'); SubElement(root,'mxCell',id='1',parent='0')
  for c in self.cells:
   fill,stroke=C[c['kind']]
   value='<b>'+escape(c['title'])+'</b>'
   if c['lines']: value+='<br><span style="font-size:12px;color:#64748b">'+'<br>'.join(escape(s) for s in c['lines'])+'</span>'
   style=f'rounded=1;whiteSpace=wrap;html=1;fontFamily=Arial;fontSize={c["font"]};fontColor=#172554;fillColor={fill};strokeColor={stroke};strokeWidth=1.5;arcSize=12;spacing=12;'
   if c['group']: style+='verticalAlign=top;align=left;container=1;collapsible=0;recursiveResize=0;dashed=1;'
   lay=presentation(c)
   if c.get('icons'):
    # Leave real image cells above/beside the text; no external URLs or fonts.
    left=lay['text_x']-c['x'] if lay['align']=='left' else 0
    style+=f'spacing=0;verticalAlign=top;align={lay["align"]};spacingTop={lay["text_y"]-c["y"]};spacingLeft={left};spacingRight=8;'
   cell=SubElement(root,'mxCell',id=c['id'],value=value,style=style,vertex='1',parent=c['parent'] or '1')
   x,y=c['x'],c['y']
   if c['parent']:
    pn=self.node(c['parent']); x-=pn['x']; y-=pn['y']
   SubElement(cell,'mxGeometry',x=str(x),y=str(y),width=str(c['w']),height=str(c['h']),attrib={'as':'geometry'})
   for i,name in enumerate(c.get('icons',[])):
    uri='data:image/svg+xml,'+quote(icon_svg(name,stroke),safe='')
    mark=SubElement(root,'mxCell',id=f'icon-{c["id"]}-{i}',value='',style=f'shape=image;imageAspect=1;aspect=fixed;verticalLabelPosition=bottom;image={uri};',vertex='1',parent=c['id'])
    SubElement(mark,'mxGeometry',x=str(lay['icon_x']-c['x']+i*(lay['icon_size']+12)),y=str(lay['icon_y']-c['y']),width=str(lay['icon_size']),height=str(lay['icon_size']),attrib={'as':'geometry'})
  sides={'left':(0,.5),'right':(1,.5),'right_low':(1,.97),'top':(.5,0),'top_web':(805/1385,0),'top_worker':(580/1385,0),'top_sandbox':(1165/1385,0),'bottom':(.5,1),'bottom_left':(.25,1),'bottom_right':(.75,1)}
  for e in self.edges:
   sx,sy=sides[e['sp']]; tx,ty=sides[e['tp']]
   style=f'edgeStyle=none;rounded=1;html=1;endArrow=block;endFill=1;strokeWidth=2;strokeColor={C[e["kind"]][1]};fontColor=#334155;fontFamily=Arial;fontSize=12;labelBackgroundColor=#FFFFFF;exitX={sx};exitY={sy};entryX={tx};entryY={ty};'
   if e['dashed']: style+='dashed=1;'
   edge=SubElement(root,'mxCell',id=e['id'],value=e['label'],style=style,edge='1',parent='1',source=e['source'],target=e['target'])
   geo=SubElement(edge,'mxGeometry',relative='1',attrib={'as':'geometry'})
   if e['via']:
    ar=SubElement(geo,'Array',attrib={'as':'points'})
    for x,y in e['via']: SubElement(ar,'mxPoint',x=str(x),y=str(y))
   if e['at']:
    mid=self.midpoint(self.path(e)); x,y=e['at']
    SubElement(geo,'mxPoint',x=str(x-mid[0]),y=str(y-mid[1]),attrib={'as':'offset'})
 def svg(self):
  parts=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{escape(self.name)}">', '<rect width="100%" height="100%" fill="white"/>','<defs>']
  for k,(_,stroke) in C.items(): parts.append(f'<marker id="arrow-{k}" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto"><path d="M0 0 L9 4.5 L0 9Z" fill="{stroke}"/></marker>')
  parts.append('</defs>')
  # Boundary groups render first; relationships remain behind opaque service cards.
  def card(c):
   fill,stroke=C[c['kind']]; x,y,w,h=[c[k] for k in ('x','y','w','h')]
   dash=' stroke-dasharray="7 5"' if c['group'] else ''
   parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" stroke="{stroke}" stroke-width="1.6"{dash}/>')
   font=c['font']; wrap=max(12,int((w-28)/(font*.51))); lines=[]
   for s in c['lines']: lines.extend(textwrap.wrap(s,width=wrap) or [''])
   if c.get('icons'):
    lay=presentation(c)
    for i,name in enumerate(c['icons']):
     mark=icon_svg(name,stroke).replace('<svg ',f'<svg x="{lay["icon_x"]+i*(lay["icon_size"]+12)}" y="{lay["icon_y"]}" width="{lay["icon_size"]}" height="{lay["icon_size"]}" ',1)
     parts.append(mark)
    textx=lay['text_x']; start=lay['text_y']+font; anchor='start' if lay['align']=='left' else 'middle'
   elif c['group']: textx=x+16; start=y+27; anchor='start'
   else: textx=x+w/2; start=y+(h-(len(lines)*font*1.35+font*1.5))/2+font; anchor='middle'
   parts.append(f'<text x="{textx}" y="{start}" text-anchor="{anchor}" font-family="Arial, sans-serif" font-size="{font}" font-weight="700" fill="#172554">{escape(c["title"])}</text>')
   if lines:
    start+=19
    for i,s in enumerate(lines): parts.append(f'<text x="{textx}" y="{start+i*16}" text-anchor="{anchor}" font-family="Arial, sans-serif" font-size="12" fill="#64748B">{escape(s)}</text>')
  for c in self.cells:
   if c['group']: card(c)
  for e in self.edges:
   points=' '.join(f'{x},{y}' for x,y in self.path(e)); dash=' stroke-dasharray="7 5"' if e['dashed'] else ''
   parts.append(f'<polyline points="{points}" fill="none" stroke="{C[e["kind"]][1]}" stroke-width="2" stroke-linejoin="round" marker-end="url(#arrow-{e["kind"]})"{dash}/>')
  for c in self.cells:
   if not c['group']: card(c)
  for e in self.edges:
   if not e['label']: continue
   x,y=e['at'] or self.midpoint(self.path(e)); width=len(e['label'])*6.8+14
   parts.append(f'<rect x="{x-width/2}" y="{y-11}" width="{width}" height="22" rx="5" fill="white"/>')
   parts.append(f'<text x="{x}" y="{y+4}" text-anchor="middle" font-family="Arial, sans-serif" font-size="12" fill="#334155">{escape(e["label"])}</text>')
  parts.append('</svg>'); (OUT/f'{self.slug}.svg').write_text('\n'.join(parts),encoding='utf-8',newline='\n')

def banner(p,title,subtitle):
 p.box('title',35,20,p.w-70,75,title,[subtitle],kind='slate',font=19)

p=Page('01 · Default VPS demo','hostinger-vps-default')
banner(p,'DIGITAL BRAIN / HOSTINGER VPS DEMO','Configured at main df446fc · docker-compose.yaml · live VPS service inventory not verified')
p.box('host',340,115,1240,1420,'HOSTINGER VPS  /  200.141.11.23',['srv1874861.hstgr.cloud'],group=True,font=16)
p.box('network',370,350,1140,730,'PRIVATE DOCKER COMPOSE NETWORK',group=True,parent='host',font=14)
p.box('github',40,180,240,100,'GitHub repository',['ajitk15/digitalbrain','main / Dockerfile'],kind='slate')
p.box('controller',390,180,270,100,'Coolify deployment',['Fetch source · build · health checks'],parent='host')
p.box('image',790,180,420,100,'Digital Brain demo image',['Python 3.12 · locked app + CarePath dependencies'],parent='host')
p.box('visitors',40,440,240,105,'Browser / demo users',['ajitconnect.in','www.ajitconnect.in'],kind='slate')
p.box('proxy',400,440,220,105,'Coolify reverse proxy',['Public :80 / :443','TLS termination'],parent='network')
p.box('app',720,400,590,500,'app · role=all · uid 10001',['One container · internal port 8000'],group=True,parent='network')
p.box('web',750,480,225,95,'Django + Uvicorn',['UI · REST API · SSE','Sessions · access policy'],parent='app')
p.box('workers',1020,480,255,125,'Background worker lanes',['intake · graph · connectors','code-graph · factory · serviceops','serviceops-tests · automations','maintenance'],parent='app',font=12)
p.box('scan',750,630,225,90,'Local processing',['ClamAV / freshclam','Tesseract OCR · converters'],kind='green',parent='app',font=12)
p.box('agents',1020,635,255,90,'Agent runtime',['Claude Agent SDK / CLI','OpenAI Agents SDK'],kind='purple',parent='app',font=12)
p.box('tests',750,750,525,50,'Demo tools: Chromium / Playwright · CarePath pytest',[''],kind='slate',parent='app',font=12)
p.box('demo-flags',750,830,525,45,'Production security · demo reset / local writes / tests',kind='slate',parent='app',font=12)
p.box('postgres',750,930,245,100,'PostgreSQL 16',['Records · jobs · audit · graph rows','Restricted runtime role + tenant RLS'],kind='green',parent='network',font=12)
p.box('redis',1060,930,245,100,'Redis 7.4',['Answer streams · cancellation','API rate limits · transient data'],kind='green',parent='network',font=12)
p.box('secretsinit',400,700,230,110,'secrets-init · one shot',['Signing key + database passwords','Redis password + Redis config','Files only · owner permissions'],kind='amber',parent='network',font=12)
p.box('certinit',400,895,230,105,'postgres-certs · one shot',['Private CA + server certificates','Postgres and Redis verified TLS'],kind='amber',parent='network',font=12)
p.box('external-title',1700,350,370,65,'EXTERNAL SERVICES',['Configured separately from VPS containers'],kind='slate',font=14)
p.box('connectors',1730,465,310,125,'Connectors + Code Factory',['Jira · ServiceNow · GitHub / Git','Import records · read source · PR delivery','Per-application credentials'],kind='purple',font=13)
p.box('models',1730,640,310,100,'AI provider APIs',['Claude / Anthropic · OpenAI','Answers · graphs · embeddings'],kind='purple',font=13)
p.box('s3',1730,1210,310,120,'Amazon S3 originals',['digital-brain-originals-prod','Region: us-west-2','Access keys remain mounted files'],kind='purple',font=13)
p.box('volumes',370,1135,1140,330,'PERSISTENT NAMED VOLUMES  /  CONSUMERS',group=True,parent='host',font=15)
volumes=[
 ('postgres-data','Postgres database files'),('platform-secrets','App: signing / DB / Redis / S3 keys'),
 ('managed-credentials','App owners: provider credentials'),('demo-workspace','CarePath source + approved edits'),('clamav-db','App: antivirus signatures'),
 ('app-runtime','App: configuration + runtime data'),('postgres-certs','Postgres / Redis / app: TLS files'),
 ('database-secret','Postgres: password file copy'),('redis-config','Redis: TLS / password config'),
 ('connector-credentials','App: shared connector files (RO)')]
for i,(name,detail) in enumerate(volumes):
 p.box('volume-'+str(i),395+(i%5)*220,1205+(i//5)*115,210,90,name,[detail],kind='slate',parent='volumes',font=11)
p.box('baseline-note',35,1555,2040,65,'DEPLOYMENT SCOPE',['Solid = runtime traffic. Dashed = deployment / file relationships. Only the proxy publishes public ports. Redis stores no durable record.'],kind='slate',font=13)
p.edge('source-build','github','controller','Deploy main',kind='slate',at=(335,220),dashed=True)
p.edge('build-image','controller','image','Docker build',kind='slate',at=(725,220),dashed=True)
p.edge('run-image','image','app','Run demo target',kind='slate',sp='bottom',tp='top',via=[(1000,320),(1015,320)],at=(1120,320),dashed=True)
p.edge('ingress','visitors','proxy','HTTPS :443',at=(340,480))
p.edge('forward','proxy','web','HTTP :8000',at=(685,510))
p.edge('pg-tls','app','postgres','TLS :5432',kind='green',sp='bottom_left',tp='top',via=[(867.5,915),(872.5,915)],at=(805,915))
p.edge('redis-tls','app','redis','TLS :6379',kind='green',sp='bottom_right',tp='top',via=[(1162.5,915),(1182.5,915)],at=(1245,915))
p.edge('connector-https','workers','connectors','HTTPS APIs / Git',kind='purple',via=[(1450,542.5),(1450,527.5)],at=(1550,527.5))
p.edge('provider-https','agents','models','HTTPS model calls',kind='purple',via=[(1450,680),(1450,690)],at=(1550,690))
p.edge('mounts','app','volumes','Named volume mounts · inventory below',kind='slate',sp='bottom',tp='top',via=[(1015,1095),(940,1095)],at=(1040,1095),dashed=True)
p.edge('originals-s3','app','s3','HTTPS · originals',kind='purple',sp='right_low',tp='left',via=[(1550,885),(1550,1270)],at=(1660,1270))

q=Page('02 · Optional scaled services','hostinger-vps-scaled',2150,1470)
banner(q,'OPTIONAL / SPLIT WORKER DEPLOYMENT','docker-compose.yaml + docker-compose.scale.yaml · alternative to the default single app · activation not verified')
q.box('host',330,130,1490,1110,'HOSTINGER VPS / SAME PRIVATE COMPOSE NETWORK',group=True,font=16)
q.box('users',45,365,220,100,'Demo users',['ajitconnect.in'],kind='slate')
q.box('proxy',390,365,230,100,'Coolify proxy',['TLS termination :443'],parent='host')
q.box('web',790,365,310,130,'app · role=web',['Django / Uvicorn :8000','Configurable web replicas','No background lanes'],parent='host')
q.box('worker',790,655,310,155,'worker · role=worker',['graph · connectors · serviceops','serviceops-tests · automations','maintenance','Chromium / Playwright'],parent='host',font=13)
q.box('sandbox',1330,655,400,155,'sandbox · role=worker',['intake · code-graph · factory','Read-only root · no Linux capabilities','/tmp 4 GB · PID 512 · memory 6 GB','Credentials mounted read-only'],parent='host',font=13)
q.box('migrate',790,175,310,110,'migrate · one shot',['Schema / runtime role / demo seed','All serving processes wait for success'],kind='amber',parent='host',font=13)
q.box('backing',365,930,1385,260,'SHARED BACKING SERVICES / USED BY APP, WORKER AND SANDBOX',group=True,kind='green',parent='host',font=14)
q.box('postgres',390,995,310,115,'PostgreSQL 16',['Durable records + shared job queues','TLS :5432 · runtime role + RLS'],kind='green',parent='backing',font=13)
q.box('redis',810,995,310,115,'Redis 7.4',['Shared streams / Stop / rate limits','Verified TLS :6379'],kind='green',parent='backing',font=13)
q.box('clamd',1330,995,400,115,'clamd · ClamAV 1.4',['Signature updates + TCP scanner :3310','Own clamd-db volume'],kind='green',parent='backing',font=13)
q.box('shared',375,1290,750,120,'Shared data and files',['platform-secrets · managed-credentials · connector-credentials','app-runtime · demo-workspace · postgres-certs','postgres-data / database-secret / redis-config retain their service owners'],kind='slate',font=13)
q.box('external',1325,1290,690,120,'External dependencies remain unchanged',['Anthropic / OpenAI · Jira / ServiceNow / GitHub · Amazon S3','Bootstrap services secrets-init and postgres-certs remain in the base file.'],kind='purple',font=13)
q.edge('ingress','users','proxy','HTTPS :443',at=(330,398))
q.edge('forward','proxy','web','HTTP :8000',at=(705,400))
q.edge('migration-gate','migrate','web','Wait for migration success',kind='amber',sp='bottom',tp='top',dashed=True,at=(945,325))
q.edge('web-backing','web','backing','SQL / streams / scan',kind='green',sp='right',tp='top_web',via=[(1170,430)],at=(1170,550))
q.edge('worker-backing','worker','backing','SQL / streams / scan',kind='green',sp='bottom',tp='top_worker',at=(945,845))
q.edge('sandbox-backing','sandbox','backing','SQL / streams / scan',kind='green',sp='bottom',tp='top_sandbox',at=(1530,845))

r=Page('03 · Optional monitoring','hostinger-vps-monitoring',2150,1180)
banner(r,'OPTIONAL / OBSERVABILITY DEPLOYMENT','Add docker-compose.observability.yaml; for split workers also add docker-compose.observability.scale.yaml · activation not verified')
r.box('host',330,150,1480,810,'HOSTINGER VPS / OBSERVABILITY OVERLAY',group=True,font=16)
r.box('app',390,270,350,130,'Application processes',['app; worker + sandbox when split','Ids, timings, counters and costs','No document text or credentials'],parent='host',font=13)
r.box('collector',1000,270,350,130,'OpenTelemetry Collector',['OTLP / HTTP over TLS :4318','Metrics exporter :8889','Traces go to collector logs'],kind='purple',parent='host',font=13)
r.box('prometheus',1000,610,350,145,'Prometheus',['Scrape collector metrics','Evaluate SLO burn-rate rules','Persist metrics for 15 days'],kind='green',parent='host',font=13)
r.box('otelcerts',390,610,350,145,'otel-certs · one shot',['Issue collector certificate from private CA','Copy collector key / certificate only','No CA private key mounted to collector'],kind='amber',parent='host',font=13)
r.box('operator',1530,610,230,145,'Operator access',['127.0.0.1:9090','SSH tunnel or authenticated proxy'],kind='slate',parent='host',font=12)
r.box('volumes',390,835,960,85,'Persistent volumes',['otel-certs: collector certificate / key    ·    prometheus-data: metrics TSDB'],kind='slate',parent='host',font=13)
r.box('limitations',350,1010,1450,120,'Not supplied by this overlay',['No durable trace backend is configured; traces are logged by the collector.','No Alertmanager is configured; Prometheus evaluates alerts without external notification routing.'],kind='amber',font=14)
r.edge('otlp','app','collector','OTLP HTTPS :4318',kind='purple',at=(870,325))
r.edge('scrape','prometheus','collector','Scrape metrics :8889',kind='green',sp='top',tp='bottom',at=(1175,500))
r.edge('operator-access','operator','prometheus','HTTP · loopback only',kind='slate',sp='left',tp='right',at=(1440,650))
r.edge('certificate','otelcerts','collector','TLS certificate files',kind='amber',sp='top',tp='bottom_left',via=[(565,505),(1087.5,505)],at=(800,505),dashed=True)

p.box('autoscaling',1270,180,280,100,'Autoscaling: OFF',['Default app must stay at one container'],kind='amber',parent='host')
q.box('autoscaling',1270,180,460,100,'Autoscaling: OFF',['Manual replicas; no automatic controller or scaling rules'],kind='amber',parent='host')

inventory={}
for page in (p,q,r):
 inventory[page.slug]=[{'id':c['id'],'title':c['title'],'details':c['lines']} for c in page.cells]
(OUT/'component-details.json').write_text(json.dumps(inventory,indent=2)+'\n',encoding='utf-8',newline='\n')

def compact(page,id,title,subtitle='',icons=(),font=16):
 c=page.node(id); c.update(title=title,lines=[subtitle] if subtitle else [],icons=list(icons),font=font)

compact(p,'title','Digital Brain · Hostinger VPS','Configured stack · df446fc · live inventory unverified',font=27)
compact(p,'host','HOSTINGER VPS · 200.141.11.23',icons=['server'])
compact(p,'network','PRIVATE DOCKER NETWORK',icons=['network'])
compact(p,'github','GitHub','main',['github'])
compact(p,'controller','Coolify','Build & deploy',['rocket'])
compact(p,'image','Demo image','Python 3.12',['docker','python'])
compact(p,'visitors','Demo users','ajitconnect.in',['users'])
compact(p,'proxy','Coolify proxy',':80 / :443',['shield'])
compact(p,'app','DIGITAL BRAIN · app','role=all · 1 container',['docker'])
compact(p,'autoscaling','Autoscaling: OFF','Single app only',['info'])
compact(p,'web','Django / Uvicorn','UI · API · SSE',['django'])
compact(p,'workers','Workers','9 lanes',['workers'])
compact(p,'scan','File processing','ClamAV · OCR',['shield'])
compact(p,'agents','Agent SDKs','Claude · OpenAI',['claude','openai'])
compact(p,'tests','Chromium · Playwright · pytest',icons=['test'],font=15)
compact(p,'demo-flags','Demo reset · write · test',icons=['toggle'],font=15)
compact(p,'postgres','PostgreSQL 16','Records · jobs · audit',['postgresql'])
compact(p,'redis','Redis 7.4','Streams · rate limits',['redis'])
compact(p,'secretsinit','secrets-init','One-time credentials',['key'])
compact(p,'certinit','postgres-certs','One-time TLS setup',['certificate'])
compact(p,'external-title','EXTERNAL SERVICES',icons=['cloud'],font=16)
compact(p,'connectors','Connectors','Jira · ServiceNow · GitHub',['jira','plug','github'])
compact(p,'models','AI providers','Claude · OpenAI',['claude','openai'])
compact(p,'s3','Amazon S3','Originals · us-west-2',['bucket'])
compact(p,'volumes','PERSISTENT VOLUMES',icons=['disk'])
for i,(name,_) in enumerate(volumes):
 icon='key' if name in ('platform-secrets','managed-credentials','database-secret','connector-credentials') else 'certificate' if name=='postgres-certs' else 'shield' if name=='clamav-db' else 'folder' if name=='demo-workspace' else 'disk' if name=='postgres-data' else 'code'
 compact(p,'volume-'+str(i),name,icons=[icon],font=13)
compact(p,'baseline-note','Solid: traffic   ·   Dashed: deployment / files','Public entry: proxy only',font=15)
for edge in p.edges:
 edge['label']={'Deploy main':'Deploy','Docker build':'Build','Run demo target':'Run','HTTPS APIs / Git':'HTTPS / Git','HTTPS model calls':'HTTPS','Named volume mounts · inventory below':'Volumes','HTTPS · originals':'HTTPS'}.get(edge['label'],edge['label'])

compact(q,'title','Optional · Manual scaling','Scale overlay · activation unverified',font=27)
compact(q,'host','HOSTINGER VPS · PRIVATE NETWORK',icons=['server'])
compact(q,'users','Demo users','ajitconnect.in',['users'])
compact(q,'proxy','Coolify proxy',':443',['shield'])
compact(q,'web','Web app','Manual replicas · default 1',['django'])
compact(q,'worker','Workers','Manual replicas · 6 lanes',['workers'])
compact(q,'sandbox','Sandbox','Manual replicas · 3 lanes',['shield','code'])
compact(q,'autoscaling','Autoscaling: OFF','No scaling controller / rules',['info'])
compact(q,'migrate','Migrations','One-time setup',['migration'])
compact(q,'backing','SHARED SERVICES · SINGLE INSTANCES',icons=['network'])
compact(q,'postgres','PostgreSQL 16','TLS :5432',['postgresql'])
compact(q,'redis','Redis 7.4','TLS :6379',['redis'])
compact(q,'clamd','ClamAV','TCP :3310',['shield'])
compact(q,'shared','Shared volumes','Data · workspace · credentials',['disk','folder','key'])
compact(q,'external','External services','AI · connectors · S3',['cloud'])
for edge in q.edges:
 edge['label']={'Wait for migration success':'Ready','SQL / streams / scan':'Data / scan'}.get(edge['label'],edge['label'])

compact(r,'title','Optional · Monitoring','Activation unverified · no autoscaling',font=27)
compact(r,'host','HOSTINGER VPS · PRIVATE NETWORK',icons=['server'])
compact(r,'app','App / workers','Traces · metrics',['docker'])
compact(r,'collector','OTEL Collector','OTLP TLS :4318',['trace'])
compact(r,'prometheus','Prometheus','Metrics · alerts · 15 days',['prometheus'])
compact(r,'otelcerts','otel-certs','One-time TLS setup',['certificate'])
compact(r,'operator','Operator','SSH / 127.0.0.1:9090',['terminal'])
compact(r,'volumes','otel-certs   ·   prometheus-data',icons=['disk'],font=15)
compact(r,'limitations','Traces → collector logs','No trace storage / Alertmanager',['info'])
for edge in r.edges:
 edge['label']={'OTLP HTTPS :4318':'OTLP / TLS :4318','Scrape metrics :8889':'Metrics :8889','HTTP · loopback only':'Loopback','TLS certificate files':'TLS files'}.get(edge['label'],edge['label'])

mx=Element('mxfile',host='app.diagrams.net',agent='Codex',version='24.7.17',type='device')
for page in (p,q,r): page.xml(mx); page.svg()
(OUT/'hostinger-vps.drawio').write_bytes(tostring(mx,encoding='utf-8',xml_declaration=True))
print('Created three editable draw.io pages and matching SVG previews.')
