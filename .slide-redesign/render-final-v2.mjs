import fs from 'node:fs/promises';
import {FileBlob,PresentationFile} from '@oai/artifact-tool';
const p=await PresentationFile.importPptx(await FileBlob.load('C:/Workspace/digitalbrian/outputs/leadership-slide/Digital-Brain-Leadership-Redesigned-v2.pptx'));
console.log((await p.inspect({kind:'slide,textbox,image,layout',maxChars:15000})).ndjson);
const b=await p.export({slide:p.slides.items[0],format:'png',scale:1});
await fs.writeFile('C:/Workspace/digitalbrian/outputs/leadership-slide/Digital-Brain-Leadership-Redesigned-v2.png',new Uint8Array(await b.arrayBuffer()));
