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

  // Two palettes rather than one lightened: a mid-tone that reads as bright on
  // near-black washes out on paper, so the light values are darkened instead.
  // These land in SVG presentation attributes and in colour arithmetic for the
  // edge gradients, where a CSS custom property is not reliably resolvable.
  const PALETTES = {
    dark: {
      document: "#6d7cff",
      record: "#b07cff",
      value: "#35c8a0",
      section: "#7f8db0",
      entity: "#e0a92e",
      edge: "#2c3c60",
      inferred: "#e0a92e",
      label: "#b8c1d9",
      focusRing: "#e4e9f2",
      ground: "#242b3d",
    },
    light: {
      document: "#4145c8",
      record: "#8a53bc",
      value: "#258777",
      section: "#7b879b",
      entity: "#b45309",
      edge: "#d7dce9",
      inferred: "#b45309",
      label: "#334155",
      focusRing: "#1f2937",
      ground: "#c2c3c8",
    },
  };
  let theme = "dark";
  let COLORS = PALETTES[theme];

  // Themes are Graphify's Leiden communities, computed on the server - the same
  // partition the Quality table lists, in the same order. Six colours in a fixed
  // order, validated per canvas for colour-blind separation; every theme past the
  // sixth folds into "Other" rather than being given a generated hue. On the
  // light canvas three of them sit under 3:1, so the legend names every one.
  const THEME_PALETTES = {
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
  };
  const themesRaw = document.getElementById("knowledge-graph-themes");
  const clusters = themesRaw ? JSON.parse(themesRaw.textContent) : null;
  const themeCount = clusters ? clusters.themes.length : 0;
  const OTHER = -1;
  const themeOf = (id) => {
    const index = clusters ? clusters.membership[id] : undefined;
    return index === undefined || index >= THEME_PALETTES.dark.length ? OTHER : index;
  };
  const colourSelect = document.getElementById("graph-colour");
  let colourBy = themeCount ? "theme" : "kind";
  try {
    if (themeCount && localStorage.getItem("digital-brain.knowledge.colour") === "kind") {
      colourBy = "kind";
    }
  } catch (failure) {
    /* no stored preference is the normal case */
  }
  const colourOf = (id) => {
    if (colourBy === "theme") {
      const index = themeOf(id);
      return index === OTHER ? COLORS.section : THEME_PALETTES[theme][index];
    }
    return COLORS[nodes.get(id).kind] || COLORS.section;
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

  // Degree decides how large a node draws. A uniform dot field says nothing
  // about which parts of the graph carry weight.
  const degree = new Map();
  graph.nodes.forEach((n) => degree.set(n.id, neighbours.get(n.id).size));
  const busiest = Math.max(1, ...degree.values());

  const hidden = new Set();
  const hiddenThemes = new Set();
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

  const toRgb = (hex) => {
    const value = parseInt(hex.slice(1), 16);
    return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  };
  const mixHex = (a, b, t) => {
    const left = toRgb(a);
    const right = toRgb(b);
    const part = (i) => Math.round(left[i] + (right[i] - left[i]) * t);
    return `#${[0, 1, 2].map((i) => part(i).toString(16).padStart(2, "0")).join("")}`;
  };

  const shown = () =>
    [...visible].filter(
      (id) => !hidden.has(nodes.get(id).kind) && !hiddenThemes.has(themeOf(id))
    );

  // Coloured by theme, the first view is the themes: the largest ones, a
  // connected slice of each grown from its hub. Starting from the documents
  // instead showed a screen of mostly "Other" - a real knowledge graph splits
  // into far more themes than there are colours, and the document-first view
  // lands in the small ones.
  function themeOverview() {
    const coloured = Math.min(themeCount, THEME_PALETTES.dark.length);
    const share = Math.max(1, Math.floor(START_NODES / coloured));
    const members = new Map();
    graph.nodes.forEach((n) => {
      const index = themeOf(n.id);
      if (index === OTHER) return;
      if (!members.has(index)) members.set(index, []);
      members.get(index).push(n.id);
    });
    const picked = new Set();
    members.forEach((ids) => {
      const inTheme = new Set(ids);
      const hub = ids.reduce((best, id) => (degree.get(id) > degree.get(best) ? id : best));
      const got = new Set([hub]);
      const queue = [hub];
      while (queue.length && got.size < share) {
        for (const next of neighbours.get(queue.shift())) {
          if (got.size >= share) break;
          if (inTheme.has(next) && !got.has(next)) {
            got.add(next);
            queue.push(next);
          }
        }
      }
      got.forEach((id) => picked.add(id));
    });
    return picked;
  }

  function overview() {
    if (colourBy === "theme") return themeOverview();
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

  // A node with nowhere to start from starts in its theme's sector, so the
  // themes open as separate regions instead of untangling from one knot.
  function home(id) {
    const index = colourBy === "theme" ? themeOf(id) : OTHER;
    if (index === OTHER) return { x: WIDTH / 2, y: HEIGHT / 2 };
    const coloured = Math.min(themeCount, THEME_PALETTES.dark.length);
    const angle = (index / coloured) * Math.PI * 2 - Math.PI / 2;
    return { x: WIDTH / 2 + Math.cos(angle) * 190, y: HEIGHT / 2 + Math.sin(angle) * 150 };
  }

  function seed(id, near) {
    const angle = Math.random() * Math.PI * 2;
    const base = near || home(id);
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
    // Each node leans towards the middle of its theme, so a theme reads as a
    // region of the canvas rather than only as a colour. Gentle next to the
    // springs: it groups, it does not override what the edges say.
    const groups = themeCount ? ids.map((id) => themeOf(id)) : null;
    for (let step = 0; step < steps; step += 1) {
      if (groups) {
        const sums = new Map();
        live.forEach((p, i) => {
          if (groups[i] === OTHER) return;
          const sum = sums.get(groups[i]) || { x: 0, y: 0, n: 0 };
          sum.x += p.x;
          sum.y += p.y;
          sum.n += 1;
          sums.set(groups[i], sum);
        });
        live.forEach((p, i) => {
          const sum = sums.get(groups[i]);
          if (!sum || sum.n < 2) return;
          p.vx += (sum.x / sum.n - p.x) * 0.02;
          p.vy += (sum.y / sum.n - p.y) * 0.02;
        });
      }
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

  //: Zoomed past this there is room on screen for every label at once.
  const LABEL_ALL_ZOOM = 1.6;

  //: How much of a long label survives, and from which end.
  //
  // Cutting the tail off assumes the beginning is what tells two labels apart.
  // For a document imported from a folder that is exactly backwards: every name
  // carries the same repository and path prefix, and the filename - the only
  // part anyone recognises - is at the end. A screen of
  // "digitalbrain-demo-artifac..." is one label drawn forty times.
  //
  // Keeping both ends costs a few characters of prefix and returns the
  // filename. The title element still carries the whole name for hovering.
  const LABEL_MAX = 30;
  const LABEL_HEAD = 8;

  function shorten(text) {
    if (text.length <= LABEL_MAX) return text;
    return `${text.slice(0, LABEL_HEAD)}…${text.slice(-(LABEL_MAX - LABEL_HEAD - 1))}`;
  }

  function applyView() {
    const viewport = canvas.querySelector("#graph-viewport");
    if (viewport) {
      viewport.setAttribute(
        "transform",
        `translate(${view.x} ${view.y}) scale(${view.k})`
      );
    }
    canvas.classList.toggle("labelled", view.k >= LABEL_ALL_ZOOM);
  }

  const minimap = document.getElementById("graph-minimap");

  function drawMinimap(ids) {
    if (!minimap) return;
    if (!ids.length) {
      minimap.replaceChildren();
      return;
    }
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    ids.forEach((id) => {
      const point = layout.get(id);
      minX = Math.min(minX, point.x);
      minY = Math.min(minY, point.y);
      maxX = Math.max(maxX, point.x);
      maxY = Math.max(maxY, point.y);
    });
    const pad = 20;
    minimap.setAttribute(
      "viewBox",
      `${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`
    );
    minimap.replaceChildren();
    ids.forEach((id) => {
      const point = layout.get(id);
      minimap.append(
        el("circle", {
          cx: point.x,
          cy: point.y,
          r: 5,
          fill: colourOf(id),
          opacity: 0.8,
        })
      );
    });
  }

  if (minimap) {
    minimap.addEventListener("click", (event) => {
      const box = minimap.getBoundingClientRect();
      const parts = (minimap.getAttribute("viewBox") || "0 0 1 1").split(" ").map(Number);
      const x = parts[0] + ((event.clientX - box.left) / box.width) * parts[2];
      const y = parts[1] + ((event.clientY - box.top) / box.height) * parts[3];
      view.x = WIDTH / 2 - x * view.k;
      view.y = HEIGHT / 2 - y * view.k;
      applyView();
    });
  }

  const themeButton = document.getElementById("graph-theme");
  if (themeButton) {
    const applyTheme = () => {
      COLORS = PALETTES[theme];
      canvas.setAttribute("data-canvas", theme);
      themeButton.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      const next = theme === "dark" ? "light" : "dark";
      themeButton.setAttribute("aria-label", `Switch the canvas to ${next}`);
      themeButton.setAttribute("title", `Switch the canvas to ${next}`);
      // A redraw is cheap here and the layout is untouched by it: positions
      // live in `layout`, so the camera and the expansion survive. The legend
      // swatches carry the palette too, so they are rebuilt with it.
      buildFilters();
      render(false);
    };
    themeButton.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem("digital-brain.knowledge.canvas", theme);
      } catch (failure) {
        /* a private window refusing storage is not a reason to fail the click */
      }
      applyTheme();
    });
    try {
      if (localStorage.getItem("digital-brain.knowledge.canvas") === "light") {
        theme = "light";
        // The palette has to move with the theme before the first render, or a
        // remembered preference draws dark nodes onto a light canvas.
        COLORS = PALETTES[theme];
      }
    } catch (failure) {
      /* no stored preference is the normal case */
    }
    canvas.setAttribute("data-canvas", theme);
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

    if (clusters && clusters.membership[id] !== undefined) {
      const inTheme = document.createElement("p");
      inTheme.className = "inspector-theme";
      inTheme.textContent = `Theme: ${clusters.themes[clusters.membership[id]].label}`;
      inspector.append(inTheme);
    }

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
    // The busiest nodes on screen keep a standing label: a hub is what you
    // navigate by, whatever kind it happens to be.
    const hubs = new Set(
      [...ids]
        .sort((left, right) => (degree.get(right) || 0) - (degree.get(left) || 0))
        .slice(0, 18)
    );
    canvas.replaceChildren();

    const defs = el("defs", {});
    canvas.append(defs);

    // The dotted ground is one pattern however far you pan, where a grid of
    // real dots would be thousands of elements.
    const dots = el("pattern", {
      id: "kg-dots",
      width: 24,
      height: 24,
      patternUnits: "userSpaceOnUse",
    });
    dots.append(el("circle", { cx: 1, cy: 1, r: 1, fill: COLORS.ground }));
    defs.append(dots);
    canvas.append(
      el("rect", { x: -4000, y: -4000, width: 9000, height: 9000, fill: "url(#kg-dots)" })
    );

    const viewport = el("g", { id: "graph-viewport" });
    canvas.append(viewport);

    const lines = el("g", { "stroke-linecap": "round" });
    viewport.append(lines);

    // An edge is a gradient between the two kinds it joins, with the midpoint
    // pulled most of the way to the edge tone: a fully saturated middle turns a
    // dense graph into stripes and stops the nodes reading as the subject.
    const gradients = new Map();
    const gradientFor = (from, to) => {
      const key = `${from}|${to}`;
      if (gradients.has(key)) return gradients.get(key);
      const id = `kg-edge-${gradients.size}`;
      const gradient = el("linearGradient", {
        id,
        gradientUnits: "objectBoundingBox",
        x1: "0",
        y1: "0",
        x2: "1",
        y2: "1",
      });
      const middle = mixHex(mixHex(from, to, 0.5), COLORS.edge, 0.55);
      gradient.append(el("stop", { offset: "0", "stop-color": from, "stop-opacity": "0.75" }));
      gradient.append(el("stop", { offset: "0.5", "stop-color": middle }));
      gradient.append(el("stop", { offset: "1", "stop-color": to, "stop-opacity": "0.75" }));
      defs.append(gradient);
      gradients.set(key, id);
      return id;
    };

    edges.forEach((e) => {
      if (!set.has(e.source) || !set.has(e.target)) return;
      const a = layout.get(e.source);
      const b = layout.get(e.target);
      const from = colourOf(e.source);
      const to = colourOf(e.target);
      const line = el("line", {
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        // An inferred relation keeps a flat amber: guessed evidence must not
        // look like the same thing as a structural fact.
        stroke: e.inferred ? COLORS.inferred : `url(#${gradientFor(from, to)})`,
        "stroke-width": e.inferred ? 1.6 : 1.2,
        "stroke-dasharray": e.inferred ? "4 3" : "",
        class: "kg-edge",
      });
      line.dataset.source = e.source;
      line.dataset.target = e.target;
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
      // Size carries degree, so the hubs are visible before anything is clicked.
      const weight = Math.sqrt((degree.get(id) || 0) / busiest);
      const radius =
        id === focus ? 13 : Math.max(4.5, (node.kind === "document" ? 7 : 5) + weight * 7);
      if (id === focus) {
        group.append(
          el("circle", {
            r: radius + 7,
            fill: "none",
            stroke: colourOf(id),
            "stroke-width": 1,
            opacity: 0.45,
          })
        );
      }
      group.append(
        el("circle", {
          r: radius,
          fill: colourOf(id),
          stroke: id === focus ? COLORS.focusRing : "none",
          "stroke-width": id === focus ? 2 : 0,
        })
      );
      // Every node carries its label and CSS decides which reach the screen, so
      // hovering or zooming reveals more without redrawing the scene. Drawing
      // them only for documents left a field of unnamed dots that could be read
      // one click at a time, which is not a way of reading a graph.
      const named =
        id === focus || node.kind === "document" || ids.length <= 45 || hubs.has(id);
      group.classList.toggle("named", named);
      group.append(
        el(
          "text",
          {
            y: radius + 12,
            "text-anchor": "middle",
            fill: COLORS.label,
            "font-size": 10,
            class: "kg-label",
          },
          shorten(node.label)
        )
      );
      group.append(el("title", {}, `${node.label} (${node.kind})`));
      viewport.append(group);
    });

    // Hover traces a neighbourhood. At 400 nodes this is most of what makes the
    // picture explorable without committing to a selection.
    const lit = (id) => {
      const near = neighbours.get(id) || new Set();
      canvas.classList.add("tracing");
      viewport.querySelectorAll(".graph-node").forEach((group) => {
        const other = group.dataset.id;
        group.classList.toggle("lit", other === id || near.has(other));
      });
      lines.querySelectorAll(".kg-edge").forEach((line) => {
        line.classList.toggle(
          "lit",
          line.dataset.source === id || line.dataset.target === id
        );
      });
    };
    const unlit = () => {
      canvas.classList.remove("tracing");
      viewport.querySelectorAll(".lit").forEach((node) => node.classList.remove("lit"));
      lines.querySelectorAll(".lit").forEach((line) => line.classList.remove("lit"));
    };
    viewport.querySelectorAll(".graph-node").forEach((group) => {
      group.addEventListener("pointerenter", () => lit(group.dataset.id));
      group.addEventListener("pointerleave", unlit);
      group.addEventListener("focus", () => lit(group.dataset.id));
      group.addEventListener("blur", unlit);
    });

    drawMinimap(ids);
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
    hiddenThemes.delete(themeOf(id));
    syncFilters();
    inspect(id);
    render(true);
    fit();
  }

  /* ---- controls ---- */

  // The legend is also the filter, and it follows the colouring: coloured by
  // theme, it lists the themes; coloured by kind, it lists the kinds.
  function filterItem(text, colour, checked, onChange, data) {
    const label = document.createElement("label");
    label.className = "graph-filter";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = checked;
    Object.assign(box.dataset, data);
    box.addEventListener("change", () => {
      onChange(box.checked);
      render(true);
    });
    const swatch = document.createElement("span");
    swatch.className = "graph-swatch";
    swatch.style.background = colour;
    const name = document.createElement("span");
    name.textContent = text;
    label.title = text;
    label.append(box, swatch, name);
    filterBox.append(label);
  }

  const toggle = (set, key) => (checked) => (checked ? set.delete(key) : set.add(key));

  function buildFilters() {
    filterBox.replaceChildren();
    filterBox.classList.toggle("by-theme", colourBy === "theme");
    if (colourBy === "theme") {
      const coloured = Math.min(themeCount, THEME_PALETTES.dark.length);
      for (let index = 0; index < coloured; index += 1) {
        filterItem(
          clusters.themes[index].label,
          THEME_PALETTES[theme][index],
          !hiddenThemes.has(index),
          toggle(hiddenThemes, index),
          { theme: String(index) }
        );
      }
      if (graph.nodes.some((n) => themeOf(n.id) === OTHER)) {
        filterItem("Other", COLORS.section, !hiddenThemes.has(OTHER), toggle(hiddenThemes, OTHER), {
          theme: String(OTHER),
        });
      }
      return;
    }
    KINDS.filter((kind) => graph.nodes.some((n) => n.kind === kind)).forEach((kind) => {
      filterItem(
        kind === "entity" ? "AI entity" : kind,
        COLORS[kind],
        !hidden.has(kind),
        toggle(hidden, kind),
        { kind }
      );
    });
  }

  function syncFilters() {
    filterBox.querySelectorAll("input[data-kind]").forEach((box) => {
      box.checked = !hidden.has(box.dataset.kind);
    });
    filterBox.querySelectorAll("input[data-theme]").forEach((box) => {
      box.checked = !hiddenThemes.has(Number(box.dataset.theme));
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
      // The node's starting position is captured here, not re-read each move:
      // see the pointermove handler for why that distinction matters.
      const node = layout.get(group.dataset.id);
      dragging = {
        id: group.dataset.id,
        start: point,
        moved: false,
        origin: node ? { x: node.x, y: node.y } : null,
      };
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
    const node = layout.get(dragging.id);
    if (!node || !dragging.origin) return;
    // Absolute from the position the drag started at, exactly like the pan
    // branch above. It used to add each frame's delta and then re-base
    // dragging.start, so every move divided a small delta by a fractional
    // scale - getBoundingClientRect() returns fractional CSS pixels, and at
    // 125%/150% Windows scaling the ratio never comes out clean. The rounding
    // error was small per event and compounded over a drag, so the node crept
    // away from the cursor and appeared to shake.
    const scale = canvas.getBoundingClientRect().width / WIDTH || 1;
    node.x = dragging.origin.x + dx / (view.k * scale);
    node.y = dragging.origin.y + dy / (view.k * scale);
    node.pinned = true;
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
    hiddenThemes.clear();
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

  if (colourSelect) {
    colourSelect.value = colourBy;
    colourSelect.addEventListener("change", () => {
      colourBy = colourSelect.value === "kind" ? "kind" : "theme";
      try {
        localStorage.setItem("digital-brain.knowledge.colour", colourBy);
      } catch (failure) {
        /* a private window refusing storage is not a reason to fail the change */
      }
      // A hidden theme would stay hidden with no checkbox left to show it, and
      // the first view depends on the colouring, so this starts over.
      hidden.clear();
      hiddenThemes.clear();
      focus = null;
      layout = new Map();
      visible = overview();
      buildFilters();
      render(true);
      fit();
    });
  }

  buildFilters();
  visible = overview();
  options();
  render(true);
  fit();
})();
