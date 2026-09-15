# Code Graph explorer — bringing the CodeGrapher layout into Digital Brain

Status: plan. No code written yet.

The reference is a screen recording of **CodeGrapher**, a separate local tool. This
plan takes its *information design* — what a node tells you at a glance, what an edge
tells you, where the detail goes — and rebuilds it inside this application's shell,
theme and security rules. It is not a port. Nothing from that tool is copied.

## 1. What the recording actually shows

Read off the frames rather than from memory:

| Element | Detail |
|---|---|
| Node | Rounded card, not a dot. Language chip (`JS` amber, `JSX` cyan, `PY`), filename in monospace, then a metrics row: `1 fn` · `0 cls` · right-aligned `279L` |
| Selection | Selected card takes a violet border and glow; its edges light up, the rest recede |
| Edge | Curved/orthogonal, arrowhead at the target, coloured by kind — blue for imports, amber for a second class, violet along the selected path |
| Edge label | A small pill sitting on the edge naming what crossed it: `useResizable`, `DIRECTION_COPY, computeImpact`, and route strings like `POST /api/…` |
| Left rail | File tree — `backend / languages / __init__.py …` — each row with a number on the right |
| Left rail (alt) | Same rail switches to a codebase chat, and to a model-settings panel |
| Right panel | Opens on selection: filename, path, tabs (Info / Used by (2) / Imports / Classes), metric tiles `LOC 279`, `Complexity Low`, `Functions 1`, `Classes 0`, then last-changed and a function list |
| Canvas chrome | `LEGEND` button top-left, minimap bottom-right, zoom control left edge |
| Status bar | Counts along the bottom: files, imports, and similar |

## 2. Adopt, adapt, reject

**Adopt** — node cards with language chip, symbol counts and line count; edge labels
naming the imported symbol; selection that highlights its neighbourhood and dims the
rest; a right-hand inspector; a minimap; a legend; a bottom count bar.

**Adapt** — the recording is dark-themed and full-bleed. Ours lives inside the
existing shell (sidebar, application menu, breadcrumbs) and must use `tokens.css`
variables so it matches Knowledge, Chat and Code Factory. No new palette.

**Reject, deliberately** —

- *The model-settings panel.* This application already configures AI per application,
  and `design/code-graph-integration-plan.md` §8 rules out "independent provider
  settings". A second place to paste an API key would also break the file-mounted
  secret invariant.
- *The codebase chat rail.* Chat already exists as its own screen with citation
  verification and usage receipts. A second chat with its own context assembly would
  duplicate that and bypass the receipt discipline. Scoped code chat is §9 of the
  existing plan and stays there.
- *`Complexity: Low`.* We do not compute complexity. Inventing a word for it would be
  the same class of error as calling an empty snapshot "ready".
- *`Last changed Aug 14`.* We index one commit; we do not read per-file history. It
  would need a second git operation per file.

## 3. What the rules force

`platform_core/middleware.py` sets, and `test_security.py` pins exactly:

```
default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; …
```

Three consequences that decide the architecture:

1. **No external libraries.** React Flow, d3, dagre and cytoscape all load from a CDN,
   and no external origin is permitted. Layout and rendering are vanilla, self-hosted,
   and there is no build step to add one (`CLAUDE.md`: "server-rendered, no JavaScript
   build step").
2. **No inline styles from script.** `style-src 'self'` is why `modal.js` leans on
   native `<dialog>` instead of writing `style.top`. **SVG presentation attributes are
   not CSS** — `setAttribute("transform", …)`, `x`, `y`, and crucially `viewBox` are
   attributes. So pan and zoom are implemented by rewriting the root `viewBox`, and
   every colour comes from a CSS class in `redesign.css`. This is the pivot the whole
   design rests on, and the current `static/code_graph.js` already works this way.
3. **Progressive enhancement is a contract.** Every control must do something useful
   with scripting off. The file list, the search form and the repository selector are
   already real forms; the inspector must degrade to a normal page navigation.

## 4. Data the analyser does not yet produce

| Need | State | Work |
|---|---|---|
| `1 fn · 0 cls` | Python sets `kind` to `function`/`class`; JavaScript lumps everything as `"symbol"` | Capture the matched keyword in `JS_SYMBOL` so `function`/`class`/`interface`/`type`/`enum` are distinguished |
| `279L` | `CodeFile.lines` already stored | none |
| Language chip | `CodeFile.language` already stored | none |
| Edge label `useResizable` | Edges store only `evidence: {"lines": […]}` | Carry the imported names into evidence in `relationships()`; they are already parsed for Python `ImportFrom` and are the captured group for JS |
| Route labels `POST /api/…` | Not extracted at all | **Out of scope here.** Route extraction is §5 of the integration plan and is its own piece of work |

Only the first and fourth are code changes, both inside
`platform_core/code_graph_analysis.py`, and both are additive — a re-index fills them
in, and older snapshots keep rendering without them.

## 5. Rendering architecture

One file, `static/code_graph.js`, no modules, no build.

**Layout.** Layered left-to-right, computed in three passes:

1. *Layer assignment* — longest-path over the edge set, with cycle members pinned to
   the layer of their first-seen member so a cyclic group cannot loop forever.
2. *Ordering within a layer* — a few barycentre sweeps to cut crossings. Three passes
   is enough at our node cap and keeps the frame budget small.
3. *Coordinates* — fixed card size, fixed gutters, so a card never overlaps another.

Bezier edges leaving the right edge of a card and entering the left edge of the next,
with the label pill placed at the curve midpoint and drawn *after* all edges so it is
never painted over.

**Pan and zoom.** One `viewBox` on the root `<svg>`, rewritten on wheel and drag.
Zoom is clamped. Fit computes the bounding box of all cards plus a margin. No CSS
transforms anywhere, so nothing touches `style`.

**Selection.** Clicking a card sets a class on it and on its incident edges, and a
`dimmed` class on everything else. Pure class toggling — the palette stays in CSS.

**Minimap.** A second `<svg>` with the same cards at small scale and a viewport
rectangle whose `x`/`y`/`width`/`height` mirror the main `viewBox`. Clicking it
recentres.

**Node cap.** 200 today. The card layout needs more room than a dot, so the honest
move is to keep the cap, state it plainly, and let search narrow — which the
truncation notice now does correctly.

## 6. The inspector, without inventing a fragment endpoint

`templates/code_file.html` already exists and already renders path, symbols,
dependencies and dependents as a standalone page. `CLAUDE.md` is explicit that no view
gets a "fragment mode" and that a popup target "must always be a page that renders and
submits on its own".

So the inspector follows the `modal.js` pattern exactly, in a side panel instead of a
dialog: the card is a real `<a href>` to `code-file`; with scripting on, a click is
intercepted, that href is fetched, and its `<main>` is lifted into the right panel.
With scripting off the link simply navigates, which is what it does today.

That means the tabs and metric tiles from the recording are built **in
`code_file.html`**, server-side, where they are testable without a browser — and the
panel inherits them for free.

## 7. Delivery order

| Stage | Contents | Exit criterion |
|---|---|---|
| 1 | Node cards: language chip, `fn`/`cls`/`L` row, language colours, legend, count bar | `storycreator` is readable without hovering anything; light and dark agree with `tokens.css` |
| 2 | Pan, zoom, fit, minimap | Navigation survives a search and a refresh; no CSS transform anywhere; CSP test still green |
| 3 | Selection highlighting + inspector panel fed by `code_file.html` | With JavaScript off the same click still reaches a working page |
| 4 | Edge labels + JS symbol kinds | A re-index shows imported names on edges; old snapshots still render |
| 5 | Folder tree in the left rail, replacing the flat list | Folders collapse; parse-status and language visible per row |

Stages 1–3 are the ones that answer "it is not what I expected". Stage 4 needs a
re-index to show anything. Stage 5 is the largest and the least urgent.

## 8. Explicitly not in this plan

Route extraction and API-consumer edges, cross-repository edges, Knowledge-to-symbol
links, code chat, exports, complexity metrics, and per-file git history. Each is
already placed in `design/code-graph-integration-plan.md`; none of them is needed to
make the graph legible, which is the complaint this plan answers.
