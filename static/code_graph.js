(() => {
  const raw = document.getElementById("code-graph-data");
  const canvas = document.getElementById("code-graph-canvas");
  if (!raw || !canvas) return;
  const graph = JSON.parse(raw.textContent);
  const NS = "http://www.w3.org/2000/svg";
  const width = 1000;
  const height = 620;
  const nodes = graph.nodes.slice(0, 200);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = graph.edges.filter((edge) => byId.has(edge.source) && byId.has(edge.target));
  const incoming = new Map(nodes.map((node) => [node.id, 0]));
  edges.forEach((edge) => incoming.set(edge.target, incoming.get(edge.target) + 1));
  const columns = new Map();
  const depth = new Map();
  const queue = nodes.filter((node) => incoming.get(node.id) === 0).map((node) => node.id);
  queue.forEach((id) => depth.set(id, 0));
  while (queue.length) {
    const source = queue.shift();
    edges.filter((edge) => edge.source === source).forEach((edge) => {
      const next = Math.min((depth.get(source) || 0) + 1, 7);
      if (!depth.has(edge.target) || depth.get(edge.target) < next) depth.set(edge.target, next);
      incoming.set(edge.target, incoming.get(edge.target) - 1);
      if (incoming.get(edge.target) === 0) queue.push(edge.target);
    });
  }
  nodes.forEach((node) => {
    const level = depth.get(node.id) || 0;
    if (!columns.has(level)) columns.set(level, []);
    columns.get(level).push(node);
  });
  const maxLevel = Math.max(0, ...columns.keys());
  const positions = new Map();
  columns.forEach((items, level) => items.forEach((node, row) => {
    positions.set(node.id, {
      x: 85 + (level * (width - 170)) / Math.max(maxLevel, 1),
      y: 45 + ((row + 0.5) * (height - 90)) / Math.max(items.length, 1),
    });
  }));
  canvas.replaceChildren();
  const defs = document.createElementNS(NS, "defs");
  const marker = document.createElementNS(NS, "marker");
  marker.setAttribute("id", "code-arrow"); marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "9"); marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "5"); marker.setAttribute("markerHeight", "5");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(NS, "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); arrow.setAttribute("fill", "#98a2b5");
  marker.appendChild(arrow); defs.appendChild(marker); canvas.appendChild(defs);
  edges.forEach((edge) => {
    const start = positions.get(edge.source); const end = positions.get(edge.target);
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", start.x); line.setAttribute("y1", start.y);
    line.setAttribute("x2", end.x); line.setAttribute("y2", end.y);
    line.setAttribute("class", edge.confidence === "static" ? "code-edge" : "code-edge inferred");
    line.setAttribute("marker-end", "url(#code-arrow)"); canvas.appendChild(line);
  });
  nodes.forEach((node) => {
    const point = positions.get(node.id);
    const link = document.createElementNS(NS, "a"); link.setAttribute("href", node.url);
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("cx", point.x); circle.setAttribute("cy", point.y); circle.setAttribute("r", "8");
    circle.setAttribute("class", node.parse_ok ? "code-node" : "code-node parse-error");
    const title = document.createElementNS(NS, "title");
    title.textContent = `${node.path} · ${node.language}${node.parse_ok ? "" : " · parse issue"}`;
    circle.appendChild(title); link.appendChild(circle); canvas.appendChild(link);
  });
})();
