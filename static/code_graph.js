// The Code Graph explorer.
//
// Everything is drawn with SVG attributes and CSS classes, never inline styles:
// `style-src 'self'` forbids writing `style.left` from script, the same rule
// that shapes modal.js. Pan and zoom are therefore rewrites of the root viewBox,
// and every colour lives in redesign.css.
//
// No library. `script-src 'self'` allows no external origin and the project has
// no build step, so the layout below is written out longhand.
(() => {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const CARD_W = 220;
  const CARD_H = 68;
  const GAP_X = 120; // ranksep
  const GAP_Y = 48; // nodesep
  const MARGIN = 60;
  const LABEL_MIN_ZOOM = 0.75;
  const SUB_COL_GAP = 46;
  const SUB_ROW_GAP = 30;
  const TARGET_ASPECT = 1.6;
  // A file's colour says what it *is* in the graph, which is worth more than
  // repeating the language already named on its chip.
  // Role colours per canvas theme. These land in SVG attributes and in colour
  // arithmetic for the edge gradients, and a custom property is not reliably
  // resolvable in a presentation attribute, so they are real hex here.
  const ROLES_BY_THEME = {
    dark: {
      entry: "#a855f7",
      service: "#3b82f6",
      leaf: "#22c55e",
      orphan: "#64748b",
      circular: "#ef4444",
      border: "#323b52",
    },
    light: {
      entry: "#9333ea",
      service: "#2563eb",
      leaf: "#16a34a",
      orphan: "#64748b",
      circular: "#dc2626",
      border: "#c5cad9",
    },
  };
  const ROLE = {
    entry: "#a855f7", // nothing imports it
    service: "#3b82f6", // imports and is imported
    leaf: "#22c55e", // imported, imports nothing
    orphan: "#64748b", // no connections at all
    circular: "#ef4444", // sits in a dependency cycle
  };
  const BORDER_TONE = "#323b52";
  const ROLE_LABEL = {
    entry: "Entry point — nothing imports it",
    service: "Module — imports and is imported",
    leaf: "Leaf — imported, imports nothing",
    orphan: "No detected links",
    circular: "In a circular dependency",
  };

  const el = (name, attributes, parent) => {
    const node = document.createElementNS(NS, name);
    for (const key in attributes) node.setAttribute(key, attributes[key]);
    if (parent) parent.appendChild(node);
    return node;
  };

  // Choosing a repository submits its form, so the page cannot sit showing one
  // repository while the buttons act on another. Without scripting the View
  // button is still there and still the way to do it.
  const selector = document.querySelector("select[data-auto-submit]");
  if (selector && selector.form) {
    selector.addEventListener("change", () => selector.form.submit());
    const view = document.querySelector("[data-auto-submit-hide]");
    if (view) view.hidden = true;
  }

  const source = document.getElementById("code-graph-data");
  const canvas = document.getElementById("code-graph-canvas");
  if (!source || !canvas) return;

  let graph;
  try {
    graph = JSON.parse(source.textContent);
  } catch (failure) {
    return;
  }
  const nodes = (graph.nodes || []).slice(0, 200);
  if (!nodes.length) {
    const note = canvas.querySelector("text");
    if (note) note.textContent = "No files to draw in this snapshot.";
    return;
  }

  const byId = new Map(nodes.map((node) => [node.id, node]));
  const edges = (graph.edges || []).filter(
    (edge) => byId.has(edge.source) && byId.has(edge.target) && edge.source !== edge.target
  );

  // --------------------------------------------------------------- roles
  // Tarjan, iterative: a deep recursion blows the stack on a large repository,
  // and a cycle is exactly what this has to survive. A component of more than
  // one file, or a self-import, is a circular dependency.

  const order = new Map();
  const low = new Map();
  const onStack = new Set();
  const stack = [];
  const component = new Map();
  let counter = 0;
  let groups = 0;

  const adjacency = new Map(nodes.map((node) => [node.id, []]));
  nodes.forEach((node) => adjacency.set(node.id, []));

  const strongConnect = (root) => {
    const work = [[root, 0]];
    while (work.length) {
      const frame = work[work.length - 1];
      const [id, step] = frame;
      if (step === 0) {
        order.set(id, counter);
        low.set(id, counter);
        counter += 1;
        stack.push(id);
        onStack.add(id);
      }
      const links = adjacency.get(id);
      if (step < links.length) {
        frame[1] += 1;
        const next = links[step];
        if (!order.has(next)) work.push([next, 0]);
        else if (onStack.has(next)) low.set(id, Math.min(low.get(id), order.get(next)));
        continue;
      }
      if (low.get(id) === order.get(id)) {
        const members = [];
        let popped;
        do {
          popped = stack.pop();
          onStack.delete(popped);
          members.push(popped);
        } while (popped !== id);
        if (members.length > 1) {
          groups += 1;
          members.forEach((member) => component.set(member, groups));
        }
      }
      work.pop();
      if (work.length) {
        const parent = work[work.length - 1][0];
        low.set(parent, Math.min(low.get(parent), low.get(id)));
      }
    }
  };

  // ------------------------------------------------------------- layering
  // Longest path with an iteration cap: a node inside a dependency cycle never
  // settles, so the cap is what guarantees this terminates.

  const outgoing = new Map(nodes.map((node) => [node.id, []]));
  const incoming = new Map(nodes.map((node) => [node.id, []]));
  edges.forEach((edge) => {
    outgoing.get(edge.source).push(edge.target);
    incoming.get(edge.target).push(edge.source);
    adjacency.get(edge.source).push(edge.target);
  });
  const selfImport = new Set(
    (graph.edges || []).filter((edge) => edge.source === edge.target).map((edge) => edge.source)
  );
  nodes.forEach((node) => {
    if (!order.has(node.id)) strongConnect(node.id);
  });
  const roleOf = (node) => {
    if (component.has(node.id) || selfImport.has(node.id)) return "circular";
    const inCount = incoming.get(node.id).length;
    const outCount = outgoing.get(node.id).length;
    if (!inCount && !outCount) return "orphan";
    if (!inCount) return "entry";
    if (!outCount) return "leaf";
    return "service";
  };
  // The server decides this over every file in the snapshot. Working it out
  // here would only ever see the drawn subset, which is how the status bar came
  // to state a cycle count that was not merely approximate but wrong. Older
  // snapshots carry no role, so the local pass stays as the fallback for them.
  const roles = new Map(
    nodes.map((node) => [node.id, node.role || roleOf(node)])
  );

  const MAX_LAYER = 24;
  const layer = new Map(nodes.map((node) => [node.id, 0]));
  for (let pass = 0; pass < MAX_LAYER; pass += 1) {
    let moved = false;
    edges.forEach((edge) => {
      const wanted = Math.min(layer.get(edge.source) + 1, MAX_LAYER);
      if (layer.get(edge.target) < wanted) {
        layer.set(edge.target, wanted);
        moved = true;
      }
    });
    if (!moved) break;
  }

  const columns = [];
  nodes.forEach((node) => {
    const index = layer.get(node.id);
    (columns[index] || (columns[index] = [])).push(node);
  });
  for (let index = 0; index < columns.length; index += 1) {
    if (!columns[index]) columns[index] = [];
  }

  // Barycentre sweeps pull each node towards the average row of its neighbours
  // so edges cross less. Three passes is plenty at a 200-node cap.
  //
  // Every comparison falls back to the path. Float barycentres tie constantly -
  // any two nodes with one shared neighbour score identically - and a tie broken
  // by array order means the same snapshot can lay out differently depending on
  // how rows arrived. Keyed on the path it is reproducible.
  const row = new Map();
  columns.forEach((column) => column.forEach((node, index) => row.set(node.id, index)));
  const average = (node, side) => {
    const near = side.get(node.id).filter((id) => row.has(id));
    if (!near.length) return row.get(node.id);
    return near.reduce((total, id) => total + row.get(id), 0) / near.length;
  };
  const byPath = (left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  for (let pass = 0; pass < 3; pass += 1) {
    const side = pass % 2 === 0 ? incoming : outgoing;
    columns.forEach((column) => {
      column.sort((left, right) => {
        const difference = average(left, side) - average(right, side);
        return Math.abs(difference) > 1e-9 ? difference : byPath(left, right);
      });
      column.forEach((node, index) => row.set(node.id, index));
    });
  }

  // Files of one folder sit together, which is what makes a package read as a
  // block instead of as scattered cards.
  columns.forEach((column) => {
    column.sort((left, right) => {
      const folder = (left.folder || "").localeCompare(right.folder || "");
      if (folder !== 0) return folder;
      const settled = row.get(left.id) - row.get(right.id);
      return settled !== 0 ? settled : byPath(left, right);
    });
    column.forEach((node, index) => row.set(node.id, index));
  });

  // --------------------------------------------------------- rank packing
  // One node per row per rank makes a "broom" - many entry points feeding a few
  // shared modules - into a single enormous column inside a landscape window.
  // Keep the rank assignment and the ordering just computed, both of which are
  // what minimise crossings, and only restack: wrap a tall rank into
  // side-by-side sub-columns, centred on a shared axis so chains still read
  // across. Choose the row count by scanning rather than fixing it, because a
  // fixed one helps this shape and hurts flatter graphs.

  const tallestRank = columns.reduce((most, column) => Math.max(most, column.length), 1);
  const shapeFor = (rows) => {
    let width = MARGIN * 2;
    let height = 0;
    columns.forEach((column, index) => {
      const subColumns = Math.max(1, Math.ceil(column.length / rows));
      width += subColumns * CARD_W + (subColumns - 1) * SUB_COL_GAP;
      if (index < columns.length - 1) width += GAP_X;
      const deep = Math.min(column.length, rows);
      height = Math.max(height, deep * CARD_H + Math.max(0, deep - 1) * SUB_ROW_GAP);
    });
    return { width, height: height + MARGIN * 2, rows };
  };

  let best = shapeFor(tallestRank);
  let bestScore = Infinity;
  for (let rows = 3; rows <= tallestRank; rows += 1) {
    const shape = shapeFor(rows);
    const score = Math.abs(Math.log(shape.width / shape.height / TARGET_ASPECT));
    if (score < bestScore) {
      bestScore = score;
      best = shape;
    }
  }
  const rowsPerColumn = best.rows;
  const width = best.width;
  const height = best.height;

  const at = new Map();
  let cursor = MARGIN;
  columns.forEach((column) => {
    const subColumns = Math.max(1, Math.ceil(column.length / rowsPerColumn));
    const spread = subColumns * CARD_W + (subColumns - 1) * SUB_COL_GAP;
    column.forEach((node, position) => {
      const sub = Math.floor(position / rowsPerColumn);
      const within = position % rowsPerColumn;
      const deep = Math.min(column.length - sub * rowsPerColumn, rowsPerColumn);
      const tall = deep * CARD_H + Math.max(0, deep - 1) * SUB_ROW_GAP;
      at.set(node.id, {
        x: cursor + sub * (CARD_W + SUB_COL_GAP),
        y: (height - tall) / 2 + within * (CARD_H + SUB_ROW_GAP),
      });
    });
    cursor += spread + GAP_X;
  });

  // ------------------------------------------------------------- rendering

  canvas.replaceChildren();
  canvas.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const defs = el("defs", {}, canvas);

  // The dotted ground. A pattern is one element however far you pan, where a
  // grid of real dots would be thousands.
  const dots = el(
    "pattern",
    { id: "code-dots", width: 26, height: 26, patternUnits: "userSpaceOnUse" },
    defs
  );
  el("circle", { cx: 1.1, cy: 1.1, r: 1.1, class: "canvas-dot" }, dots);

  [
    ["code-arrow", "arrow-line"],
    ["code-arrow-on", "arrow-line on"],
  ].forEach(([id, className]) => {
    const marker = el(
      "marker",
      {
        id,
        viewBox: "0 0 10 10",
        refX: "9",
        refY: "5",
        markerWidth: "14",
        markerHeight: "14",
        orient: "auto-start-reverse",
        markerUnits: "userSpaceOnUse",
      },
      defs
    );
    el("path", { d: "M 0 1 L 9 5 L 0 9 z", class: className }, marker);
  });

  // An edge is a gradient between the two roles it joins, with the midpoint
  // pulled most of the way to the border tone. A fully saturated middle turns a
  // dense graph into stripes and stops the cards reading as the subject.
  const toRgb = (hex) => {
    const value = parseInt(hex.slice(1), 16);
    return [(value >> 16) & 255, (value >> 8) & 255, value & 255];
  };
  const mixHex = (a, b, t) => {
    const left = toRgb(a);
    const right = toRgb(b);
    const channel = (index) => Math.round(left[index] + (right[index] - left[index]) * t);
    return `#${[0, 1, 2].map((i) => channel(i).toString(16).padStart(2, "0")).join("")}`;
  };
  const gradients = new Map();
  const gradientFor = (from, to) => {
    const key = `${from}|${to}`;
    if (gradients.has(key)) return gradients.get(key);
    const id = `edge-${gradients.size}`;
    const gradient = el(
      "linearGradient",
      { id, gradientUnits: "objectBoundingBox", x1: "0", y1: "0", x2: "1", y2: "0" },
      defs
    );
    const middle = mixHex(mixHex(from, to, 0.5), BORDER_TONE, 0.55);
    el("stop", { offset: "0", "stop-color": from, "stop-opacity": "0.85" }, gradient);
    el("stop", { offset: "0.5", "stop-color": middle }, gradient);
    el("stop", { offset: "1", "stop-color": to, "stop-opacity": "0.85" }, gradient);
    gradients.set(key, id);
    return id;
  };

  el("rect", { x: -20000, y: -20000, width: 40000, height: 40000, fill: "url(#code-dots)" }, canvas);

  const scene = el("g", { class: "scene" }, canvas);
  const edgeLayer = el("g", { class: "edges" }, scene);
  const labelLayer = el("g", { class: "edge-labels" }, scene);
  const nodeLayer = el("g", { class: "nodes" }, scene);

  const drawn = new Map();
  const labels = new Map();
  edges.forEach((edge) => {
    const from = at.get(edge.source);
    const to = at.get(edge.target);
    const x1 = from.x + CARD_W;
    const y1 = from.y + CARD_H / 2;
    const x2 = to.x;
    const y2 = to.y + CARD_H / 2;
    const bend = Math.max(36, Math.abs(x2 - x1) / 2);
    drawn.set(
      edge,
      el(
        "path",
        {
          d: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
          class: `code-edge${edge.confidence === "inferred" ? " inferred" : ""}`,
          ...(edge.kind === "api"
            ? {}
            : {
                stroke: `url(#${gradientFor(
                  ROLE[roles.get(edge.source)],
                  ROLE[roles.get(edge.target)]
                )})`,
              }),
          "marker-end": "url(#code-arrow)",
          fill: "none",
        },
        edgeLayer
      )
    );

    if (edge.label && x2 - x1 > 110) {
      const group = el("g", { class: "edge-label" }, labelLayer);
      const plate = el("rect", { rx: 4, class: "edge-label-plate" }, group);
      const text = el(
        "text",
        { x: (x1 + x2) / 2, y: (y1 + y2) / 2 + 3, "text-anchor": "middle" },
        group
      );
      text.textContent = edge.label;
      // Size the plate from the rendered text so it never crops. getBBox is
      // geometry, not style, so nothing here touches the CSSOM.
      const box = text.getBBox();
      plate.setAttribute("x", box.x - 6);
      plate.setAttribute("y", box.y - 3);
      plate.setAttribute("width", box.width + 12);
      plate.setAttribute("height", box.height + 6);
      labels.set(edge, group);
    }
  });

  const cards = new Map();
  nodes.forEach((node) => {
    const point = at.get(node.id);
    const role = roles.get(node.id);
    const link = el(
      "a",
      { href: node.url, class: `code-card role-${role}`, "data-id": node.id },
      nodeLayer
    );
    el(
      "rect",
      {
        x: point.x,
        y: point.y,
        width: CARD_W,
        height: CARD_H,
        rx: 10,
        class: `card-body${node.parse_ok ? "" : " parse-error"}`,
      },
      link
    );
    // The role stripe down the left edge, which is what carries the colour.
    el(
      "rect",
      { x: point.x, y: point.y + 2, width: 3, height: CARD_H - 4, rx: 1.5, fill: ROLE[role] },
      link
    );

    el(
      "rect",
      { x: point.x + 13, y: point.y + 11, width: 34, height: 16, rx: 4, class: "card-chip" },
      link
    );
    const badge = el(
      "text",
      { x: point.x + 30, y: point.y + 22.5, "text-anchor": "middle", class: "card-badge" },
      link
    );
    badge.textContent = node.badge || "";

    const name = el("text", { x: point.x + 55, y: point.y + 24, class: "card-name" }, link);
    name.textContent = node.label;

    if (node.folder) {
      const folder = el("text", { x: point.x + 13, y: point.y + 41, class: "card-folder" }, link);
      folder.textContent = node.folder.length > 30 ? `…${node.folder.slice(-29)}` : node.folder;
    }

    const metrics = el("text", { x: point.x + 13, y: point.y + 57, class: "card-metrics" }, link);
    const fn = el("tspan", { class: "metric-fn" }, metrics);
    fn.textContent = `${node.functions} fn`;
    const cls = el("tspan", { class: "metric-cls", dx: "10" }, metrics);
    cls.textContent = `${node.classes} cls`;

    const loc = el(
      "text",
      { x: point.x + CARD_W - 13, y: point.y + 57, "text-anchor": "end", class: "card-loc" },
      link
    );
    loc.textContent = `${node.lines}L`;

    const title = el("title", {}, link);
    title.textContent = `${node.path} · ${ROLE_LABEL[role]}${node.parse_ok ? "" : " · parse issue"}`;
    cards.set(node.id, link);
  });

  // ------------------------------------------------------- pan, zoom, fit

  let port = null;
  const view = { x: 0, y: 0, w: width, h: height };
  const apply = () => {
    canvas.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    // Edge labels are only legible close up, and at fit zoom a few dozen of
    // them cover the cards they describe. Hide them until the scale earns them.
    const scale = (canvas.clientWidth || width) / view.w;
    labelLayer.classList.toggle("too-small", scale < LABEL_MIN_ZOOM);
    if (port) {
      port.setAttribute("x", view.x);
      port.setAttribute("y", view.y);
      port.setAttribute("width", view.w);
      port.setAttribute("height", view.h);
    }
  };
  const fit = () => {
    Object.assign(view, { x: 0, y: 0, w: width, h: height });
    apply();
  };
  const zoom = (factor, originX, originY) => {
    const next = Math.min(Math.max(view.w * factor, width / 12), width * 2.5);
    const scale = next / view.w;
    view.x = originX - (originX - view.x) * scale;
    view.y = originY - (originY - view.y) * scale;
    view.w = next;
    view.h *= scale;
    apply();
  };
  const pointAt = (event) => {
    const box = canvas.getBoundingClientRect();
    return {
      x: view.x + ((event.clientX - box.left) / box.width) * view.w,
      y: view.y + ((event.clientY - box.top) / box.height) * view.h,
    };
  };

  canvas.addEventListener(
    "wheel",
    (event) => {
      event.preventDefault();
      const origin = pointAt(event);
      zoom(event.deltaY > 0 ? 1.12 : 0.89, origin.x, origin.y);
    },
    { passive: false }
  );

  let dragging = null;
  canvas.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".code-card")) return;
    dragging = pointAt(event);
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("grabbing");
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!dragging) return;
    const now = pointAt(event);
    view.x -= now.x - dragging.x;
    view.y -= now.y - dragging.y;
    apply();
  });
  const endDrag = () => {
    dragging = null;
    canvas.classList.remove("grabbing");
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  document.querySelectorAll("[data-graph-action]").forEach((button) => {
    button.addEventListener("click", () => {
      const action = button.getAttribute("data-graph-action");
      const midX = view.x + view.w / 2;
      const midY = view.y + view.h / 2;
      if (action === "fit") fit();
      if (action === "in") zoom(0.8, midX, midY);
      if (action === "out") zoom(1.25, midX, midY);
    });
  });

  // ------------------------------------------------------- canvas theme
  // Switching re-tints the stripes and the gradients in place. It must not
  // rebuild the scene: a relayout would throw away the pan, the zoom and the
  // selection, which is a lot to lose for a colour change.

  const frame = canvas.closest(".graph-canvas-frame");
  const themeButton = document.querySelector("[data-graph-theme]");
  let theme = "dark";

  const retint = () => {
    const palette = ROLES_BY_THEME[theme];
    cards.forEach((card, id) => {
      const stripe = card.querySelectorAll("rect")[1];
      if (stripe) stripe.setAttribute("fill", palette[roles.get(id)] || palette.orphan);
    });
    gradients.forEach((id, key) => {
      const [from, to] = key.split("|");
      const gradient = document.getElementById(id);
      if (!gradient) return;
      const stops = gradient.querySelectorAll("stop");
      const middle = mixHex(mixHex(from, to, 0.5), palette.border, 0.55);
      if (stops[0]) stops[0].setAttribute("stop-color", from);
      if (stops[1]) stops[1].setAttribute("stop-color", middle);
      if (stops[2]) stops[2].setAttribute("stop-color", to);
    });
  };

  if (themeButton && frame) {
    themeButton.addEventListener("click", () => {
      theme = theme === "dark" ? "light" : "dark";
      frame.setAttribute("data-canvas", theme);
      themeButton.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
      const next = theme === "dark" ? "light" : "dark";
      themeButton.setAttribute("aria-label", `Switch the canvas to ${next}`);
      themeButton.setAttribute("title", `Switch the canvas to ${next}`);
      // Role colours are per theme; the gradient key holds the dark pair, so
      // re-tinting reads the current palette rather than rebuilding the scene.
      try {
        localStorage.setItem("digital-brain.code-graph.canvas", theme);
      } catch (failure) {
        /* a private window refusing storage is not a reason to fail the click */
      }
      retint();
    });
    try {
      if (localStorage.getItem("digital-brain.code-graph.canvas") === "light") {
        themeButton.click();
      }
    } catch (failure) {
      /* no stored preference is the normal case */
    }
  }

  // ----------------------------------------------------------- minimap

  const minimap = document.getElementById("code-graph-minimap");
  if (minimap) {
    minimap.setAttribute("viewBox", `0 0 ${width} ${height}`);
    minimap.replaceChildren();
    nodes.forEach((node) => {
      const point = at.get(node.id);
      el(
        "rect",
        {
          x: point.x,
          y: point.y,
          width: CARD_W,
          height: CARD_H,
          rx: 6,
          class: `mini-node lang-${node.language || "other"}`,
        },
        minimap
      );
    });
    port = el("rect", { class: "mini-port", rx: 6 }, minimap);
    minimap.addEventListener("click", (event) => {
      const box = minimap.getBoundingClientRect();
      view.x = ((event.clientX - box.left) / box.width) * width - view.w / 2;
      view.y = ((event.clientY - box.top) / box.height) * height - view.h / 2;
      apply();
    });
  }
  fit();

  // The label gate is computed from the canvas's pixel width, so it goes stale
  // whenever that width changes — a window resize, or the inspector opening and
  // taking a third of the row. Re-apply instead of waiting for the next zoom.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(() => apply()).observe(canvas);
  } else {
    window.addEventListener("resize", apply);
  }

  // --------------------------------------------------------- selection

  const panel = document.getElementById("code-inspector");
  const body = document.getElementById("code-inspector-body");
  const closer = document.getElementById("code-inspector-close");

  const clearSelection = () => {
    canvas.classList.remove("has-selection");
    cards.forEach((card) => card.classList.remove("selected", "related"));
    edges.forEach((edge) => {
      drawn.get(edge).classList.remove("on");
      drawn.get(edge).setAttribute("marker-end", "url(#code-arrow)");
      const label = labels.get(edge);
      if (label) label.classList.remove("on");
    });
  };

  const select = (id) => {
    clearSelection();
    canvas.classList.add("has-selection");
    const related = new Set([id]);
    edges.forEach((edge) => {
      if (edge.source === id) related.add(edge.target);
      if (edge.target === id) related.add(edge.source);
    });
    cards.forEach((card, cardId) => {
      if (cardId === id) card.classList.add("selected");
      else if (related.has(cardId)) card.classList.add("related");
    });
    edges.forEach((edge) => {
      if (edge.source !== id && edge.target !== id) return;
      drawn.get(edge).classList.add("on");
      drawn.get(edge).setAttribute("marker-end", "url(#code-arrow-on)");
      const label = labels.get(edge);
      if (label) label.classList.add("on");
    });
  };

  // The card is a real link to a page that stands on its own. With scripting off
  // it simply navigates; here that same page is lifted into the panel, exactly
  // as modal.js does for dialogs. No view grows a "fragment mode".
  const open = (node) => {
    if (!panel || !body) return;
    panel.hidden = false;
    const loading = document.createElement("p");
    loading.className = "muted";
    loading.textContent = "Loading…";
    body.replaceChildren(loading);
    fetch(node.url, { credentials: "same-origin" })
      .then((response) => (response.ok ? response.text() : Promise.reject(response.status)))
      .then((html) => {
        const main = new DOMParser().parseFromString(html, "text/html").querySelector("main");
        if (!main) throw new Error("no main");
        body.replaceChildren(...main.childNodes);
      })
      .catch(() => {
        const failed = document.createElement("p");
        failed.appendChild(document.createTextNode("This panel could not load. "));
        const link = document.createElement("a");
        link.href = node.url;
        link.textContent = "Open the file page";
        failed.appendChild(link);
        body.replaceChildren(failed);
      });
  };

  cards.forEach((card, id) => {
    card.addEventListener("click", (event) => {
      event.preventDefault();
      select(id);
      open(byId.get(id));
    });
  });

  // ------------------------------------------------------- hover tracing

  const hoverClear = () => {
    canvas.classList.remove("has-hover");
    cards.forEach((card) => card.classList.remove("hover", "hover-related"));
    edges.forEach((edge) => {
      drawn.get(edge).classList.remove("hover-on");
      const label = labels.get(edge);
      if (label) label.classList.remove("hover-on");
    });
  };

  const hoverTrace = (id) => {
    hoverClear();
    canvas.classList.add("has-hover");
    const near = new Set([id]);
    edges.forEach((edge) => {
      if (edge.source === id) near.add(edge.target);
      if (edge.target === id) near.add(edge.source);
    });
    cards.forEach((card, cardId) => {
      if (cardId === id) card.classList.add("hover");
      else if (near.has(cardId)) card.classList.add("hover-related");
    });
    edges.forEach((edge) => {
      if (edge.source !== id && edge.target !== id) return;
      drawn.get(edge).classList.add("hover-on");
      const label = labels.get(edge);
      if (label) label.classList.add("hover-on");
    });
  };

  cards.forEach((card, id) => {
    card.addEventListener("pointerenter", () => hoverTrace(id));
    card.addEventListener("pointerleave", hoverClear);
  });

  // ---------------------------------------------------------- live search
  // Dimming instead of redrawing: a filter that removes nodes takes the shape
  // you were reading with it. The form still submits, because only the server
  // can look inside file contents or past the node cap.

  const search = document.getElementById("code-search");
  const status = document.getElementById("code-search-status");
  const applySearch = (raw) => {
    const query = raw.trim().toLowerCase();
    if (!query) {
      canvas.classList.remove("searching");
      cards.forEach((card) => card.classList.remove("no-match", "match"));
      edges.forEach((edge) => {
        drawn.get(edge).classList.remove("no-match");
        const label = labels.get(edge);
        if (label) label.classList.remove("no-match");
      });
      if (status) status.hidden = true;
      return;
    }
    const matched = new Set();
    nodes.forEach((node) => {
      if ((node.path || "").toLowerCase().includes(query)) matched.add(node.id);
    });
    canvas.classList.add("searching");
    cards.forEach((card, id) => {
      card.classList.toggle("match", matched.has(id));
      card.classList.toggle("no-match", !matched.has(id));
    });
    edges.forEach((edge) => {
      const on = matched.has(edge.source) && matched.has(edge.target);
      drawn.get(edge).classList.toggle("no-match", !on);
      const label = labels.get(edge);
      if (label) label.classList.toggle("no-match", !on);
    });
    if (status) {
      status.hidden = false;
      const count = matched.size;
      status.textContent = count
        ? `${count} file${count === 1 ? "" : "s"} match \u201c${raw.trim()}\u201d \u2014 others dimmed`
        : `No drawn file matches \u201c${raw.trim()}\u201d. Press Search to look inside file contents.`;
    }
  };

  if (search) {
    // Only on typing: a page arriving with ?q= was already filtered by the
    // server, which also matches file contents, so re-filtering by path here
    // would dim files that legitimately matched.
    search.addEventListener("input", (event) => applySearch(event.target.value));
  }

  const dismiss = () => {
    if (!panel || panel.hidden) return;
    panel.hidden = true;
    clearSelection();
  };
  if (closer) closer.addEventListener("click", dismiss);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") dismiss();
  });
})();
