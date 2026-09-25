import fs from 'node:fs/promises';
import {Presentation,PresentationFile,FileBlob} from '@oai/artifact-tool';
import {finalizePresentation} from 'file:///C:/Users/ajitk/.codex/plugins/cache/openai-primary-runtime/presentations/26.915.20218/skills/presentations/container_tools/artifact_tool_utils.mjs';
const root='C:/Workspace/digitalbrian';
const dir=root+'/.slide-redesign';
const skill='C:/Users/ajitk/.codex/plugins/cache/openai-primary-runtime/presentations/26.915.20218/skills/presentations';
const p=Presentation.create({slideSize:{width:1280,height:720}});
const s=p.slides.add(); s.background.fill='#FFFFFF';
const C={navy:'#132D46',teal:'#087F83',ink:'#283D50',muted:'#607180',light:'#EDF5F6',line:'#D2DFE5',blue:'#EAF0F6'};
function txt(t,x,y,w,h,size=14,color=C.ink,bold=false,align='left',fill='none'){
 const a=s.shapes.add({geometry:'textbox',name:t.slice(0,50),position:{left:x,top:y,width:w,height:h},fill,line:{fill:'none',width:0}});
 a.text=t; a.text.style={typeface:'Arial',fontSize:size,color,bold,alignment:align,verticalAlignment:'middle',autoFit:'none',wrap:'square',insets:{left:0,right:0,top:0,bottom:0}}; return a;
}
function line(x,y,w,color=C.line){s.shapes.add({geometry:'line',position:{left:x,top:y,width:w,height:0},fill:'none',line:{fill:color,width:1}});}
async function icon(n,x,y,size=20){s.images.add({blob:new Uint8Array(await fs.readFile(dir+'/image'+n+'.png')),contentType:'image/png',position:{left:x,top:y,width:size,height:size},fit:'contain'});}
txt('Digital Brain',36,22,820,49,38,C.navy,true);
txt('ARCHITECTURE',36,88,280,22,14,C.teal,true);
function box(x,y,w,h,fill=C.light,stroke=C.line){return s.shapes.add({geometry:'rect',position:{left:x,top:y,width:w,height:h},fill,line:{fill:stroke,width:1}});}
function seg(x1,y1,x2,y2,color=C.teal,dashed=false){return s.shapes.add({geometry:'line',position:{left:Math.min(x1,x2),top:Math.min(y1,y2),width:Math.abs(x2-x1),height:Math.abs(y2-y1)},fill:'none',line:{fill:color,width:2,style:dashed?'dashed':'solid'}});}
function anchor(x,y){return box(x-.05,y-.05,.1,.1,'none','none');}
function connect(a,b,from='right',to='left',color=C.teal,arrow=true){const c=s.shapes.connect(a,b,{kind:'straight',fromSide:from,toSide:to,line:{fill:color,width:2},tail:{type:arrow?'triangle':'none',width:'sm',length:'sm'}});c.bringToFront();return c;}
function arrow(x1,y1,x2,y2,color=C.teal){const a=anchor(x1,y1),b=anchor(x2,y2);return connect(a,b,x2>x1?'right':x2<x1?'left':y2>y1?'bottom':'top',x2>x1?'left':x2<x1?'right':y2>y1?'top':'bottom',color);}
txt('Connect',36,154,106,26,18,C.navy,true);
txt('pluggable\nconnectors',36,181,100,32,12,C.muted);
const cons=['Upload & links','SharePoint\nOneDrive','GitHub','Jira','ServiceNow','Add more'];
for(let i=0;i<6;i++){
 const x=153+i*116;
 box(x,129,105,79,'#FFFFFF');await icon(i+1,x+41,139,24);
 txt(cons[i],x+4,170,97,29,12.3,C.navy,true,'center');
 seg(x+52.5,208,x+52.5,222);
}
seg(205.5,222,785.5,222);
txt('Ingest',36,256,106,26,18,C.navy,true);
txt('safe &\ntraceable',36,285,108,31,12,C.muted);
const ingest=[['Secure upload','held privately'],['Security scan','unsafe files blocked'],['To Markdown','converted offline'],['Knowledge','version controlled']];
const ingNodes=[];
ingest.forEach(([a,b],i)=>{
 const x=153+i*179;ingNodes.push(box(x,248,160,62,i===3?C.navy:C.blue,i===3?C.navy:C.line));
 txt(a,x+5,257,150,23,15,i===3?'#FFFFFF':C.navy,true,'center');
 txt(b,x+5,282,150,18,12,i===3?'#D8E7ED':C.muted,false,'center');
});
connect(anchor(233,222),ingNodes[0],'bottom','top');
for(let i=0;i<3;i++)connect(ingNodes[i],ingNodes[i+1]);
txt('FORMATS',153,317,75,17,10,C.teal,true);
txt('PDF · Word · Excel · PowerPoint · Outlook · HTML · CSV · JSON · XML · Text',227,317,624,17,10.5,C.muted);
txt('Know &\nAct',36,425,107,50,18,C.navy,true);
txt('sealed per\napplication',36,479,106,32,12,C.muted);
// Shared knowledge distribution is independent of the result return path.
seg(770,310,770,341);seg(140,341,770,341);seg(140,341,140,532);
function lane(y,name,inputs,steps,teal){
 const color=teal?C.teal:C.navy, inputFill=teal?'#DCF0ED':'#DCE7F2';
 box(153,y,698,124,'none',C.line);
 txt(name,165,y+4,240,21,14,color,true);
 txt('USES',165,y+27,42,17,11,color,true);
 const iw=(629-14*(inputs.length-1))/inputs.length, inputNodes=[];
 inputs.forEach(([a,b],i)=>{
  const xx=212+i*(iw+14);inputNodes.push(box(xx,y+27,iw,43,inputFill,teal?'#90BDB6':'#A5B9CC'));
  txt(a,xx+5,y+29,iw-10,21,14,color,true,'center');
  txt(b,xx+5,y+50,iw-10,17,12,C.ink,false,'center');
 });
 // Every input joins one bus, which enters the first workflow step.
 const sw=(629-18*(steps.length-1))/steps.length, runNodes=[];
 txt('RUNS',165,y+96,42,17,11,color,true);
 steps.forEach((a,i)=>{
  const xx=212+i*(sw+18);runNodes.push(box(xx,y+89,sw,31,teal?C.teal:C.navy,'none'));
  txt(a,xx+2,y+90,sw-4,29,12.2,'#FFFFFF',true,'center');
 });
 for(let i=0;i<steps.length-1;i++)connect(runNodes[i],runNodes[i+1],'right','left',color);
 const first=212+sw/2,lastInput=212+(inputs.length-1)*(iw+14)+iw/2;
 inputNodes.forEach((n,i)=>seg(212+i*(iw+14)+iw/2,y+70,212+i*(iw+14)+iw/2,y+79,color));
 seg(first,y+79,lastInput,y+79,color);
 connect(anchor(first,y+79),runNodes[0],'bottom','top',color);
 connect(anchor(140,y+49),inputNodes[0],'right','left',C.teal);
 return {last:runNodes.at(-1),endY:y+104.5};
}
const code=lane(351,'CODE FACTORY',[['Project docs','knowledge graph'],['Code','code graph'],['Bug','from ticket']],['Pre-checks','Analysis','Approve gaps','Agents','Pull request','Tests','Re-sync'],false);
const ops=lane(483,'SERVICEOPS',[['Knowledge','app graph'],['History','past incidents'],['Changes','same CI'],['Incident','new ticket']],['Evidence','Redact','Hypotheses','Verify & score','Responder decides'],true);
// Amber distinguishes results returning to version-controlled knowledge.
const feedback='#A66A22';
connect(code.last,anchor(875,code.endY),'right','left',feedback,false);
connect(ops.last,anchor(875,ops.endY),'right','left',feedback,false);
seg(875,279,875,ops.endY,feedback,true);
connect(anchor(875,279),ingNodes[3],'left','right',feedback,true);
txt('results update the knowledge',577,609,274,18,11.5,feedback,false,'right');
await icon(7,309,355,14);await icon(7,282,487,14);
await icon(8,155,635,18);
txt('Access',183,632,57,24,13,C.teal,true);
txt('Chat · REST API · MCP',246,632,195,24,13,C.navy,true);
txt('for people and any AI tool',450,632,240,24,12,C.muted);
txt('OpenAI or Claude',36,633,111,24,11,C.muted);
line(153,630,698);line(153,658,698);

txt('KEY FEATURES',914,88,330,22,14,C.teal,true);
const features=[
 ['Organizations:','portfolio → product → application, access per app'],
 ['Graph management:','source → graph → quality check → publish'],
 ['Code Factory:','ticket → plan → approval → draft PR → Re-sync Knowledge'],
 ['ServiceOps:','incidents open with past fixes and recent changes'],
 ['Guided onboarding:','Onboarding checklist per application'],
 ['Usage tracking:','AI cost per call, per application'],
 ['Connectors:','Jira, ServiceNow and GitHub synced on a schedule']];
features.forEach(([a,b],i)=>{const t=txt('',914,123+i*33,330,32,11.6,C.ink);t.text=[[{run:a+'  ',textStyle:{bold:true,color:C.navy}},{run:b}]];});
line(914,362,330);
txt('KEY DIFFERENTIATORS',914,373,330,22,14,C.teal,true);
const diff=[
 ['Answers that prove themselves','Every answer is traceable to a verified source.'],
 ['One brain for build and run','Every resolved incident strengthens the next response.'],
 ['Knowledge released like software','All knowledge is reviewed and approved before use.'],
 ['Always up to date','Sources and knowledge refresh after major changes, like a bug fix.'],
 ['Requirements and code in one graph','Every change is linked to its requirement and code.'],
 ['Bring your own model','Trusted knowledge is accessible from any AI tool.']];
diff.forEach(([a,b],i)=>{const yy=397+i*44;txt(a,914,yy,330,18,12.2,C.navy,true);txt(b,914,yy+18,330,25,11.2,C.muted);});
line(36,669,1208);
txt('TECH STACK',36,682,105,17,10.5,C.teal,true);
txt('CORE',162,682,42,17,9.5,C.muted,true);
txt('Python · Django · PostgreSQL',205,680,223,22,11.5,C.navy);
txt('AI & GRAPH',437,682,74,17,9.5,C.muted,true);
txt('OpenAI Agents SDK · Claude Agent SDK · Graphify',518,680,391,22,11.5,C.navy);
txt('SUPPORTING',934,682,80,17,9.5,C.muted,true);
txt('MarkItDown · MCP · Docker',1018,680,222,22,11.5,C.navy);

const orig=await PresentationFile.importPptx(await FileBlob.load('C:/Users/ajitk/Downloads/Digital-Brain-Leadership-Slide-v3.pptx'));
if(orig.slides.items[0].speakerNotes?.textFrame?.text) s.speakerNotes.textFrame.setText(orig.slides.items[0].speakerNotes.textFrame.text);
await(await PresentationFile.exportPptx(p)).save(dir+'/candidate-v3.pptx');
const preview=await p.export({slide:s,format:'png',scale:1.5});
await fs.writeFile(dir+'/preview-v3.png',new Uint8Array(await preview.arrayBuffer()));
await fs.writeFile(dir+'/layout-v3.json',await(await s.export({format:'layout'})).text());
console.log('Draft and preview created');
if(process.argv.includes('--finalize')){
console.log(await finalizePresentation({workspaceDir:root,candidatePath:dir+'/candidate-v3.pptx',finalPath:root+'/outputs/leadership-slide/Digital-Brain-Leadership-Redesigned-v3.pptx',pythonExecutable:'C:/Users/ajitk/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:1,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:['Arial']},verifyArtifactToolImport:true,receiptPath:dir+'/validation-v3.json'}));
}
