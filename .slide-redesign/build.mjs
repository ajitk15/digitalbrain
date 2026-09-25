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
txt('Digital Brain',40,24,800,48,38,C.navy,true);
txt('ARCHITECTURE',40,83,220,21,13,C.teal,true);
line(40,111,1200);
// Editable architecture, arranged as three stages.
txt('Connect',40,123,150,27,21,C.navy,true);
txt('pluggable connectors',40,151,160,19,12,C.muted);
const cons=['Upload & links','SharePoint OneDrive','GitHub','Jira','ServiceNow','Add more'];
for(let i=0;i<6;i++){await icon(i+1,40,184+i*27,19);txt(cons[i],68,182+i*27,143,23,13,C.ink,i===0);}
txt('→',212,121,26,32,26,C.teal,true,'center');
txt('Ingest',244,123,160,27,21,C.navy,true);
txt('safe & traceable',244,151,180,19,12,C.muted);
const ingest=[['Secure upload','held privately'],['Security scan','unsafe files blocked'],['To Markdown','converted offline'],['Knowledge','version controlled']];
for(let i=0;i<4;i++){
 const y=180+i*43;
 txt(ingest[i][0],244,y,180,21,14,C.navy,true);
 txt(ingest[i][1],244,y+21,180,15,11.5,C.muted);
 if(i<3)txt('↓',418,y+28,17,20,15,C.teal,true,'center');
}
txt('→',442,121,26,32,26,C.teal,true,'center');
txt('Know & Act',486,123,205,27,21,C.navy,true);
txt('sealed per application',702,127,235,23,12,C.muted);
const x=486;
function pipeline(labels,y,color,fill){
 const gap=14, width=(754-gap*(labels.length-1))/labels.length;
 labels.forEach((label,i)=>{
  txt(label,x+i*(width+gap),y,width,31,12,color,true,'center',fill);
  if(i<labels.length-1)txt('→',x+width+i*(width+gap),y,14,31,13,C.teal,false,'center');
 });
}
txt('CODE FACTORY',x,167,150,20,13,C.teal,true);
txt('USES',x,191,44,20,10,C.muted,true);
txt('Project docs\nknowledge graph',539,188,193,32,12,C.ink);
txt('Code\ncode graph',773,188,153,32,12,C.ink);
txt('Bug\nfrom ticket',1051,188,160,32,12,C.ink);
txt('RUNS',x,224,48,16,10,C.muted,true);
pipeline(['Pre-checks','Analysis','Approve gaps','Agents','Pull request','Tests','Re-sync'],242,C.navy,C.blue);
await icon(7,722,223,14);
txt('SERVICEOPS',x,286,150,20,13,C.teal,true);
txt('USES',x,309,44,20,10,C.muted,true);
txt('Knowledge\napp graph',539,306,145,32,12,C.ink);
txt('History\npast incidents',713,306,150,32,12,C.ink);
txt('Changes\nsame CI',898,306,146,32,12,C.ink);
txt('Incident\nnew ticket',1084,306,150,32,12,C.ink);
txt('RUNS',x,341,46,16,10,C.muted,true);
pipeline(['Evidence','Redact','Hypotheses','Verify & score','Responder decides'],359,C.teal,C.light);
await icon(7,1187,337,14);
txt('results update the knowledge',x,394,754,17,11.5,C.teal,false,'right');
// A return connector closes the knowledge loop for both applications.
s.shapes.add({geometry:'line',position:{left:425,top:333,width:44,height:0},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:469,top:333,width:0,height:70},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:469,top:403,width:550,height:0},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:1249,top:258,width:0,height:145},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:1240,top:258,width:9,height:0},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:1240,top:375,width:9,height:0},fill:'none',line:{fill:C.teal,width:1}});
s.shapes.add({geometry:'line',position:{left:1240,top:403,width:9,height:0},fill:'none',line:{fill:C.teal,width:1}});
txt('←',422,323,20,20,15,C.teal);
txt('FORMATS',40,359,86,19,10,C.teal,true);
txt('PDF · Word · Excel · PowerPoint · Outlook ·\nHTML · CSV · JSON · XML · Text',40,380,397,33,11,C.muted);
line(40,425,1200);
await icon(8,40,434,16);
txt('Access',65,431,57,21,13,C.teal,true);
txt('Chat · REST API · MCP',126,431,184,21,13,C.navy,true);
txt('for people and any AI tool',320,431,264,21,12,C.muted);
txt('OpenAI or Claude',1090,431,150,21,12,C.muted,false,'right');
line(40,460,1200);
txt('KEY FEATURES',40,473,280,22,13,C.teal,true);
const features=[
 ['Organizations:','portfolio → product → application, access per app'],
 ['Graph management:','source → graph → quality check → publish'],
 ['Code Factory:','ticket → plan → approval → draft PR → Re-sync Knowledge'],
 ['ServiceOps:','incidents open with past fixes and recent changes'],
 ['Guided onboarding:','Onboarding checklist per application'],
 ['Usage tracking:','AI cost per call, per application'],
 ['Connectors:','Jira, ServiceNow and GitHub synced on a schedule']];
features.forEach(([a,b],i)=>{const t=txt('',40,502+i*22,548,21,12.2,C.ink);t.text=[[{run:a+'  ',textStyle:{bold:true,color:C.navy}},{run:b}]];});
txt('KEY DIFFERENTIATORS',625,473,615,22,13,C.teal,true);
const diff=[
 ['Answers that prove themselves','Every answer is traceable to a verified source.'],
 ['One brain for build and run','Every resolved incident strengthens the next response.'],
 ['Knowledge released like software','All knowledge is reviewed and approved before use.'],
 ['Always up to date','Sources and knowledge refresh after major changes, like a bug fix.'],
 ['Requirements and code in one graph','Every change is linked to its requirement and code.'],
 ['Bring your own model','Trusted knowledge is accessible from any AI tool.']];
diff.forEach(([a,b],i)=>{const xx=i<3?625:947, yy=502+(i%3)*51;txt(a,xx,yy,293,20,12.7,C.navy,true);txt(b,xx,yy+20,293,30,11.8,C.muted);});
line(40,668,1200);
txt('TECH STACK',40,682,105,17,10.5,C.teal,true);
txt('CORE',162,682,42,17,9.5,C.muted,true);
txt('Python · Django · PostgreSQL',205,680,223,22,11.5,C.navy);
txt('AI & GRAPH',437,682,74,17,9.5,C.muted,true);
txt('OpenAI Agents SDK · Claude Agent SDK · Graphify',518,680,391,22,11.5,C.navy);
txt('SUPPORTING',934,682,80,17,9.5,C.muted,true);
txt('MarkItDown · MCP · Docker',1018,680,222,22,11.5,C.navy);
const orig=await PresentationFile.importPptx(await FileBlob.load('C:/Users/ajitk/Downloads/Digital-Brain-Leadership-Slide-v3.pptx'));
if(orig.slides.items[0].speakerNotes?.textFrame?.text) s.speakerNotes.textFrame.setText(orig.slides.items[0].speakerNotes.textFrame.text);
await(await PresentationFile.exportPptx(p)).save(dir+'/candidate.pptx');
const preview=await p.export({slide:s,format:'png',scale:1.5});
await fs.writeFile(dir+'/preview.png',new Uint8Array(await preview.arrayBuffer()));
await fs.writeFile(dir+'/layout.json',await(await s.export({format:'layout'})).text());
console.log('Draft and preview created');
if(process.argv.includes('--finalize')){
console.log(await finalizePresentation({workspaceDir:root,candidatePath:dir+'/candidate.pptx',finalPath:root+'/outputs/leadership-slide/Digital-Brain-Leadership-Redesigned.pptx',pythonExecutable:'C:/Users/ajitk/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:1,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:['Arial']},verifyArtifactToolImport:true,receiptPath:dir+'/validation.json'}));
}
