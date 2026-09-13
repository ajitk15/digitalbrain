/*
 * Interactive knowledge graph explorer.
 *
 * No external library: the CSP allows scripts only from this origin, and a graph
 * this size does not need one. Layout is a small force simulation, rendering is
 * plain SVG, and every node label is set with textContent so source-derived text
 * is never interpreted as markup.
 *
 * The old renderer drew a fixed ring of at most 80 nodes and silently dropped the
 * rest. This one starts from a readable overview and lets you expand outwards, so
 * a large graph is navigable rather than arbitrarily truncated.
 */
(() => {
  const raw = document.getElementById("knowledge-graph-data");
  if (!raw) return;

  const graph = JSON.parse(raw.textContent);
  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  const canvas = document.getElementById("graph-canvas");
  const select = document.getElementById("graph-node");
  const search = document.getElementById("graph-search");
  const inspector = document.getElementById("graph-inspector");
  const counter = document.getElementById("graph-count");
  const filterBox = document.getElementById("graph-filters");
  const expandButton = document.getElementById("graph-expand");
  const resetButton = document.getElementById("graph-reset");
  const zoomIn = document.getElementById("graph-zoom-in");
  const zoomOut = document.getElementById("graph-zoom-out");
  const zoomFit = document.getElementById("graph-fit");

  const NS = "http://www.w3.org/2000/svg";
  const COLORS = {
    document: "#4145c8",
    record: "#8a53bc",
    value: "#258777",
    section: "#7b879b",
    entity: "#c57624",
  };
  const KINDS = ["document", "record", "value", "section", "entity"];
  const START_NODES = 120;
  const MAX_NODES = 400;
  const WIDTH = 900;
  const HEIGHT = 540;

  // Adjacency once, rather than scanning every edge on each interaction.
  const neighbours = new Map();
  graph.nodes.forEach((n) => neighbours.set(n.id, new Set()));
  const edges = graph.edges.filter((e) => nodes.has(e.source) && nodes.has(e.target));
  edges.forEach((e) => {
    neighbours.get(e.source).add(e.target);
    neighbours.get(e.target).add(e.source);
  });

  const hidden = new Set();
  let visible = new Set();
  let focus = null;
  let layout = new Map();
  let view = { x: 0, y: 0, k: 1 };
  let ticking = null;

  const el = (tag, attrs, text) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
    if (text !== undefined) node.textContent = text;
    return node;
  };

  const shown = () => [...visible].filter((id) => !hidden.has(nodes.get(id).kind));

  function overview() {
    const picked = new Set();
    const documents = graph.nodes.filter((n) => n.kind === "document");
    documents.forEach((d) => picked.add(d.id));
    // Fill outwards from documents so the first view shows real structure.
    const queue = [...picked];
    while (queue.length && picked.size < START_NODES) {
      const current = queue.shift();
      for (const next of neighbours.get(current)) {
        if (picked.size >= START_NODES) break;
        if (!picked.has(next)) {
          picked.add(next);
          queue.push(next);
        }
      }
    }
    if (!picked.size) graph.nodes.slice(0, START_NODES).forEach((n) => picked.add(n.id));
    return picked;
  }

  function expand(id) {
    let added = 0;
    for (const next of neighbours.get(id) || []) {
      if (visible.size >= MAX_NODES) break;
      if (!visible.has(next)) {
        visible.add(next);
        seed(next, layout.get(id));
        added += 1;
      }
    }
    return added;
  }

  function expandAll() {
    let added = 0;
    for (const id of shown()) {
      added += expand(id);
      if (visible.size >= MAX_NODES) break;
    }
    return added;
  }

  function seed(id, near) {
    const angle = Math.random() * Math.PI * 2;
    const base = near || { x: WIDTH / 2, y: HEIGHT / 2 };
    layout.set(id, {
      x: base.x + Math.cos(angle) * 60,
      y: base.y + Math.sin(angle) * 60,
      vx: 0,
      vy: 0,
      pinned: false,
    });
  }

  function ensureLayout() {
    shown().forEach((id) => {
      if (!layout.has(id)) seed(id, null);
    });
  }

  /* A small spring/repulsion simulation. Deterministic enough to be stable and
     cheap enough for a few hundred nodes without a library. */
  function relax(steps) {
    const ids = shown();
    const live = ids.map((id) => layout.get(id));
    const index = new Map(ids.map((id, i) => [id, i]));
    const active = edges.filter((e) => index.has(e.source) && index.has(e.target));
    const centreX = WIDTH / 2;
    const centreY = HEIGHT / 2;
    for (let step = 0; step < steps; step += 1) {
      for (let i = 0; i < live.length; i += 1) {
        for (let j = i + 1; j < live.length; j += 1) {
          const a = live[i];
          const b = live[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distance = Math.hypot(dx, dy) || 0.01;
          if (distance > 260) continue;
          const push = 2600 / (distance * distance);
          dx /= distance;
          dy /= distance;
          a.vx -= dx * push;
          a.vy -= dy * push;
          b.vx += dx * push;
          b.vy += dy * push;
        }
      }
      active.forEach((e) => {
        const a = live[index.get(e.source)];
        const b = live[index.get(e.target)];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distance = Math.hypot(dx, dy) || 0.01;
        const pull = (distance - 90) * 0.012;
        const ux = (dx / distance) * pull;
        const uy = (dy / distance) * pull;
        a.vx += ux;
        a.vy += uy;
        b.vx -= ux;
        b.vy -= uy;
      });
      live.forEach((p) => {
        if (p.pinned) {
          p.vx = 0;
          p.vy = 0;
          return;
        }
        p.vx += (centreX - p.x) * 0.004;
        p.vy += (centreY - p.y) * 0.004;
        p.vx *= 0.82;
        p.vy *= 0.82;
        p.x += Math.max(-25, Math.min(25, p.vx));
        p.y += Math.max(-25, Math.min(25, p.vy));
      });
    }
  }

  function applyView() {
    const viewport = canvas.querySelector("#graph-viewport");
    if (viewport) {
      viewport.setAttribute(
        "transform",
        `translate(${view.x} ${view.y}) scale(${view.k})`
      );
    }
  }

  function fit() {
    const points = shown().map((id) => layout.get(id)).filter(Boolean);
    if (!points.length) return;
    const xs = points.map((p) => p.x);
    const ys = points.map((p) => p.y);
    const minX = Math.min(...xs) - 40;
    const maxX = Math.max(...xs) + 40;
    const minY = Math.min(...ys) - 40;
    const maxY = Math.max(...ys) + 40;
    const k = Math.max(0.2, Math.min(2.2, Math.min(WIDTH / (maxX - minX), HEIGHT / (maxY - minY))));
    view = {
      k,
      x: WIDTH / 2 - ((minX + maxX) / 2) * k,
      y: HEIGHT / 2 - ((minY + maxY) / 2) * k,
    };
    applyView();
  }

  function inspect(id) {
    const node = nodes.get(id);
    inspector.replaceChildren();
    const title = document.createElement("h2");
    title.textContent = node.label;
    inspector.append(title);

    const kind = document.createElement("p");
    kind.className = "inspector-kind";
    kind.textContent = node.kind;
    inspector.append(kind);

    const related = edges.filter((e) => e.source === id || e.target === id);
    const inferred = related.filter((e) => e.inferred).length;

    const summary = document.createElement("p");
    summary.textContent =
      `${related.length} relationship(s), ${inferred} AI-inferred. ` +
      "Showing up to 30 evidence entries.";
    inspector.append(summary);

    const expandHere = document.createElement("button");
    expandHere.type = "button";
    expandHere.className = "button secondary";
    expandHere.textContent = "Expand neighbours";
    expandHere.addEventListener("click", () => {
      expand(id);
      render(true);
    });
    inspector.append(expandHere);

    related.slice(0, 30).forEach((e) => {
      const block = document.createElement("div");
      block.className = "graph-evidence";
      const relation = document.createElement("strong");
      relation.textContent = e.relation;
      if (e.inferred) {
        const tag = document.createElement("span");
        tag.className = "inferred-tag";
        tag.textContent = "AI";
        relation.append(" ", tag);
      }
      block.append(relation);
      if (e.knowledge_id) {
        const link = document.createElement("a");
        link.textContent = `View source · line ${e.line}`;
        link.href = `../knowledge/${e.knowledge_id}/`;
        block.append(link);
      }
      if (e.evidence) {
        const quote = document.createElement("pre");
        quote.textContent = e.evidence;
        block.append(quote);
      }
      inspector.append(block);
    });
  }

  function render(settle) {
    ensureLayout();
    if (settle) relax(140);

    const ids = shown();
    const set = new Set(ids);
    canvas.replaceChildren();
    const viewport = el("g", { id: "graph-viewport" });
    canvas.append(viewport);

    const lines = el("g", { "stroke-linecap": "round" });
    viewport.append(lines);

    edges.forEach((e) => {
      if (!set.has(e.source) || !set.has(e.target)) return;
      const a = layout.get(e.source);
      const b = layout.get(e.target);
      const line = el("line", {
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        stroke: e.inferred ? "#c57624" : "#d7dce9",
        "stroke-width": e.inferred ? 1.8 : 1,
        "stroke-dasharray": e.inferred ? "4 3" : "",
      });
      line.append(el("title", {}, `${e.relation}${e.inferred ? " (AI-inferred)" : ""}`));
      lines.append(line);
    });

    ids.forEach((id) => {
      const node = nodes.get(id);
      const point = layout.get(id);
      const group = el("g", {
        tabindex: 0,
        role: "button",
        class: "graph-node",
        "aria-label": `${node.kind}: ${node.label}`,
        transform: `translate(${point.x} ${point.y})`,
      });
      group.dataset.id = id;
      const radius = id === focus ? 12 : node.kind === "document" ? 9 : 6;
      group.append(
        el("circle", {
          r: radius,
          fill: COLORS[node.kind] || "#7b879b",
          stroke: id === focus ? "#1f2937" : "#fff",
          "stroke-width": id === focus ? 2 : 1,
        })
      );
      // Labels only where they stay legible.
      if (id === focus || node.kind === "document" || ids.length <= 45) {
        group.append(
          el(
            "text",
            { y: radius + 12, "text-anchor": "middle", fill: "#334155", "font-size": 10 },
            node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label
          )
        );
      }
      group.append(el("title", {}, `${node.label} (${node.kind})`));
      viewport.append(group);
    });

    applyView();
    counter.textContent =
      `${ids.length} of ${graph.nodes.length} nodes shown` +
      (visible.size >= MAX_NODES ? " · display limit reached" : "");
    expandButton.disabled = visible.size >= MAX_NODES;
  }

  function focusOn(id) {
    focus = id;
    if (!visible.has(id)) {
      visible.add(id);
      seed(id, null);
      expand(id);
    }
    hidden.delete(nodes.get(id).kind);
    syncFilters();
    inspect(id);
    render(true);
    fit();
  }

  /* ---- controls ---- */

  function buildFilters() {
    const present = KINDS.filter((kind) => graph.nodes.some((n) => n.kind === kind));
    filterBox.replaceChildren();
    present.forEach((kind) => {
      const label = document.createElement("label");
      label.className = "graph-filter";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !hidden.has(kind);
      box.dataset.kind = kind;
      box.addEventListener("change", () => {
        if (box.checked) hidden.delete(kind);
        else hidden.add(kind);
        render(true);
      });
      const swatch = document.createElement("span");
      swatch.className = "graph-swatch";
      swatch.style.background = COLORS[kind];
      const text = document.createElement("span");
      text.textContent = kind === "entity" ? "AI entity" : kind;
      label.append(box, swatch, text);
      filterBox.append(label);
    });
  }

  function syncFilters() {
    filterBox.querySelectorAll("input[data-kind]").forEach((box) => {
      box.checked = !hidden.has(box.dataset.kind);
    });
  }

  function options() {
    const term = search.value.toLowerCase();
    select.replaceChildren();
    const prompt = document.createElement("option");
    prompt.value = "";
    prompt.textContent = "Select a node…";
    select.append(prompt);
    graph.nodes
      .filter((n) => n.label.toLowerCase().includes(term))
      .slice(0, 300)
      .forEach((n) => {
        const option = document.createElement("option");
        option.value = n.id;
        option.textContent = `${n.kind}: ${n.label}`;
        select.append(option);
      });
  }

  // Pointer: drag a node to pin it, drag the background to pan.
  let dragging = null;
  canvas.addEventListener("pointerdown", (event) => {
    const group = event.target.closest(".graph-node");
    const point = { x: event.clientX, y: event.clientY };
    if (group) {
      dragging = { id: group.dataset.id, start: point, moved: false };
    } else {
      dragging = { pan: true, start: point, origin: { ...view }, moved: false };
    }
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const dx = event.clientX - dragging.start.x;
    const dy = event.clientY - dragging.start.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) dragging.moved = true;
    if (dragging.pan) {
      view.x = dragging.origin.x + dx;
      view.y = dragging.origin.y + dy;
      applyView();
      return;
    }
    const point = layout.get(dragging.id);
    if (!point) return;
    const scale = canvas.getBoundingClientRect().width / WIDTH || 1;
    point.x += dx / (view.k * scale);
    point.y += dy / (view.k * scale);
    point.pinned = true;
    dragging.start = { x: event.clientX, y: event.clientY };
    render(false);
  });

  canvas.addEventListener("pointerup", (event) => {
    if (dragging && !dragging.moved && !dragging.pan) focusOn(dragging.id);
    dragging = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  });

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const scale = rect.width / WIDTH || 1;
      const px = (event.clientX - rect.left) / scale;
      const py = (event.clientY - rect.top) / scale;
      const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
      const next = Math.max(0.2, Math.min(4, view.k * factor));
      view.x = px - ((px - view.x) * next) / view.k;
      view.y = py - ((py - view.y) * next) / view.k;
      view.k = next;
      applyView();
    },
    { passive: false }
  );

  canvas.addEventListener("keydown", (event) => {
    const group = event.target.closest(".graph-node");
    if (!group) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      focusOn(group.dataset.id);
    }
  });

  const zoomBy = (factor) => {
    view.k = Math.max(0.2, Math.min(4, view.k * factor));
    applyView();
  };

  search.addEventListener("input", options);
  select.addEventListener("change", () => {
    if (select.value) focusOn(select.value);
  });
  expandButton.addEventListener("click", () => {
    expandAll();
    render(true);
  });
  resetButton.addEventListener("click", () => {
    search.value = "";
    focus = null;
    hidden.clear();
    layout = new Map();
    visible = overview();
    syncFilters();
    options();
    render(true);
    fit();
  });
  zoomIn.addEventListener("click", () => zoomBy(1.25));
  zoomOut.addEventListener("click", () => zoomBy(1 / 1.25));
  zoomFit.addEventListener("click", fit);

  // Keep the simulation warm briefly after a change rather than freezing mid-move.
  const settle = () => {
    window.clearTimeout(ticking);
    ticking = window.setTimeout(() => {
      relax(40);
      render(false);
    }, 60);
  };
  canvas.addEventListener("pointerup", settle);

  buildFilters();
  visible = overview();
  options();
  render(true);
  fit();
})();
