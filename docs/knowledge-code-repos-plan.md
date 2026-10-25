# Code Repos under Knowledge: feasibility, risks and plan

Not implemented. This covers three things:

- whether Knowledge can take a **Code Repos** tab;
- whether Code Graph can be folded into that tab entirely;
- what that costs, what could go wrong, and what onboarding needs.

```
Today      Knowledge:  Documents · Tickets · Graph · Quality · Versions
           Code Graph: Graph · Reviews · Documentation        (its own menu item)

Proposed   Knowledge:  Documents · Code Repos · Tickets · Graph · Quality · Versions
           Code Repos → one repository: Structure · Reviews · Documentation
           (the Code Graph menu item is removed)
```

## Verdict

**Feasible, and low-to-medium complexity, if the change is kept to navigation.**
Code Graph can be folded into Knowledge → Code Repos with no new model, no
migration, no change to retrieval, graph building, the scorecard or row security.
The risk is in wording and in where links point, not in data. Merging code *into
the knowledge graph* is a different change with high risk (see Option C), and
this plan does not do it.

## What exists today (measured)

| Area | Size | Coupling |
|---|---|---|
| Views | `code_graph.py` 597 lines; `code_review.py` 1,112; `code_docs.py` 1,016 | Reviews and Docs pick a repository with `code_graph.selected_repository(?repository=)`, so they already read from one list |
| Ingest | `code_graph_ingest/clone/local/analysis/graphify.py`, about 1,900 lines | Not touched: storage and indexing stay the same |
| Templates | `code_graph.html`, `code_file.html`, `code_reviews.html`, `code_review_detail.html`, `code_docs.html`, `repository_add.html`, `repository_browse.html`, `_code_graph_tabs.html` | Every one starts with `application_nav` and draws `_code_graph_tabs.html` |
| Script | `static/code_graph.js` | Loaded only by `code_graph.html`, reading `#code-graph-data` and `#code-graph-canvas`. No collision with `graph.js` |
| Route names | `code-graph`, `code-file`, `code-reviews`, `code-review`, `code-docs`, `code-graph-add`, `code-graph-browse` | Linked from `run_detail.html`, `_before_after.html`, `_source_row.html`, `code_knowledge.py`, `readiness.py`, `workspace.py` |
| Feature key `code_graph` | 36 uses in 14 modules | `services.PURPOSES` (engineering), `gateway.PURPOSES` (code_review, code_docs), Code Factory gates, `source_library`, `graphs`, `readiness` |
| Tests | About 66 references to the route names in 9 files (`test_code_graph` 25, `test_code_review` 19, `test_code_graph_local` 9, `test_browser` 5, …) | Plus 3 assertions on the words "Code Graph" (`test_creation:462`, `test_code_factory_delivery:1028`, `test_analysis_view:96`) |
| Lane name `"code-graph"` | `document_worker`, `queue_status` | The name of a worker lane, not a URL. **Must not change** (heartbeats, `queue_status --lanes`, compose health checks) |
| Words outside code | `docs/index.html` (21 mentions), demo kit (16 mentions in 5 files), slides, video narration | These are the product's own words: Code Graph is a named thing in the demo |

The knowledge graph is built from `KnowledgeEntry` alone (`graphs.sources_for`).
Code reaches knowledge only through `code_knowledge.links`, which is worked out on
demand, gives references and never asserts anything. Code Factory requires
Knowledge (`code_factory.start_run` calls `access(..., "knowledge")`), so every
application that delivers code already has Knowledge on.

## Options

| | A. Code Repos tab, Code Graph menu kept | **B. Fold Code Graph into Code Repos** | C. Code joins the knowledge graph |
|---|---|---|---|
| What it means | Repositories are managed under Knowledge; Code Graph is a menu item for reading them | Code Repos lists repositories; opening one shows Structure / Reviews / Documentation; the Code Graph menu item goes | Code files become graph sources: chat cites code; Graph / Quality / Versions include it |
| Data or migration | None | None | New source kind, chunking of code, citations against `CodeFile`, benchmark questions about code |
| Invariants touched | None | None | Citation verification (code is not `KnowledgeEntry`); "derived text is never requirement evidence"; the scorecard baseline; the per-source graph limits |
| Effort | About 3 days | **About 4-5 days** | Weeks, and its own measured plan |
| Risk | Low | Low-medium | High |
| UX | Two places for code | **One place for code** | Hides which graph answered |

**Recommendation: B.** You've said you're happy to drop the Code Graph menu item, and
B is what gives one place for code. It costs roughly a day more than A. C stays out
of scope.

## Option B in detail

### Principle: move it in the navigation, not in the URL space

Keep every URL path, route name and the feature key `code_graph`. Only the
navigation, page headings and tab bars change. Then the 66 test references and
every link from Code Factory keep working, as do bookmarks, the `?repository=`
links inside run pages, and the `code-file` links `code_knowledge` builds. One route
is added: `code-repositories` (`applications/<pk>/knowledge/repositories/`).

### Screens

1. **Knowledge → Code Repos** (new view, `code-repositories`): one row per active
   repository. Columns: name with provider badge (GitHub / Server folder / Docs
   only), last snapshot and commit, branch drift, languages, documents imported
   from it. Actions: Open, Check branch, Refresh index, Retire. Each action is a
   real POST with the checks it has today. Add repository and Browse open as
   popups from here. Snapshots are prefetched, so the page does not run one query
   per row.
2. **One repository** (the existing `code-graph?repository=`, `code-reviews`,
   `code-docs`): the Knowledge tab bar on top with Code Repos current, then the
   repository's own tabs. **"Graph" is renamed to "Structure"** there, so the
   application doesn't have two tabs called Graph with different versioning
   (snapshots for code, revisions for knowledge). The repository `<select>` stays
   as the picker inside a repository. Without a `?repository=`, the existing
   fallback (the first repository) stays for old links. New links always name one.
3. **Menu** (`workspace.py:application_menu`): remove the Code Graph entry and add
   its route set to Knowledge's. The Knowledge entry shows when `knowledge` **or**
   `code_graph` is on. Its link is the first tab the application has, because
   today it always links to `graph`, which 404s with Knowledge off.
4. **Tabs** (`knowledge_nav.html`): each tab checks its own feature. Documents,
   Tickets, Graph, Quality and Versions need `knowledge`. Code Repos needs
   `code_graph`. `_code_graph_tabs.html` becomes the repository's sub-tabs.

### Access

- Code Repos and the repository pages keep `access(user, pk, "code_graph")`. With
  no grant you get a 404, as everywhere. Folding them under Knowledge must **not**
  switch them to the `knowledge` check, or an engineering application with
  Knowledge off would lose its repositories.
- An Operations-only application ("Runbooks") has `code_graph` off, so it gets no
  Code Repos tab. If its owner ticks Code Graph, `services.purposes` already reads
  the application as Engineering too, and the label goes back to "Knowledge".
- Nothing new is reachable. Every POST keeps its role check (owner or contributor)
  and audit event (`code_repository.retired` and the rest).

## Onboarding: what changes

What onboarding is made of, and what each part needs:

| Piece | File | Change |
|---|---|---|
| "Code indexed" step (Delivery gate) | `readiness.steps`, around line 448 | **Route `code-graph` → `code-repositories`.** Fix text "Register the repository on the Code Graph screen…" → "Add the repository under Knowledge › Code Repos…". With `code_graph` off it still routes to `application-features`, and its text ("Tick Code Graph on the Features screen") follows whatever the feature is called |
| "One repository to read" step | `readiness.steps`, around line 497 | **Route `code-graph` → `code-repositories`. This is the one that would break**: its fix is "Remove the repositories that are not this application's code", and Retire moves to Code Repos. Left on `code-graph`, the checklist would send people to a page with no Remove button |
| Button labels | `readiness.PLACES` | Add `"code-repositories": "Code Repos"` (the button reads "Open Code Repos"). Drop or keep `"code-graph"` (harmless if unused) |
| `Step.modal` comment | `readiness.Step` | Says Knowledge, Connectors and Code Graph are "places you go". Update the name. The step stays `modal=False`: after a repository is added, `modal.js` follows the redirect away from the checklist anyway (`landed.pathname !== target` → `location.assign`), and Code Repos is where the indexing progress shows |
| Connector question | `onboarding_connectors.html`, `connector_kinds` GitHub `summary` | No change needed: GitHub already reads "Issues from a repository". Optional extra line: "Code is added under Knowledge › Code Repos, not here", because the GitHub connector and a GitHub repository are easy to confuse |
| Create form | `services.PURPOSE_CHOICES` ("Code Graph and Code Factory"), `FEATURES["code_graph"]` label | Only if the name changes (decision below). The key and `PURPOSES` stay |
| Code Factory pre-checks | `code_factory.prevalidate` | No change: it narrates the **Analysis** steps only, and both repository steps are in Delivery |
| Setup strip, Attention list | `application_nav.html`, `attention.py` | No change: they show the step *label* ("Code indexed"), which stays. The strip's link goes to the checklist |
| Step numbering | `workbench.onboarding` | No change: same steps, same order. `test_readiness:286` ("7. Code indexed") still holds |

Tests for onboarding: in `test_readiness`, assert both repository steps route to
`code-repositories` with "Open Code Repos". Assert they route to
`application-features` with Code Graph off. Assert an Operations-only application
shows neither step. Keys (`test_readiness:188`) are unchanged.

## Other links to repoint

- `run_detail.html:60`, `code_factory_build.py` around 486 and 621-623, and the
  `test_code_factory_delivery:1028` assertion: "Index the repository in Code
  Graph" / "No repository is registered in Code Graph" should name Knowledge ›
  Code Repos.
- `code_docs.html` and `code_reviews.html` empty states ("Add one on the Graph
  tab"): change to Code Repos.
- `repository_add.html` and `repository_browse.html`: Cancel/Back go to Code
  Repos, and `repository_add` redirects there after a successful POST.
- `_source_row.html` and `_before_after.html`: their `code-graph?repository=`
  links keep working. Optionally point the document-row chip at Code Repos.
- `test_creation:462` asserts `>Code Graph</a>` is absent for Operations. That
  still passes but no longer tests anything once the item is gone, so rewrite it
  to check Code Repos is absent.

## Risks

| # | Risk | Likelihood / impact | Mitigation |
|---|---|---|---|
| 1 | People assume chat now knows the code, because it sits under Knowledge | High / medium | One line on Code Repos: Code Graph, Code review and Code Factory read these; chat and the knowledge graph do not |
| 2 | The onboarding "remove extra repositories" step points at a page with no Remove button | Certain if missed / medium | Repoint both repository steps (table above) and test the routes |
| 3 | Two tabs called "Graph" | Certain if not renamed / low | Rename the repository tab to "Structure" |
| 4 | Two rows of tabs (Knowledge, then the repository's) | Medium / low | Same depth as today (menu, then the Code Graph tab row). The list page has one row; the second row appears only inside a repository |
| 5 | Knowledge off, Code Graph on: no way to reach repositories | Low (Code Factory needs Knowledge) / medium | Menu shows Knowledge for either feature; tabs gate themselves; test it |
| 6 | The lane name `code-graph` gets renamed along with the screens | Low / high (heartbeats, health checks, `--lanes`) | Say in the change that the lane name is not a screen name; leave it |
| 7 | Product language: demo script, `docs/index.html`, slides and video say "Code Graph" | Certain / medium for demos | Keep "Code Graph" as the feature and explorer name, so most of it stays true. Update the demo kit's navigation steps in the same change |
| 8 | Browser tests and `data-modal` flows move (Add repository, Browse) | Certain / low | Update `test_browser` to open them from Code Repos (submit success, an error re-rendered in the dialog) |
| 9 | A static change is not picked up | Medium / low | If `redesign.css` changes: `collectstatic` and restart. Check no Code Factory run is in flight first, because a restart kills it |
| 10 | Conflicts with work in progress | Medium / low | `domain-ontology-packs` has 22 modified files, including `redesign.css` and Knowledge templates. Build this on its own branch after that lands |
| 11 | Old links without `?repository=` land on an arbitrary repository | Low / low | Existing fallback (the first repository); every new link names one |

## Complexity

| Step | What | Effort |
|---|---|---|
| 1 | Code Repos view and template, tab, menu, feature gating | 1.5 d |
| 2 | Move Check / Refresh / Retire / Add / Browse there; repository pages get the Knowledge bar and the Structure / Reviews / Documentation sub-tabs | 1 d |
| 3 | Onboarding steps, Code Factory messages, empty states, link repointing | 0.5 d |
| 4 | Docs-only rows with Index code; document counts both ways | 0.5 d |
| 5 | Tests (views, gating, readiness, browser), the five gates, CLAUDE.md, demo kit wording | 1-1.5 d |

Total about 4.5-5 days. Steps 1-3 ship together, or there would be two places to
manage repositories. Step 4 can follow. Expect the scorecard to read the same:
nothing it measures changes.

## Decisions to make before building

1. **The feature's name** on the Features screen and the create form: keep "Code
   Graph" (recommended: fewer words change and the demo stays true) or rename it
   "Code repositories".
2. **The Knowledge tab label "Graph" or "Graphs"**: keep "Graph" (recommended).
   Quality and Versions are about knowledge graph versions only, so a plural tab
   that also showed code would make those two tabs ambiguous.
3. **The repository tab name**: "Structure" (recommended), "Explorer" or "Code
   map".
