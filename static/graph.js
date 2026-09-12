(() => {
  const raw = document.getElementById("knowledge-graph-data");
  if (!raw) return;
  const graph = JSON.parse(raw.textContent), nodes = new Map(graph.nodes.map(n => [n.id, n]));
  const canvas = document.getElementById("graph-canvas"), select = document.getElementById("graph-node");
  const search = document.getElementById("graph-search"), inspector = document.getElementById("graph-inspector");
  const ns = "http://www.w3.org/2000/svg", colors = {document:"#4145c8",record:"#8a53bc",value:"#258777",section:"#7b879b",entity:"#c57624"};
  function element(tag, attrs, text) {
    const el = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k,v]) => el.setAttribute(k,v));
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function options() {
    const term = search.value.toLowerCase(); select.replaceChildren();
    const prompt = document.createElement("option"); prompt.value=""; prompt.textContent="Select a node…"; select.append(prompt);
    graph.nodes.filter(n => n.label.toLowerCase().includes(term)).forEach(n => {
      const option = document.createElement("option"); option.value=n.id; option.textContent=n.kind+": "+n.label; select.append(option);
    });
  }
  function inspect(id) {
    inspector.replaceChildren();
    const node=nodes.get(id), title=document.createElement("h2"); title.textContent=node.label; inspector.append(title);
    const kind=document.createElement("p"); kind.textContent=node.kind; inspector.append(kind);
    const matches=graph.edges.filter(e=>e.source===id||e.target===id);
    const count=document.createElement("p"); count.textContent=matches.length+" relationship(s). Showing up to 30 evidence entries."; inspector.append(count);
    matches.slice(0,30).forEach(e=>{
      const block=document.createElement("div"); block.className="graph-evidence";
      const relation=document.createElement("strong"); relation.textContent=e.relation; block.append(relation);
      const link=document.createElement("a"); link.textContent="View source · line "+e.line;
      link.href="../knowledge/"+e.knowledge_id+"/"; block.append(link);
      const quote=document.createElement("pre"); quote.textContent=e.evidence; block.append(quote); inspector.append(block);
    });
  }
  function draw(focus) {
    let chosen;
    if (focus) {
      const ids=new Set([focus]); graph.edges.forEach(e=>{if(e.source===focus) ids.add(e.target); if(e.target===focus) ids.add(e.source);});
      chosen=Array.from(ids).slice(0,80).map(id=>nodes.get(id));
      inspect(focus);
    } else {
      const docs=graph.nodes.filter(n=>n.kind==="document");
      const ids=new Set(docs.map(n=>n.id));
      docs.forEach(doc=>{graph.edges.filter(e=>e.source===doc.id).sort((a,b)=>(nodes.get(b.target)?.kind==="entity")-(nodes.get(a.target)?.kind==="entity")).slice(0,Math.max(1,Math.floor((80-docs.length)/Math.max(1,docs.length)))).forEach(e=>{if(ids.size<80)ids.add(e.target);});});
      chosen=Array.from(ids).slice(0,80).map(id=>nodes.get(id));
    }
    canvas.replaceChildren(); const positions=new Map();
    const inner=chosen.filter(n=>n.kind==="document"&&!focus);
    const outer=chosen.filter(n=>n.id!==focus&&!inner.includes(n));
    chosen.forEach((n)=>{
      const ring=inner.includes(n)?inner:outer;
      const angle=2*Math.PI*Math.max(0,ring.indexOf(n))/Math.max(ring.length,1);
      const r=n.id===focus?0:(inner.includes(n)?90:215);
      positions.set(n.id,{x:450+Math.cos(angle)*r*1.65,y:270+Math.sin(angle)*r});
    });
    graph.edges.forEach(e=>{
      const a=positions.get(e.source),b=positions.get(e.target); if(!a||!b)return;
      const line=element("line",{x1:a.x,y1:a.y,x2:b.x,y2:b.y,stroke:e.inferred?"#c57624":"#d7dce9","stroke-width":e.inferred?2:1});
      line.append(element("title",{},e.relation)); canvas.append(line);
    });
    chosen.forEach((n,index)=>{
      const p=positions.get(n.id),g=element("g",{tabindex:0,role:"button","aria-label":n.kind+": "+n.label});
      g.append(element("circle",{cx:p.x,cy:p.y,r:n.id===focus?13:8,fill:colors[n.kind]}));
      if(chosen.length<=24||n.id===focus||n.kind==="document"||index<10) g.append(element("text",{x:p.x,y:p.y+22,"text-anchor":"middle",fill:"#334155","font-size":10},n.label.slice(0,18)));
      g.append(element("title",{},n.label)); g.addEventListener("click",()=>{select.value=n.id;draw(n.id);});
      g.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();draw(n.id);}}); canvas.append(g);
    });
  }
  search.addEventListener("input",options); select.addEventListener("change",()=>{if(select.value)draw(select.value);});
  document.getElementById("graph-reset").addEventListener("click",()=>{search.value="";options();draw();});
  options();draw();
})();
