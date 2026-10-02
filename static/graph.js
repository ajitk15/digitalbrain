/*
 * Interactive knowledge graph explorer, drawn the way Obsidian's graph view is:
 * every node on one canvas, laid out by a live force simulation you can watch
 * settle and pull on, with hovering lighting up a node's neighbourhood and
 * signals running along its connections.
 *
 * No external library: the CSP allows scripts only from this origin. The
 * simulation is d3-force's model - many-body repulsion through a Barnes-Hut
 * quadtree, springs along edges, a gentle pull towards each theme's region -
 * written out here. Rendering is Canvas 2D, because thousands of SVG elements
 * redrawn sixty times a second is more than a browser will do smoothly. Labels
 * are painted with fillText, so source-derived text is never interpreted as
 * markup.
 */
(() => {
  const raw = document.getElementById("knowledge-graph-data");
  const canvas = document.getElementById("graph-canvas");
  if (!raw || !canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");

  const graph = JSON.parse(raw.textContent);
  const nodes = new Map(graph.nodes.map((n) => [n.id, n]));
  const select = document.getElementById("graph-node");
  const search = document.getElementById("graph-search");
  const inspector = document.getElementById("graph-inspector");
  const counter = document.getElementById("graph-count");
  const filterBox = document.getElementById("graph-filters");
  const expandButton = document.getElementById("graph-expand");
  const resetButton = document.getElementById("graph-reset");
  const expandCodeButton = document.getElementById("graph-expand-code");
  const codeStatus = document.getElementById("graph-code-status");
  function codeControl(message, action = null) {
    if (!expandCodeButton) return;
    expandCodeButton.disabled = !action;
    expandCodeButton.onclick = action;
    expandCodeButton.title = message;
    codeStatus.textContent = message;
  }
  const zoomIn = document.getElementById("graph-zoom-in");
  const zoomOut = document.getElementById("graph-zoom-out");
  const zoomFit = document.getElementById("graph-fit");
  const minimap = document.getElementById("graph-minimap");
  const themeButton = document.getElementById("graph-theme");
  const colourSelect = document.getElementById("graph-colour");
  const legendNodes = document.getElementById("graph-legend-nodes");
  const legendEdges = document.getElementById("graph-legend-edges");

  const reducedMotion =
    !!window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // Two palettes rather than one lightened: a mid-tone that reads as bright on
  // near-black washes out on paper, so the light values are darkened instead.
  const PALETTES = {
    dark: {
      document: "#6d7cff",
      record: "#b07cff",
      value: "#35c8a0",
      section: "#4fb3d9",
      other: "#7f8db0",
      entity: "#e0a92e",
      incident: "#6d7cff",
      change: "#e0a92e",
      component: "#35c8a0",
      service: "#b07cff",
      symptom: "#ef7a7a",
      group: "#7f8db0",
      passage: "#b8c1d9",
      kb: "#f2c94c",
      automation: "#f2789f",
      code: "#55b9ff",
      confirmed: "#4cc38a",
      inferred: "#e0a92e",
      edge: "#8a96b8",
      edgeAlpha: 0.2,
      accent: "#b49cff",
      label: "#d4dbec",
      labelHalo: "#0b0e14",
      glow: 0.5,
      blend: "lighter",
      dim: 0.1,
    },
    light: {
      document: "#4145c8",
      record: "#8a53bc",
      value: "#258777",
      section: "#1f7fa8",
      other: "#7b879b",
      entity: "#b45309",
      incident: "#4145c8",
      change: "#b45309",
      component: "#258777",
      service: "#8a53bc",
      symptom: "#a13e3e",
      group: "#7b879b",
      passage: "#58627a",
      kb: "#a16207",
      automation: "#be185d",
      code: "#0064a8",
      confirmed: "#1d6b45",
      inferred: "#b45309",
      edge: "#6b7591",
      edgeAlpha: 0.26,
      accent: "#6d4bd8",
      label: "#273142",
      labelHalo: "#f2f3f7",
      glow: 0.22,
      blend: "source-over",
      dim: 0.14,
    },
  };
  let theme = "dark";
  let COLORS = PALETTES[theme];

  // Themes are Graphify's Leiden communities, computed on the server - the same
  // partition the Quality table lists, in the same order. Six colours in a fixed
  // order, validated per canvas for colour-blind separation; every theme past the
  // sixth folds into "Other" rather than being given a generated hue.
  const THEME_PALETTES = {
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300"],
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"],
  };
  const themesRaw = document.getElementById("knowledge-graph-themes");
  const clusters = themesRaw ? JSON.parse(themesRaw.textContent) : null;
  const themeCount = clusters ? clusters.themes.length : 0;
  const COLOURED = Math.min(themeCount, THEME_PALETTES.dark.length);
  const OTHER = -1;
  const themeOf = (id) => {
    const index = clusters ? clusters.membership[id] : undefined;
    return index === undefined || index >= THEME_PALETTES.dark.length ? OTHER : index;
  };
  // Theme colouring is the default only where the coloured themes cover most of
  // the graph. A graph of hundreds of small themes would otherwise draw almost
  // every node in the grey of "Other", and kind says more than that.
  const themed = graph.nodes.filter((n) => themeOf(n.id) !== OTHER).length;
  let colourBy = themeCount && themed * 2 >= graph.nodes.length ? "theme" : "kind";
  try {
    const stored = localStorage.getItem("digital-brain.knowledge.colour");
    if (themeCount && (stored === "kind" || stored === "theme")) colourBy = stored;
  } catch (failure) {
    /* no stored preference is the normal case */
  }
  const colourOf = (id) => {
    if (nodes.get(id).kind === "code") return COLORS.code;
    if (colourBy === "theme") {
      const index = themeOf(id);
      return index === OTHER ? COLORS.other : THEME_PALETTES[theme][index];
    }
    return COLORS[nodes.get(id).kind] || COLORS.other;
  };
  // The operations layer's kinds follow the knowledge graph's; a filter is
  // offered only for kinds the graph on screen actually has.
  const KINDS = [
    "document",
    "record",
    "value",
    "section",
    "entity",
    "incident",
    "change",
    "component",
    "service",
    "symptom",
    "group",
    "passage",
    "kb",
    "automation",
    "code",
  ];

  //: Everything is drawn at once up to this many nodes, as Obsidian does. A
  //: larger graph opens on a connected overview of this size and expands.
  const MAX_NODES = 4000;
  //: How many of the busiest connections carry an ambient signal.
  const SIGNALS = 60;
  //: How many of the busiest nodes keep a label at every zoom.
  const HUB_LABELS = 14;

  // Adjacency once, rather than scanning every edge on each interaction.
  const neighbours = new Map();
  graph.nodes.forEach((n) => neighbours.set(n.id, new Set()));
  const edges = graph.edges.filter((e) => nodes.has(e.source) && nodes.has(e.target));
  edges.forEach((e) => {
    neighbours.get(e.source).add(e.target);
    neighbours.get(e.target).add(e.source);
  });
  const degree = new Map();
  graph.nodes.forEach((n) => degree.set(n.id, neighbours.get(n.id).size));

  const hidden = new Set();
  const hiddenThemes = new Set();
  let visible = new Set();
  let focus = null;
  let hover = null;

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
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));

  const shown = () =>
    [...visible].filter(
      (id) => !hidden.has(nodes.get(id).kind) && !hiddenThemes.has(themeOf(id))
    );

  /* ---- which nodes start on screen ---- */

  // Coloured by theme, a graph too large to draw whole opens on its themes: a
  // connected slice of each grown from its hub.
  function themeOverview(limit) {
    const share = Math.max(1, Math.floor(limit / Math.max(1, COLOURED)));
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
    if (graph.nodes.length <= MAX_NODES) return new Set(graph.nodes.map((n) => n.id));
    const limit = Math.floor(MAX_NODES / 2);
    if (colourBy === "theme" && COLOURED) return themeOverview(limit);
    const picked = new Set(graph.nodes.filter((n) => n.kind === "document").map((n) => n.id));
    const queue = [...picked];
    while (queue.length && picked.size < limit) {
      for (const next of neighbours.get(queue.shift())) {
        if (picked.size >= limit) break;
        if (!picked.has(next)) {
          picked.add(next);
          queue.push(next);
        }
      }
    }
    if (!picked.size) graph.nodes.slice(0, limit).forEach((n) => picked.add(n.id));
    return picked;
  }

  /* ---- the simulation ---- */

  const ALPHA_MIN = 0.002;
  const ALPHA_DECAY = 1 - Math.pow(ALPHA_MIN, 1 / 220);
  const VELOCITY = 0.6;
  // Spacing for a graph of thousands. A small, dense one - the operations
  // layer is a few dozen nodes with most of them linked - is spread wider in
  // rebuild(), or its labels land on top of each other.
  let LINK_DISTANCE = 30;
  let CHARGE = -38;
  const THETA2 = 0.81;
  const DISTANCE_MAX2 = 600 * 600;
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));

  let sim = new Map();
  let bodies = [];
  let links = [];
  let pulses = [];
  let hubs = new Set();
  let alpha = 1;
  let alphaTarget = 0;

  // Each theme gets a region on a circle, so the themes bloom apart as
  // separate galaxies instead of untangling from one knot.
  function home(id) {
    const index = colourBy === "theme" ? themeOf(id) : OTHER;
    if (index === OTHER || !COLOURED) return { x: 0, y: 0 };
    const spread = 7 * Math.sqrt(Math.max(1, visible.size));
    const angle = (index / COLOURED) * Math.PI * 2 - Math.PI / 2;
    return { x: Math.cos(angle) * spread, y: Math.sin(angle) * spread };
  }

  function bodyFor(id, near) {
    if (sim.has(id)) return sim.get(id);
    let x;
    let y;
    if (near) {
      const angle = Math.random() * Math.PI * 2;
      x = near.x + Math.cos(angle) * 14;
      y = near.y + Math.sin(angle) * 14;
    } else {
      // Phyllotaxis, as d3 seeds: an even disc, nothing on top of anything.
      const i = sim.size;
      const radius = 6 * Math.sqrt(0.5 + i);
      const centre = home(id);
      x = centre.x * 0.5 + radius * Math.cos(i * GOLDEN);
      y = centre.y * 0.5 + radius * Math.sin(i * GOLDEN);
    }
    const body = { id, x, y, vx: 0, vy: 0, fx: null, fy: null, r: 3, colour: "#888888" };
    sim.set(id, body);
    return body;
  }

  // The arrays the simulation and the renderer walk, rebuilt whenever what is
  // on screen or how it is coloured changes.
  function rebuild() {
    const ids = shown();
    const set = new Set(ids);
    bodies = ids.map((id) => bodyFor(id, null));
    const count = new Map(ids.map((id) => [id, 0]));
    links = [];
    edges.forEach((e) => {
      if (!set.has(e.source) || !set.has(e.target) || e.source === e.target) return;
      count.set(e.source, count.get(e.source) + 1);
      count.set(e.target, count.get(e.target) + 1);
      links.push({ s: sim.get(e.source), t: sim.get(e.target), e });
    });
    links.forEach((link) => {
      const left = count.get(link.s.id);
      const right = count.get(link.t.id);
      link.bias = left / (left + right);
      link.strength = 1 / Math.min(left, right);
    });
    bodies.forEach((body) => {
      // Size carries degree, so the hubs are visible before anything is clicked.
      body.r = Math.min(16, 2.6 + Math.sqrt(count.get(body.id)) * 1.5);
      body.colour = colourOf(body.id);
      const centre = home(body.id);
      body.tx = centre.x;
      body.ty = centre.y;
      body.pull = colourBy === "theme" && themeOf(body.id) !== OTHER ? 0.05 : 0.02;
    });
    hubs = new Set(
      [...ids].sort((a, b) => count.get(b) - count.get(a)).slice(0, HUB_LABELS)
    );
    pulses = links
      .filter((link) => !link.e.inferred)
      .map((link) => [link, count.get(link.s.id) + count.get(link.t.id)])
      .sort((a, b) => b[1] - a[1])
      .slice(0, SIGNALS)
      .map(([link], i) => ({ link, phase: (i * 0.618) % 1, speed: 0.25 + ((i * 7) % 5) * 0.06 }));
    const spread = clamp(Math.sqrt(500 / Math.max(1, bodies.length)), 1, 2.6);
    LINK_DISTANCE = 30 * spread;
    CHARGE = -38 * spread * spread;
    canvas.dataset.nodes = String(bodies.length);
    canvas.dataset.edges = String(links.length);
    canvas.dataset.codeNodes = String(bodies.filter((b) => nodes.get(b.id).kind === "code").length);
    canvas.dataset.codeEdges = String(links.filter((link) => link.e.codeReference).length);
  }

  function quadtree(list) {
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    list.forEach((b) => {
      x0 = Math.min(x0, b.x);
      y0 = Math.min(y0, b.y);
      x1 = Math.max(x1, b.x);
      y1 = Math.max(y1, b.y);
    });
    const root = cell(x0, y0, Math.max(x1 - x0, y1 - y0) + 1);
    list.forEach((b) => insert(root, b, 0));
    accumulate(root);
    return root;
  }

  function cell(x, y, s) {
    return { x, y, s, body: null, extra: null, kids: null, mass: 0, cx: 0, cy: 0 };
  }

  function insert(c, b, depth) {
    if (!c.kids) {
      if (!c.body) {
        c.body = b;
        return;
      }
      if (depth > 30) {
        // Coincident points: kept together in one leaf rather than split forever.
        (c.extra || (c.extra = [])).push(b);
        return;
      }
      const old = c.body;
      c.body = null;
      c.kids = [null, null, null, null];
      place(c, old, depth);
    }
    place(c, b, depth);
  }

  function place(c, b, depth) {
    const half = c.s / 2;
    const qx = b.x >= c.x + half ? 1 : 0;
    const qy = b.y >= c.y + half ? 1 : 0;
    const k = qy * 2 + qx;
    if (!c.kids[k]) c.kids[k] = cell(c.x + qx * half, c.y + qy * half, half);
    insert(c.kids[k], b, depth + 1);
  }

  // The two functions below run for every node on every tick, so they are
  // written as plain loops: a closure or a spread array here is a few thousand
  // allocations a frame for the collector to chase.
  function accumulate(c) {
    if (c.kids) {
      for (let i = 0; i < 4; i += 1) {
        const kid = c.kids[i];
        if (!kid) continue;
        accumulate(kid);
        c.mass += kid.mass;
        c.cx += kid.cx * kid.mass;
        c.cy += kid.cy * kid.mass;
      }
    } else {
      c.mass = 1;
      c.cx = c.body.x;
      c.cy = c.body.y;
      if (c.extra) {
        for (let i = 0; i < c.extra.length; i += 1) {
          c.mass += 1;
          c.cx += c.extra[i].x;
          c.cy += c.extra[i].y;
        }
      }
    }
    c.cx /= c.mass;
    c.cy /= c.mass;
  }

  function push(b, dx, dy, d2, mass) {
    const w = (CHARGE * mass * alpha) / Math.max(d2, 1);
    b.vx += dx * w;
    b.vy += dy * w;
  }

  function pushFrom(m, b) {
    if (m === b) return;
    let mx = m.x - b.x;
    let my = m.y - b.y;
    if (mx === 0 && my === 0) {
      mx = (Math.random() - 0.5) * 1e-3;
      my = (Math.random() - 0.5) * 1e-3;
    }
    const m2 = mx * mx + my * my;
    if (m2 < DISTANCE_MAX2) push(b, mx, my, m2, 1);
  }

  function repel(c, b) {
    const dx = c.cx - b.x;
    const dy = c.cy - b.y;
    const d2 = dx * dx + dy * dy;
    if (c.kids) {
      if ((c.s * c.s) / THETA2 < d2) {
        if (d2 < DISTANCE_MAX2) push(b, dx, dy, d2, c.mass);
        return;
      }
      for (let i = 0; i < 4; i += 1) if (c.kids[i]) repel(c.kids[i], b);
      return;
    }
    pushFrom(c.body, b);
    if (c.extra) for (let i = 0; i < c.extra.length; i += 1) pushFrom(c.extra[i], b);
  }

  function tick() {
    links.forEach((link) => {
      const { s, t } = link;
      let x = t.x + t.vx - s.x - s.vx || 1e-6;
      let y = t.y + t.vy - s.y - s.vy || 1e-6;
      let length = Math.sqrt(x * x + y * y);
      length = ((length - LINK_DISTANCE) / length) * alpha * link.strength;
      x *= length;
      y *= length;
      t.vx -= x * link.bias;
      t.vy -= y * link.bias;
      s.vx += x * (1 - link.bias);
      s.vy += y * (1 - link.bias);
    });
    if (bodies.length > 1) {
      const tree = quadtree(bodies);
      bodies.forEach((b) => repel(tree, b));
    }
    bodies.forEach((b) => {
      b.vx += (b.tx - b.x) * b.pull * alpha;
      b.vy += (b.ty - b.y) * b.pull * alpha;
      if (b.fx !== null) {
        b.x = b.fx;
        b.y = b.fy;
        b.vx = 0;
        b.vy = 0;
      } else {
        b.vx *= VELOCITY;
        b.vy *= VELOCITY;
        b.x += b.vx;
        b.y += b.vy;
      }
    });
    alpha += (alphaTarget - alpha) * ALPHA_DECAY;
  }

  const reheat = (to) => {
    alpha = Math.max(alpha, to);
    wake();
  };

  /* ---- the camera ---- */

  let width = 900;
  let height = 540;
  let ratio = 1;
  // Screen = world * k + (x, y), in CSS pixels.
  let cam = { x: 450, y: 270, k: 1 };
  let goal = null;
  let autoFit = true;
  let follow = null;

  function fitGoal() {
    if (!bodies.length) return null;
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    bodies.forEach((b) => {
      x0 = Math.min(x0, b.x - b.r);
      y0 = Math.min(y0, b.y - b.r);
      x1 = Math.max(x1, b.x + b.r);
      y1 = Math.max(y1, b.y + b.r);
    });
    const pad = 40;
    const k = clamp(
      Math.min((width - pad * 2) / Math.max(1, x1 - x0), (height - pad * 2) / Math.max(1, y1 - y0)),
      0.05,
      1.4
    );
    return { k, x: width / 2 - ((x0 + x1) / 2) * k, y: height / 2 - ((y0 + y1) / 2) * k };
  }

  function centreGoal(body, k) {
    return { k, x: width / 2 - body.x * k, y: height / 2 - body.y * k };
  }

  const toWorld = (sx, sy) => ({ x: (sx - cam.x) / cam.k, y: (sy - cam.y) / cam.k });

  function stopCamera() {
    autoFit = false;
    follow = null;
    goal = null;
  }

  function zoomAt(sx, sy, factor) {
    stopCamera();
    const k = clamp(cam.k * factor, 0.05, 8);
    cam.x = sx - ((sx - cam.x) * k) / cam.k;
    cam.y = sy - ((sy - cam.y) * k) / cam.k;
    cam.k = k;
    wake();
  }

  function moveCamera() {
    if (autoFit) goal = fitGoal();
    else if (follow && sim.has(follow)) goal = centreGoal(sim.get(follow), goal ? goal.k : cam.k);
    if (!goal) return false;
    const step = reducedMotion ? 1 : 0.14;
    cam.k += (goal.k - cam.k) * step;
    cam.x += (goal.x - cam.x) * step;
    cam.y += (goal.y - cam.y) * step;
    const still =
      Math.abs(goal.k - cam.k) < 0.001 && Math.abs(goal.x - cam.x) + Math.abs(goal.y - cam.y) < 0.5;
    if (still && !autoFit && !follow) goal = null;
    return !still;
  }

  /* ---- drawing ---- */

  // A glow is one pre-rendered sprite per colour, stamped under each node: a
  // radial gradient per node per frame is what would make this slow.
  const sprites = new Map();
  function sprite(colour, strength) {
    const key = `${colour}|${strength}`;
    if (sprites.has(key)) return sprites.get(key);
    const size = 64;
    const surface = document.createElement("canvas");
    surface.width = size;
    surface.height = size;
    const paint = surface.getContext("2d");
    const [r, g, b] = toRgb(colour);
    const gradient = paint.createRadialGradient(32, 32, 0, 32, 32, 32);
    gradient.addColorStop(0, `rgba(${r},${g},${b},${strength})`);
    gradient.addColorStop(0.3, `rgba(${r},${g},${b},${strength * 0.4})`);
    gradient.addColorStop(1, `rgba(${r},${g},${b},0)`);
    paint.fillStyle = gradient;
    paint.fillRect(0, 0, size, size);
    sprites.set(key, surface);
    return surface;
  }

  //: How much of a long label survives, and from which end. Documents imported
  //: from a folder share their prefix; the filename at the end is what anyone
  //: recognises, so both ends are kept.
  const LABEL_MAX = 30;
  const LABEL_HEAD = 8;
  function shorten(text) {
    if (text.length <= LABEL_MAX) return text;
    return `${text.slice(0, LABEL_HEAD)}…${text.slice(-(LABEL_MAX - LABEL_HEAD - 1))}`;
  }

  // 0 with nothing hovered or selected, easing to 1 when something is: the
  // rest of the graph fades rather than snapping out.
  let highlight = 0;

  function litSet() {
    const centre = hover || focus;
    if (!centre || !sim.has(centre)) return null;
    const set = new Set([centre]);
    (neighbours.get(centre) || new Set()).forEach((id) => set.add(id));
    return set;
  }

  function toCamera(paint) {
    paint.setTransform(ratio * cam.k, 0, 0, ratio * cam.k, ratio * cam.x, ratio * cam.y);
  }

  // Signals: a sample of the busiest connections fire continuously; a hovered
  // or selected node fires along every connection it has.
  function drawSignals(paint, time, { onScreen, px, lit, centre, fade }) {
    if (reducedMotion) return;
    paint.globalCompositeOperation = COLORS.blend;
    const seconds = time / 1000;
    const size = 6 * px;
    const stamp = (from, to, phase, colour) => {
      const x = from.x + (to.x - from.x) * phase;
      const y = from.y + (to.y - from.y) * phase;
      paint.drawImage(sprite(colour, 0.95), x - size, y - size, size * 2, size * 2);
    };
    paint.globalAlpha = lit ? 0.25 * fade : 0.9;
    pulses.forEach(({ link, phase, speed }) => {
      if (!onScreen(link.s, 50) && !onScreen(link.t, 50)) return;
      stamp(link.s, link.t, (seconds * speed + phase) % 1, mixHex(link.t.colour, "#ffffff", 0.45));
    });
    if (lit) {
      paint.globalAlpha = highlight;
      const from = sim.get(centre);
      links.forEach(({ s, t }, i) => {
        if (s.id !== centre && t.id !== centre) return;
        const to = s.id === centre ? t : s;
        stamp(from, to, (seconds * 0.7 + i * 0.137) % 1, COLORS.accent);
      });
    }
    paint.globalCompositeOperation = "source-over";
    paint.globalAlpha = 1;
  }

  function draw(paint, time, inlineSignals) {
    paint.setTransform(ratio, 0, 0, ratio, 0, 0);
    paint.clearRect(0, 0, width, height);
    if (!bodies.length) {
      paint.fillStyle = COLORS.label;
      paint.font = "13px system-ui, sans-serif";
      paint.textAlign = "center";
      paint.fillText("Nothing to show with these filters.", width / 2, height / 2);
      return {};
    }
    const k = cam.k;
    const px = 1 / k;
    toCamera(paint);
    const left = -cam.x / k;
    const top = -cam.y / k;
    const right = (width - cam.x) / k;
    const bottom = (height - cam.y) / k;
    const onScreen = (b, margin) =>
      b.x + margin > left && b.x - margin < right && b.y + margin > top && b.y - margin < bottom;

    const lit = litSet();
    const centre = lit ? hover || focus : null;
    const fade = 1 - highlight * (1 - COLORS.dim);
    const view = { onScreen, px, lit, centre, fade };

    // Edges: one path per colour, tinted by the cluster they belong to, so a
    // theme reads as a galaxy of its own colour rather than a grey web.
    const groups = new Map();
    const mixed = new Path2D();
    const inferred = new Path2D();
    const confirmed = new Path2D();
    const lighted = new Path2D();
    const codeReferences = new Path2D();
    links.forEach(({ s, t, e }) => {
      let path;
      if (e.codeReference) path = codeReferences;
      else if (lit && (s.id === centre || t.id === centre)) path = lighted;
      else if (e.confirmed) path = confirmed;
      else if (e.inferred) path = inferred;
      else if (s.colour === t.colour) {
        if (!groups.has(s.colour)) groups.set(s.colour, new Path2D());
        path = groups.get(s.colour);
      } else path = mixed;
      path.moveTo(s.x, s.y);
      path.lineTo(t.x, t.y);
    });
    paint.lineCap = "round";
    paint.lineWidth = px;
    paint.globalAlpha = COLORS.edgeAlpha * 1.4 * fade;
    groups.forEach((path, colour) => {
      paint.strokeStyle = mixHex(colour, COLORS.edge, 0.35);
      paint.stroke(path);
    });
    paint.globalAlpha = COLORS.edgeAlpha * fade;
    paint.strokeStyle = COLORS.edge;
    paint.stroke(mixed);
    // A guessed relation keeps a flat amber dash: it must not look like the
    // same thing as a structural fact.
    paint.globalAlpha = 0.8 * fade;
    paint.strokeStyle = COLORS.inferred;
    paint.setLineDash([4 * px, 3 * px]);
    paint.lineWidth = 1.3 * px;
    paint.stroke(inferred);
    paint.setLineDash([]);
    paint.strokeStyle = COLORS.confirmed;
    paint.lineWidth = 2.4 * px;
    paint.stroke(confirmed);
    if (lit) {
      paint.globalAlpha = 0.35 + 0.6 * highlight;
      paint.strokeStyle = COLORS.accent;
      paint.lineWidth = 1.6 * px;
      paint.stroke(lighted);
    }
    paint.globalAlpha = 0.9;
    paint.strokeStyle = COLORS.code;
    paint.lineWidth = 2 * px;
    paint.stroke(codeReferences);

    // Glows, then the nodes themselves, batched by colour.
    paint.globalCompositeOperation = COLORS.blend;
    bodies.forEach((b) => {
      const glowing = lit ? lit.has(b.id) : true;
      const reach = b.r * (hubs.has(b.id) || b.id === centre ? 3.6 : 3);
      // A glow under three pixels across is invisible and still costs a stamp.
      if (reach * k < 3 || !onScreen(b, reach)) return;
      paint.globalAlpha = glowing ? 1 : fade;
      paint.drawImage(sprite(b.colour, COLORS.glow), b.x - reach, b.y - reach, reach * 2, reach * 2);
    });
    if (inlineSignals) drawSignals(paint, time, view);
    paint.globalCompositeOperation = "source-over";

    const fills = new Map();
    bodies.forEach((b) => {
      if (!onScreen(b, b.r)) return;
      const key = `${b.colour}|${lit && !lit.has(b.id) ? "dim" : "lit"}`;
      if (!fills.has(key)) fills.set(key, new Path2D());
      const path = fills.get(key);
      if (nodes.get(b.id).kind === "code") {
        path.rect(b.x - b.r, b.y - b.r, b.r * 2, b.r * 2);
      } else {
        path.moveTo(b.x + b.r, b.y);
        path.arc(b.x, b.y, b.r, 0, Math.PI * 2);
      }
    });
    fills.forEach((path, key) => {
      const [colour, state] = key.split("|");
      paint.globalAlpha = state === "dim" ? fade : 1;
      paint.fillStyle = mixHex(colour, "#ffffff", 0.12);
      paint.fill(path);
    });
    paint.globalAlpha = 1;
    [focus, hover].forEach((id, index) => {
      const b = id && sim.get(id);
      if (!b) return;
      paint.beginPath();
      paint.arc(b.x, b.y, b.r + 3 * px, 0, Math.PI * 2);
      paint.strokeStyle = index === 0 ? COLORS.accent : COLORS.label;
      paint.lineWidth = (index === 0 ? 2 : 1.2) * px;
      paint.stroke();
    });

    // Labels fade in with zoom, bigger nodes first; the busiest few, and
    // whatever is lit, are always named.
    paint.font = `${12 * px}px system-ui, -apple-system, "Segoe UI", sans-serif`;
    paint.textAlign = "center";
    paint.textBaseline = "top";
    paint.lineJoin = "round";
    paint.lineWidth = 3 * px;
    paint.strokeStyle = COLORS.labelHalo;
    paint.fillStyle = COLORS.label;
    bodies.forEach((b) => {
      if (!onScreen(b, 120 * px)) return;
      let visibility = clamp((b.r * k - 4) / 4, 0, 1);
      if (hubs.has(b.id)) visibility = Math.max(visibility, 0.9);
      if (lit) visibility = lit.has(b.id) ? Math.max(visibility, highlight) : visibility * fade;
      if (b.id === focus) visibility = 1;
      if (visibility < 0.03) return;
      const text = shorten(nodes.get(b.id).label);
      paint.globalAlpha = visibility;
      paint.strokeText(text, b.x, b.y + b.r + 3 * px);
      paint.fillText(text, b.x, b.y + b.r + 3 * px);
    });
    paint.globalAlpha = 1;
    return view;
  }

  function drawMinimap() {
    if (!minimap || !minimap.getContext) return;
    const box = minimap.getBoundingClientRect();
    if (!box.width) return;
    const paint = minimap.getContext("2d");
    if (minimap.width !== Math.round(box.width * ratio)) {
      minimap.width = Math.round(box.width * ratio);
      minimap.height = Math.round(box.height * ratio);
    }
    paint.setTransform(1, 0, 0, 1, 0, 0);
    paint.clearRect(0, 0, minimap.width, minimap.height);
    const extent = fitGoal();
    if (!extent) return;
    // The minimap is the fitted view, scaled down to its own size.
    const scale = Math.min(box.width / width, box.height / height);
    minimap.dataset.k = String(extent.k * scale);
    minimap.dataset.x = String(extent.x * scale);
    minimap.dataset.y = String(extent.y * scale);
    paint.setTransform(ratio * extent.k * scale, 0, 0, ratio * extent.k * scale, ratio * extent.x * scale, ratio * extent.y * scale);
    const dot = 2 / (extent.k * scale);
    bodies.forEach((b) => {
      paint.fillStyle = b.colour;
      paint.fillRect(b.x - dot / 2, b.y - dot / 2, dot, dot);
    });
    const view = { x: -cam.x / cam.k, y: -cam.y / cam.k, w: width / cam.k, h: height / cam.k };
    paint.strokeStyle = COLORS.accent;
    paint.lineWidth = 1.5 / (extent.k * scale);
    paint.strokeRect(view.x, view.y, view.w, view.h);
  }

  /* ---- the frame loop ---- */

  let frame = null;
  let frames = 0;
  let last = 0;

  // Anything that changes the picture calls this; it also marks the cached
  // scene stale, since a pan or a hover does not move the simulation.
  let dirty = true;
  function wake() {
    dirty = true;
    if (frame === null) frame = window.requestAnimationFrame(step);
  }

  // Once the layout is still, the whole scene is painted once into a bitmap
  // and each later frame is that bitmap plus the moving signals. Without it a
  // settled graph of thousands of nodes repainted everything thirty times a
  // second for the sake of a few dozen dots.
  const scene = document.createElement("canvas");
  const sceneCtx = scene.getContext("2d");
  let sceneView = null;

  function step(time) {
    frame = null;
    const simulating = alpha >= ALPHA_MIN || alphaTarget > 0;
    if (simulating) {
      // Reduced motion: settle in as few frames as the time allows, so the
      // layout arrives rather than drifts.
      const started = performance.now();
      do tick();
      while (reducedMotion && alpha >= ALPHA_MIN && performance.now() - started < 14);
    }
    const panning = moveCamera();
    const target = hover || focus ? 1 : 0;
    highlight += (target - highlight) * (reducedMotion ? 1 : 0.18);
    const easing = Math.abs(target - highlight) > 0.01;
    const signalling = !reducedMotion && pulses.length > 0;
    const busy = simulating || panning || easing || !!dragging || dirty;
    dirty = false;
    if (busy) {
      draw(ctx, time, true);
      sceneView = null;
      frames += 1;
      if (frames % 12 === 0) drawMinimap();
    } else if (!sceneView) {
      if (scene.width !== canvas.width || scene.height !== canvas.height) {
        scene.width = canvas.width;
        scene.height = canvas.height;
      }
      sceneView = draw(sceneCtx, time, false);
      drawMinimap();
    }
    if (!busy && sceneView && time - last > 32) {
      // Signals alone run at half rate: they are atmosphere, and a settled
      // graph should not cost a laptop its battery.
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(scene, 0, 0);
      if (signalling && bodies.length) {
        toCamera(ctx);
        drawSignals(ctx, time, sceneView);
      }
      last = time;
    }
    canvas.dataset.state = simulating ? "running" : "settled";
    if (busy || signalling || !sceneView) frame = window.requestAnimationFrame(step);
  }

  /* ---- what is on screen ---- */

  function count() {
    counter.textContent =
      `${bodies.length} of ${graph.nodes.length} nodes shown` +
      (visible.size >= MAX_NODES ? " · display limit reached" : "");
    expandButton.disabled =
      visible.size >= MAX_NODES || visible.size >= graph.nodes.length;
  }

  function refresh(heat) {
    rebuild();
    count();
    reheat(heat);
  }

  function expand(id) {
    let added = 0;
    const near = sim.get(id) || null;
    for (const next of neighbours.get(id) || []) {
      if (visible.size >= MAX_NODES) break;
      if (!visible.has(next)) {
        visible.add(next);
        bodyFor(next, near);
        added += 1;
      }
    }
    return added;
  }

  function expandAll() {
    for (const id of shown()) {
      expand(id);
      if (visible.size >= MAX_NODES) break;
    }
  }

  function start() {
    codeControl("Select a node to find related code.");
    clearCodeOverlay();
    buildFilters();
    options();
    sim = new Map();
    focus = null;
    hover = null;
    follow = null;
    canvas.dataset.focus = "";
    visible = overview();
    autoFit = true;
    goal = null;
    alpha = 1;
    rebuild();
    count();
    wake();
  }

  function focusOn(id) {
    focus = id;
    canvas.dataset.focus = id;
    if (!visible.has(id)) {
      visible.add(id);
      bodyFor(id, null);
    }
    expand(id);
    hidden.delete(nodes.get(id).kind);
    hiddenThemes.delete(themeOf(id));
    syncFilters();
    inspect(id);
    refresh(0.25);
    autoFit = false;
    follow = id;
    goal = centreGoal(sim.get(id), Math.max(cam.k, 1.6));
  }

  function inspect(id) {
    codeControl("Select a knowledge node to find related code.");
    const node = nodes.get(id);
    inspector.replaceChildren();
    const title = document.createElement("h2");
    title.textContent = node.label;
    inspector.append(title);

    const kind = document.createElement("p");
    kind.className = "inspector-kind";
    kind.textContent = node.kind;
    inspector.append(kind);
    if (node.kind === "code") {
      const open = document.createElement("a");
      open.href = node.code.url;
      open.textContent = "Open in Code Graph";
      const detail = document.createElement("p");
      detail.textContent = `${node.code.repository} · commit ${node.code.commit.slice(0, 8)}`;
      inspector.append(open, detail);
      const back = document.createElement("button");
      back.type = "button";
      back.className = "button secondary";
      back.textContent = "Back to knowledge node";
      back.addEventListener("click", () => focusOn(node.knowledgeNode));
      inspector.append(back);
    }

    if (clusters && clusters.membership[id] !== undefined) {
      const inTheme = document.createElement("p");
      inTheme.className = "inspector-theme";
      inTheme.textContent = `Theme: ${clusters.themes[clusters.membership[id]].label}`;
      inspector.append(inTheme);
    }

    if (node.knowledge_id) {
      const open = document.createElement("a");
      open.textContent = "Open the record";
      open.href = `../knowledge/${node.knowledge_id}/`;
      inspector.append(open);
    }

    const related = edges.filter((e) => e.source === id || e.target === id);
    const inferred = related.filter((e) => e.inferred).length;
    const confirmed = related.filter((e) => e.confirmed).length;

    const summary = document.createElement("p");
    summary.textContent =
      `${related.length} relationship(s), ` +
      (graph.layer === "operations"
        ? `${related.length - confirmed} derived by rules, ${confirmed} confirmed by a person. `
        : `${inferred} AI-inferred. `) +
      "Showing up to 30 evidence entries.";
    inspector.append(summary);

    const expandHere = document.createElement("button");
    expandHere.type = "button";
    expandHere.className = "button secondary";
    expandHere.textContent = "Expand neighbours";
    expandHere.addEventListener("click", () => {
      expand(id);
      refresh(0.4);
    });
    inspector.append(expandHere);

    showRelatedCode(id);
    related.slice(0, 30).forEach((e) => {
      const block = document.createElement("div");
      block.className = "graph-evidence";
      const relation = document.createElement("strong");
      // Which way the relation reads: "INC2 is similar to INC1", not just "is similar to".
      const other = nodes.get(e.source === id ? e.target : e.source);
      relation.textContent =
        e.source === id ? `${e.relation} ${other.label}` : `${other.label} ${e.relation} ${node.label}`;
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
      if (e.codeReference) {
        const explanation = document.createElement("p");
        explanation.textContent = "Explicit reference; this does not prove the requirement is satisfied.";
        block.append(explanation);
        for (const item of e.references) {
          const proof = document.createElement("pre");
          proof.textContent = `${item.reason}\n${item.evidence}` +
            (item.code_evidence ? `\nCode line ${item.code_line}: ${item.code_evidence}` : "");
          block.append(proof);
        }
      }
      inspector.append(block);
    });
  }

  async function showRelatedCode(id) {
    const endpoint = canvas.dataset.relatedCodeUrl;
    if (!endpoint || nodes.get(id).kind === "code") return;
    codeControl("Checking related code…");
    const panel = document.createElement("section");
    const heading = document.createElement("h3");
    heading.textContent = "Related code";
    const status = document.createElement("p");
    status.textContent = "Checking explicit references…";
    panel.append(heading, status);
    inspector.append(panel);
    try {
      const response = await fetch(`${endpoint}?node=${encodeURIComponent(id)}`);
      if (!panel.isConnected || focus !== id) return;
      if (response.status === 403 || response.status === 404) {
        codeControl("Related code is unavailable for this node.");
        panel.remove();
        return;
      }
      if (!response.ok) throw new Error("Links unavailable");
      const data = await response.json();
      if (!panel.isConnected || focus !== id) return;
      status.textContent = data.links.length
        ? "Explicit references, not proof that a requirement is satisfied."
        : "No verified code references found in the indexed snapshots.";
      if (data.links.length) {
        codeControl(`${new Set(data.links.map((item) => item.file_id)).size} related code files.`, () => {
          const shownCount = showCodeOverlay(id, data.links);
          status.textContent = `${shownCount} code file(s) connected on the canvas. ` +
            "Showing up to 30 files; references do not prove implementation correctness.";
          codeStatus.textContent = `${shownCount} code files shown on the canvas.`;
        });
      } else {
        codeControl("No verified code references found for this node.");
      }
      for (const item of data.links) {
        const block = document.createElement("div");
        block.className = "graph-evidence";
        const open = document.createElement("a");
        open.href = item.url;
        open.textContent = item.path;
        const reason = document.createElement("p");
        reason.textContent = `${item.reason} · ${item.repository} · ${item.commit.slice(0, 8)}`;
        const source = document.createElement("a");
        source.href = item.source_url;
        source.textContent = `${item.source_title} · line ${item.source_line} · knowledge v${item.revision}`;
        const evidence = document.createElement("pre");
        evidence.textContent = item.evidence;
        block.append(open, reason, source, evidence);
        if (item.code_evidence) {
          const code = document.createElement("pre");
          code.textContent = `Code line ${item.code_line}: ${item.code_evidence}`;
          block.append(code);
        }
        panel.append(block);
      }
      if (data.limited) {
        const limit = document.createElement("p");
        limit.textContent = "Reference scan reached its limit; additional links may exist.";
        panel.append(limit);
      }
    } catch {
      if (panel.isConnected && focus === id) {
        status.textContent = "Related code could not be loaded. Select the node to retry.";
        codeControl(status.textContent);
      }
    }
  }

  function clearCodeOverlay() {
    const codeIds = new Set(graph.nodes.filter((n) => n.kind === "code").map((n) => n.id));
    if (!codeIds.size) return;
    for (let i = edges.length - 1; i >= 0; i -= 1) {
      if (edges[i].codeReference) edges.splice(i, 1);
    }
    graph.nodes = graph.nodes.filter((n) => !codeIds.has(n.id));
    codeIds.forEach((id) => {
      nodes.delete(id);
      neighbours.delete(id);
      degree.delete(id);
      sim.delete(id);
      visible.delete(id);
    });
    neighbours.forEach((adjacent, id) => {
      codeIds.forEach((codeId) => adjacent.delete(codeId));
      degree.set(id, adjacent.size);
    });
  }

  function showCodeOverlay(origin, references) {
    clearCodeOverlay();
    const grouped = new Map();
    for (const item of references) {
      const id = `code:${item.snapshot}:${item.file_id}`;
      if (!grouped.has(id) && grouped.size >= 30) continue;
      if (!grouped.has(id)) grouped.set(id, []);
      grouped.get(id).push(item);
    }
    const near = sim.get(origin);
    grouped.forEach((items, id) => {
      const item = items[0];
      const node = { id, kind: "code", label: item.path, code: item, knowledgeNode: origin };
      nodes.set(id, node);
      graph.nodes.push(node);
      neighbours.set(id, new Set([origin]));
      neighbours.get(origin).add(id);
      degree.set(id, 1);
      bodyFor(id, near);
      edges.push({
        source: origin, target: id, relation: "references code", codeReference: true,
        knowledge_id: item.entry_id, line: item.source_line, references: items,
      });
    });
    degree.set(origin, neighbours.get(origin).size);
    // A small connected view makes the requirement and its code legible together.
    // Overview restores the original graph; the exported snapshot is never changed.
    visible = new Set([origin, ...neighbours.get(origin)]);
    visible.forEach((id) => {
      hidden.delete(nodes.get(id).kind);
      hiddenThemes.delete(themeOf(id));
    });
    hover = null;
    focus = origin;
    follow = null;
    autoFit = true;
    buildFilters();
    options();
    refresh(0.6);
    return grouped.size;
  }

  /* ---- controls ---- */

  // The filter follows the colouring: coloured by theme, it lists the themes;
  // coloured by kind, it lists the kinds. The filter is folded away, so every
  // entry is repeated in the legend on the canvas, which stays open.
  function legendItem(group, text, colour, line) {
    if (!group) return;
    const item = document.createElement("span");
    item.className = "legend-item";
    item.title = text;
    const swatch = document.createElement("i");
    swatch.className = line ? `legend-line${line === "dashed" ? " dashed" : ""}` : "legend-swatch";
    if (line) swatch.style.borderTopColor = colour;
    else swatch.style.background = colour;
    const name = document.createElement("span");
    name.textContent = text;
    item.append(swatch, name);
    group.append(item);
  }

  function legendHeading(group, text) {
    if (!group) return;
    const heading = document.createElement("b");
    heading.textContent = text;
    group.append(heading);
  }

  // What a line means differs by layer: in the knowledge graph a dash is an
  // AI-inferred relation, in the operations layer green is a person's verdict.
  function buildEdgeLegend() {
    if (!legendEdges) return;
    legendEdges.replaceChildren();
    legendHeading(legendEdges, "Connections");
    if (graph.layer === "operations") {
      legendItem(legendEdges, "Derived by a rule", COLORS.edge, "solid");
      legendItem(legendEdges, "Confirmed cause", COLORS.confirmed, "solid");
      return;
    }
    legendItem(legendEdges, "Structure", COLORS.edge, "solid");
    if (edges.some((e) => e.codeReference)) {
      legendItem(legendEdges, "Code reference", COLORS.code, "solid");
    }
    if (edges.some((e) => e.inferred)) {
      legendItem(legendEdges, "AI-inferred", COLORS.inferred, "dashed");
    }
  }

  function filterItem(text, colour, checked, onChange, data) {
    legendItem(legendNodes, text, colour);
    const label = document.createElement("label");
    label.className = "graph-filter";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = checked;
    Object.assign(box.dataset, data);
    box.addEventListener("change", () => {
      onChange(box.checked);
      refresh(0.3);
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
    if (legendNodes) {
      legendNodes.replaceChildren();
      legendNodes.classList.toggle("by-theme", colourBy === "theme");
      legendHeading(legendNodes, colourBy === "theme" ? "Themes" : "Kinds");
    }
    buildEdgeLegend();
    if (colourBy === "theme") {
      if (graph.nodes.some((n) => n.kind === "code")) {
        filterItem("Code file", COLORS.code, !hidden.has("code"), toggle(hidden, "code"), {
          kind: "code",
        });
      }
      for (let index = 0; index < COLOURED; index += 1) {
        filterItem(
          clusters.themes[index].label,
          THEME_PALETTES[theme][index],
          !hiddenThemes.has(index),
          toggle(hiddenThemes, index),
          { theme: String(index) }
        );
      }
      if (graph.nodes.some((n) => themeOf(n.id) === OTHER)) {
        filterItem("Other", COLORS.other, !hiddenThemes.has(OTHER), toggle(hiddenThemes, OTHER), {
          theme: String(OTHER),
        });
      }
      return;
    }
    KINDS.filter((kind) => graph.nodes.some((n) => n.kind === kind)).forEach((kind) => {
      filterItem(
        ({ entity: "AI entity", kb: "KB article", code: "Code file" })[kind] || kind,
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

  function nodeAt(sx, sy) {
    const point = toWorld(sx, sy);
    const slack = 4 / cam.k;
    let best = null;
    let nearest = Infinity;
    bodies.forEach((b) => {
      const dx = b.x - point.x;
      const dy = b.y - point.y;
      const d2 = dx * dx + dy * dy;
      const reach = b.r + slack;
      if (d2 <= reach * reach && d2 < nearest) {
        best = b;
        nearest = d2;
      }
    });
    return best;
  }

  const local = (event) => {
    const box = canvas.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };

  // Drag a node and its neighbours follow on their springs; drag the
  // background to pan; click a node to inspect it.
  let dragging = null;
  canvas.addEventListener("pointerdown", (event) => {
    const at = local(event);
    const body = nodeAt(at.x, at.y);
    if (body) {
      dragging = { body, start: at, moved: false };
      body.fx = body.x;
      body.fy = body.y;
    } else {
      dragging = { pan: true, start: at, origin: { x: cam.x, y: cam.y }, moved: false };
    }
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    const at = local(event);
    if (!dragging) {
      const body = nodeAt(at.x, at.y);
      const next = body ? body.id : null;
      if (next !== hover) {
        hover = next;
        canvas.classList.toggle("hovering", !!hover);
        wake();
      }
      return;
    }
    const dx = at.x - dragging.start.x;
    const dy = at.y - dragging.start.y;
    if (!dragging.moved && Math.abs(dx) + Math.abs(dy) > 3) {
      dragging.moved = true;
      if (!dragging.pan) alphaTarget = 0.3;
    }
    if (!dragging.moved) return;
    if (dragging.pan) {
      stopCamera();
      canvas.classList.add("panning");
      cam.x = dragging.origin.x + dx;
      cam.y = dragging.origin.y + dy;
    } else {
      if (autoFit) stopCamera();
      const point = toWorld(at.x, at.y);
      dragging.body.fx = point.x;
      dragging.body.fy = point.y;
    }
    wake();
  });

  const release = (event) => {
    if (dragging && !dragging.pan) {
      dragging.body.fx = null;
      dragging.body.fy = null;
      alphaTarget = 0;
      if (!dragging.moved) focusOn(dragging.body.id);
    }
    dragging = null;
    canvas.classList.remove("panning");
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    wake();
  };
  canvas.addEventListener("pointerup", release);
  canvas.addEventListener("pointercancel", release);
  canvas.addEventListener("pointerleave", () => {
    if (hover && !dragging) {
      hover = null;
      canvas.classList.remove("hovering");
      wake();
    }
  });

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const at = local(event);
      zoomAt(at.x, at.y, Math.exp(-event.deltaY * 0.0015));
    },
    { passive: false }
  );

  // The canvas takes the keyboard too: the node list above is the way to a
  // node, and these move the view.
  canvas.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: [40, 0], ArrowRight: [-40, 0], ArrowUp: [0, 40], ArrowDown: [0, -40] };
    if (moves[event.key]) {
      stopCamera();
      cam.x += moves[event.key][0];
      cam.y += moves[event.key][1];
    } else if (event.key === "+" || event.key === "=") zoomAt(width / 2, height / 2, 1.25);
    else if (event.key === "-") zoomAt(width / 2, height / 2, 0.8);
    else if (event.key === "0") {
      follow = null;
      autoFit = true;
    } else if (event.key === "Escape") {
      focus = null;
      follow = null;
      canvas.dataset.focus = "";
    } else return;
    event.preventDefault();
    wake();
  });

  if (minimap) {
    minimap.addEventListener("click", (event) => {
      const box = minimap.getBoundingClientRect();
      const k = Number(minimap.dataset.k || 0);
      if (!k) return;
      const x = (event.clientX - box.left - Number(minimap.dataset.x)) / k;
      const y = (event.clientY - box.top - Number(minimap.dataset.y)) / k;
      autoFit = false;
      follow = null;
      goal = { k: cam.k, x: width / 2 - x * cam.k, y: height / 2 - y * cam.k };
      wake();
    });
  }

  if (themeButton) {
    const applyTheme = () => {
      COLORS = PALETTES[theme];
      canvas.setAttribute("data-canvas", theme);
      themeButton.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      const next = theme === "dark" ? "light" : "dark";
      themeButton.setAttribute("aria-label", `Switch the canvas to ${next}`);
      themeButton.setAttribute("title", `Switch the canvas to ${next}`);
      // Positions live in the simulation, so the camera and the expansion
      // survive; only the colours are taken again.
      buildFilters();
      rebuild();
      wake();
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
        COLORS = PALETTES[theme];
      }
    } catch (failure) {
      /* no stored preference is the normal case */
    }
    canvas.setAttribute("data-canvas", theme);
  }

  search.addEventListener("input", options);
  select.addEventListener("change", () => {
    if (select.value) focusOn(select.value);
  });
  expandButton.addEventListener("click", () => {
    expandAll();
    refresh(0.5);
  });
  resetButton.addEventListener("click", () => {
    search.value = "";
    hidden.clear();
    hiddenThemes.clear();
    syncFilters();
    options();
    start();
  });
  zoomIn.addEventListener("click", () => zoomAt(width / 2, height / 2, 1.25));
  zoomOut.addEventListener("click", () => zoomAt(width / 2, height / 2, 0.8));
  zoomFit.addEventListener("click", () => {
    follow = null;
    autoFit = true;
    wake();
  });

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
      // the regions depend on the colouring, so this starts over.
      hidden.clear();
      hiddenThemes.clear();
      buildFilters();
      start();
    });
  }

  // The backing store follows the element's size and the screen's density, so
  // lines stay one pixel wide on a high-DPI display and in focus mode.
  function resize() {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const before = { width, height };
    width = box.width;
    height = box.height;
    // Capped at 2: past that a thousands-of-nodes canvas pays for pixels
    // nobody can tell apart.
    ratio = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * ratio);
    canvas.height = Math.round(height * ratio);
    cam.x += (width - before.width) / 2;
    cam.y += (height - before.height) / 2;
    wake();
  }
  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);
  window.addEventListener("resize", resize);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) wake();
  });

  resize();
  buildFilters();
  options();
  start();
  const requestedNode = new URLSearchParams(window.location.search).get("node");
  if (requestedNode && nodes.has(requestedNode)) focusOn(requestedNode);
})();
