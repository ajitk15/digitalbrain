# Code Graph — gap analysis against RECREATE.md

Status: analysis and plan. No code written for the gaps below.

RECREATE.md specifies **CodeGrapher**: a local, single-user FastAPI + React tool pointed at
one directory on disk. This document compares it capability by capability with what Code
Graph does today, and plans the gaps worth closing.

The two are not the same product, so a flat "missing feature" list would mislead. Section 3
records what we have that it does not, because several of those are why some of its features
cannot be copied as written.

## 1. Scorecard

`full` = comparable. `partial` = exists but narrower. `none` = absent.

### Analysis

| Capability | RECREATE | Ours | State |
|---|---|---|---|
| Python parsing | `ast` | `ast` | full |
| JS / JSX / TS / TSX | tree-sitter | regex | **partial** |
| Java, C, C++ | tree-sitter | — | **none** |
| Top-level functions and classes | yes | yes, typed since `structural-v2` | full |
| Imports with bound names | yes | yes | full |
| Nearest-ancestor resolution, no basename fallback | §8.4 | same rule, pinned by test | full |
| Package fallback only when no submodule matched | §8.4 | same | full |
| HTTP route declarations | yes | — | **none** |
| Outbound HTTP calls | yes | — | **none** |
| API seam edges (call → route) | yes | — | **none** |
| Cycle detection (Tarjan, iterative) | backend, stored | browser only, over drawn nodes | **partial** |
| Orphan detection | backend, stored | browser only, over drawn nodes | **partial** |
| Complexity rating | Low/Medium/High | — | **none** |
| `last_modified` per file | `st_mtime` | — | **none** |
| Per-file LOC | yes | yes | full |

### Graph and interaction

| Capability | RECREATE | Ours | State |
|---|---|---|---|
| Node cards: chip, name, metrics | yes | yes | full |
| Role colouring (entry/module/leaf/orphan/circular) | yes | yes | full |
| Gradient edges with pulled midpoint | yes | yes | full |
| Edge labels naming imported symbols | yes | yes | full |
| Label hidden below a zoom threshold | `LABEL_MIN_ZOOM 0.75` | same | full |
| Dotted canvas, minimap, legend, status bar | yes | yes | full |
| Pan / zoom / fit | React Flow | viewBox rewrites | full |
| Layered layout | dagre | own longest-path + barycentre | partial |
| `compactRanks` aspect packing | §8.6 | — | **none** |
| Position memory across refresh | §8.5 | — | **none** |
| Node dragging | yes | — | **none** |
| Search dims, keeps graph shape | yes | filters server-side, redraws | **partial** |
| Hover tracing | yes | — | **none** |
| Transitive impact with hop rings | yes | 1 hop only | **partial** |
| Click focus camera | yes | — | none |
| Light + dark canvas themes | both, toggled | dark only | **partial** |
| PNG export of the canvas | yes | — | none |

### Panels

| Capability | RECREATE | Ours | State |
|---|---|---|---|
| Inspector on selection | yes | yes, lifts the real file page | full |
| Inspector tabs | Overview / Deps / Used by / Impact / Chat | one scrolling page | **partial** |
| File tree with folders | yes | flat list | **partial** |
| Codebase chat | yes | deliberately not | n/a |
| Per-file chat | yes | deliberately not | n/a |
| Word document export | yes | deliberately not | n/a |
| Model settings panel | yes | deliberately not | n/a |

## 2. Requirements from §8 we already satisfy

Worth recording so they are not "fixed" twice.

- **§8.2 node id collisions** — does not arise. Ids are `CodeFile` UUIDs, not flattened paths.
  The failure it describes (`api/routes.py` and `api_routes.py` colliding) is impossible here.
- **§8.3 iterative Tarjan** — `static/code_graph.js` uses an explicit work stack for exactly
  the stated reason. Self-imports are tracked separately from the component map.
- **§8.4 resolver discipline** — `_resolve_python` tries every `module.name` candidate before
  falling back to the bare module, and never matches on basename.
  `test_analysis_resolves_project_imports_without_basename_guessing` pins it.
- **§8.14 no CSS variables in SVG attributes** — forced on us anyway: `style-src 'self'`
  means the renderer writes no inline styles at all, and role colours reach SVG as literal
  hex from one table in the script.
- **§8.16 path containment** — does not arise. Nothing takes a user-supplied filesystem path;
  `code_graph_clone.py` clones into a `TemporaryDirectory` it owns.
- **§8.17 no tools rather than instructions** — the same principle already governs
  `agent_runtime/tools.py`, which binds scope before a run rather than asking for it.
- **§8.18 CORS** — not applicable. One Django origin, session or bearer, CSP `default-src 'none'`.

## 3. What we have that RECREATE does not

These are why several of its designs cannot be lifted verbatim.

- **Multi-tenant, not single-user.** Every read is re-checked per request; a repository belongs
  to one application. RECREATE has no authorisation model at all.
- **Many repositories per application.** RECREATE analyses one directory. Our node ids,
  snapshots and queries are all repository-scoped.
- **Immutable snapshots pinned to a commit.** RECREATE re-reads the working tree; there is no
  history and no "which version was this". Our citations stay answerable later.
- **Code Factory integration** — a pinned snapshot feeds analysis.
- **No execution, ever.** We clone with hooks disabled and never run project code.

## 4. Gaps worth closing

Ranked by how much each changes what the screen is worth.

### Tier 1 — the graph is currently misleading or forgetful

**G1. Cycle and orphan counts are computed in the browser, over drawn nodes only.**
The status bar reads "0 circular groups · 6 with no links". Those are computed from the ≤200
nodes and ≤600 edges the page happened to send. On a repository larger than the cap the
numbers are not "approximate", they are wrong, and they are stated with the same confidence
as the file count. This is the same class of defect as an empty snapshot reporting `ready`.

Fix: compute Tarjan and orphan status **server-side over the whole snapshot** at index time,
store per-file role and a `cycles` count on `CodeSnapshot`, and render those. The browser
keeps its own pass only for colouring what is drawn. RECREATE §8.3 is explicit that
`circular_dep_count` is `len(cycles)`, not a file count — ours should store both.

**G2. A refresh reshuffles the whole graph.**
We recompute layout from scratch on every page load, so re-indexing a repository moves every
card even when one file changed. RECREATE §8.5 calls this out precisely: the camera holds
still while the content swims underneath. It keeps previous positions per node id and parks
genuinely new nodes in a grid below the settled ones.

Ours is harder — we have no client-side persistence of layout and a page reload is the normal
path. The honest version: derive positions deterministically from stable inputs (path and
role) so the same snapshot always lays out identically, and only reflow when the node set
actually changes. Store nothing; make it reproducible instead.

**G3. Impact is one hop.** The inspector shows direct dependencies and direct dependents.
"What else could this change break" is a transitive question, and it is the single thing
Code Factory would most use. RECREATE walks rings with `RING_OPACITY [1, 1, 0.82, 0.66, 0.52]`
and badges each node with its hop count.

Its caveat text is worth taking almost verbatim, because it is the honest framing:
*reachability is measured between files, not functions — a file that imports one untouched
function still appears here.*

**G4. Search filters instead of dimming.** Typing a query re-queries the server and redraws a
smaller graph, so the shape you were reading disappears. RECREATE keeps every node mounted and
dims non-matches, with a pill reading `{n} files match “{q}” — others dimmed`. That preserves
the map. Ours should dim client-side for matches within the drawn set, and keep the
server-side filter only for narrowing past the 200-node cap.

### Tier 2 — parity that changes daily use

**G5. `compactRanks`.** Our layout gives every node in a layer the same x and stacks it, which
is exactly the "narrow vertical ribbon" RECREATE §8.6 describes. A broom-shaped repository —
many entry points into a few shared modules — produces one enormous column. Their fix keeps
dagre's rank assignment and within-rank ordering, then restacks each rank into side-by-side
sub-columns, choosing rows per project by minimising `|log((w/h) / 1.6)|`. Ours can do the same
over our own ranks.

**G6. Hover tracing.** Highlight a node's edges on hover without committing to a selection.
Cheap, and it is most of what makes a dense graph explorable.

**G7. Folder tree.** The left rail is a flat 200-row list. A collapsible tree with language and
parse-status indicators was already stage 5 of the explorer plan.

**G8. Light canvas.** We render dark always. RECREATE ships both and inverts the ladder rather
than lightening it — *"a mid-tone that reads as bright on near-black reads as washed out on a
light ground"*, with amber `#eab308` → `#b45309` as the example. A toggle is a `data-theme`
attribute on the frame plus a second palette; the renderer already takes colours from one table.

**G9. Inspector tabs.** One scrolling page today. `Overview / Deps (n) / Used by (n) / Impact`
is better once G3 lands. Tabs must stay server-rendered in `code_file.html` so the standalone
page keeps working — the panel lifts that page, and no view grows a fragment mode.

### Tier 3 — analysis breadth

**G10. Java, C, C++ — and better JS/TS.** Ours parses JavaScript with two regexes. They find
`function`/`class`/`interface`/`type`/`enum` declarations and `import`/`require` specifiers,
and miss `const x = () => {}`, re-exports, and dynamic imports. Tree-sitter is the stated
answer, but it is a compiled dependency: `platform_core` currently has no native extension, and
adding one changes the deployment story on Windows. Prototype before committing, per the
integration plan §8.

**G11. API seam edges.** Routes and outbound calls, matched on normalised path, marked
`inferred`. This is the largest single item and already has a home in
`design/code-graph-integration-plan.md` §6. Django `urls.py` with `include()` prefix
composition matters more for our own codebase than the FastAPI/Flask patterns RECREATE
emphasises.

**G12. Complexity, G13. `last_modified`.** Deliberately deferred, not merely absent. We do not
compute complexity, and inventing a Low/Medium/High for it would repeat the error of calling an
empty snapshot `ready`. `last_modified` needs `git log` per file, which a `--depth 1` clone
cannot answer — it would need a second, deeper fetch.

## 5. Not closing

Chat rails, per-file chat, the model-settings panel, and Word export stay out. Each duplicates
something this platform already does with discipline RECREATE does not have — verified
citations, per-application credentials, usage receipts — and `CLAUDE.md` already records the
reasoning. PNG export is plausible later; its §8.10 lesson (render undimmed via a flag rather
than clearing selection state) should be read before anyone tries.

## 6. Suggested order

| Stage | Contents | Exit criterion |
|---|---|---|
| A | G1 server-side cycles and orphans, stored on the snapshot | Counts are right on a repository past the 200-node cap; `circular_dep_count` is cycles, not files |
| B | G4 dim-on-search, G6 hover tracing | Searching keeps the layout still; a hover reads without a click |
| C | G2 deterministic layout, G5 compactRanks | The same snapshot lays out identically twice; a broom-shaped repo is not one column |
| D | G3 transitive impact + G9 inspector tabs | Hop counts shown with the reachability caveat; the file page still stands alone |
| E | G7 folder tree, G8 light canvas | Tree collapses; both themes legible |
| F | G10 parser breadth, then G11 API seams | Coverage matrix published before either is claimed |

Stage A first because it is the only one where the screen currently states something untrue.
