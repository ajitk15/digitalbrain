# Desktop UI/UX review and fix plan

Reviewed 2026-10-25 against the development database (CarePathDev, AI v1 check) in
headless Chrome at 1280x800, 1440x900 and 1920x1080, signed in as `acmeadmin`. Thirty
screens, plus two popups. Phone layouts were not judged: the aim is the best desktop
experience, and a narrow window only has to not break.

**Scope of the plan: presentation only.** Templates, CSS and template filters. No view,
model, URL or form-field changes, no change to what a POST does, and none of the
invariants in `CLAUDE.md` move: no inline styles (CSP), progressive enhancement and
`data-modal` targets stay real pages, JavaScript-bound ids and classes keep their names,
and `_graph_ai_plan.html` still shows the same answer on Knowledge, upload and Generate.
Items that would need more than presentation are listed separately at the end.

## Status: all three phases done (2026-10-25, not yet committed)

Checked at 1280x800, 1440x900 and 1920x1080, and with the five gates. Where it went
differently from the plan below:

- **#1 Versions.** The date stays `Oct 25, 2026 04:19` with the full time in a
  `<time>` title; numbers are right-aligned; the table no longer splits words. The
  pager shows only with more than one page.
- **#7 One row less.** The trail, the application name, the tabs and the focus button
  are one bar (`application_menu.html`); the setup strip stays in `application_nav.html`.
- **#8 Inspector.** Always beside the canvas at 1200 and wider (a 320px column), canvas
  `clamp(420px, 62vh, 820px)`. Tested by
  `test_on_a_desktop_the_inspector_sits_beside_the_canvas`.
- **#10 Tickets.** Graph usage is one pill and "+N" (`_source_versions.html` with
  `compact`, shared with Documents); chips stay under the title. Fits nine columns at
  1280.
- **#12 Settings navigation** was already sticky; nothing to do.
- **#14 Wayfinding, revised.** The breadcrumb now sits in the application bar, so instead
  of a second trail every page with a parent has one "Back to ..." link in one place,
  first under the bar (`.page-back`), replacing the different links at the top, foot or
  rail. Their wording is unchanged because tests rely on it.
- **#15 Headings.** The application name is no longer repeated as an eyebrow over a
  page heading (it is in the bar); popups keep theirs, since the bar is behind them.
  Chat keeps its own heading layout.
- **#16 AI plan.** "AI enrichment is on. The next graph version is built with X, charged
  to Y." on one line, the rest in a native `<details>`; "built without AI" stays open,
  because its reason is what an owner acts on.
- **#17 Popups.** `modal.js` adds a header × (a real button; with script off there is no
  dialog to close) and puts Cancel beside the primary button when the popup has one
  form; a popup with several forms (Add a source) keeps one Cancel at the foot. Tested
  by `test_the_close_in_a_popups_header_shuts_it`. Personal data's Save and Mask sit in
  one row, Mask still its own form and confirmation through the `form` attribute.
- **#18 Paging.** The pager already hid on one page; the "Showing" line now hides only
  when there is nothing to show, because tests pin the count on a single page.
- **#13 Titles.** Every page sets `<title>`: `<Page> · <Application>` inside an
  application, `<Page> · Digital Brain` outside one.

## What already works well on desktop

- **A real design system.** Tokens in `tokens.css` and the `legacy, redesign` cascade
  layers mean a fix is one low-specificity rule, never an `!important` fight.
- **Focus mode and a collapsible sidebar**, so a graph or chat can have the whole monitor.
- **Chat fills the window properly**: conversation list, messages and a composer pinned
  to the bottom, with no page scroll. This is the best-proportioned screen in the app.
- **Popups are native `<dialog>`s** at a sensible desktop width (about 620px), centred,
  with Escape and focus handling free, and they still work as plain pages.
- **Icon-only controls are labelled.** Every one checked (delete, resync, open origin,
  focus) has an `aria-label` and a `title` tooltip.
- **Settings have their own navigation** (Application / AI / Integrations / Access), and
  Access & roles uses the width well with Invite and Give access side by side.
- **Empty states say what to do next**, with the primary action in the empty state itself
  (ServiceOps, Code Factory, Runs).
- **No page scrolls sideways at 1280 or wider.** Tabular figures stop numbers jittering.

## Findings

Severity: **High** looks broken or hides something at a normal desktop width; **Medium**
wastes the desktop's width or height, or is inconsistent; **Low** is polish.

### Broken at normal desktop widths

| # | Sev | Where | What happens |
|---|-----|-------|--------------|
| 1 | High | Knowledge → Versions (`graph.html`) | At 1440 the model wraps mid-name (`claude-sonnet-5-` / `5`) and the benchmark figure splits as `16/1` / `6`. At 1280 the date, model, quality and the three actions all wrap, so each row is three lines. **Publish**, the main action on the page, is a small underlined link after "View graph · Export". |
| 2 | High | Knowledge → Quality → Quality by version (`_quality_trend.html`) | The table is wider than its card (1458px in 1085px at 1440, 925px at 1280), so the last columns are cut off behind a horizontal scroll. The chart above it is drawn at about half the card's width. |
| 3 | Medium | Document page → Document type (`_document_type.html`) | The label "Check it as" wraps onto two lines beside the select. |
| 4 | Medium | Knowledge → Graph toolbar (`graph.html`, `graph.js`) | At 1440 the search placeholder is cut ("Type a record, value o") and the hint "Related code appears with the node …" is truncated with an ellipsis, so the toolbar reads as broken. |
| 5 | Low | Features, Chat | An inline chip followed by punctuation leaves a gap: "set up for **Engineering**   . Switching…", "under Knowledge → Versions   ." |
| 6 | Low | Code Factory | The onboarding link's icon touches its text ("⊘Onboarding: …"). |

### Not using the width or height a desktop has

| # | Sev | Where | What happens |
|---|-----|-------|--------------|
| 7 | High | Every application page | Four rows of chrome before any content: top bar (56px), application menu (52px), breadcrumbs (60px), then section tabs. About 245px, which is 27% of a 900px-tall window and pushes the graph canvas below the fold even at 1920x1080. |
| 8 | High | Knowledge → Graph | Node details ("Source evidence") sit **below** the canvas. At 1920 the canvas itself starts halfway down, so clicking a node changes something off screen. Code Graph already shows its inspector as a right-hand column, so the two graph screens behave differently. |
| 9 | Medium | Document page (`document_detail.html`) | One long column at 1440. The file's facts and every action (View Markdown, Download, Download graph input, View graph, Convert again, Delete) are at the very bottom, 1,000px below the title. |
| 10 | Medium | Knowledge → Tickets (`tickets.html`) | Each row is about 110px tall because Graph usage stacks "v5 · Draft", "v4 · Draft" and "3 earlier versions", while the title column wraps to three lines with room to spare elsewhere. Seven tickets fill 800px. |
| 11 | Medium | Code Graph → Files | The file list grows the page to its full length (1,334px) beside a fixed-height canvas, and the "python · leaf" line under each file is not indented with its name. |
| 12 | Medium | Settings → Features | Eighteen single-line rows in one column, 1,700px tall, with **Save features** at the very bottom. |
| 13 | Medium | Settings (AI settings 2,400px, AI costs 2,300px) | The settings navigation scrolls away with the page. |
| 14 | Medium | Settings → AI costs (`usage.html`) | Figures are left-aligned and unformatted: `702168` tokens, `USD 0.00723800`, a total of `4.6599` with no currency sign, and task slugs (`graph_generation`) instead of the names AI settings uses. Rows are 50px, so a page of 30 is 1,500px. |
| 15 | Low | Knowledge → Quality | Four separate full-width notice strips in a row (call limits, one unreadable reply, one source not extracted, 74 excluded). They read as four alerts rather than one set of notes about this version. |
| 16 | Low | Overview at 1920 | Application cards are 510px wide holding a short list of links; most of the screen is empty. (Layout only; showing more would need data, see the end.) |

### Consistency and wayfinding

| # | Sev | Where | What happens |
|---|-----|-------|--------------|
| 17 | High | 47 templates | The browser tab says only "Digital Brain" on Documents, Tickets, Graph, Code Graph, Plans, Features, Access, AI costs, the Audit log and more. With several tabs open, as on a desktop, they cannot be told apart. Pages that set a title (Chat, Runs, AI settings) show what it should look like. |
| 18 | Medium | Application pages | Breadcrumbs stop at the application, so a document, a run or a code review has no trail back to its section. Pages add their own back links instead ("← Back to Code Factory", "Back to documents", "Back to application"), in different places. The breadcrumb's back chevron goes to the organization ("Back to ACME"), not to the parent level. |
| 19 | Medium | Page headings | Four patterns: Knowledge has an `h2` under tabs and no `h1`; Code Graph has eyebrow + `h1` + actions; Code Factory has `h1` with no eyebrow; Chat has `h1` with an inline caption; ServiceOps puts the setup strip above the breadcrumbs. |
| 20 | Medium | Knowledge (every tab), upload popup | The AI enrichment explanation is a two-line banner on every tab. It must keep saying the same thing in all three places, but it does not need two lines on each. |
| 21 | Low | Chat | **Send** is dark navy; every other primary button is brand indigo. |
| 22 | Low | Popups | No × in the header; Cancel sits on its own line under the help text rather than beside the primary button. |
| 23 | Low | AI settings → Personal data | **Save** and **Mask existing sources** are stacked one above the other with different widths, so the second reads as part of the first. |
| 24 | Low | Versions, AI costs, Audit log | "Page 1 of 1" and "Showing 0–0 of 0 sources" are shown where there is nothing to page. |

### Found on the way, not a layout issue

- **The Quality tab returned a 500 on first open** (`OperationalError` in `django.request`,
  at the moment the worker lanes logged one too) and loaded on retry in about 12 seconds.
  It looks like the first draw filling version measures (`graph_trend`) ran past the
  10-second `statement_timeout`. Worth its own investigation; nothing here changes it.
- The document page says "Content check: 2 things to fix" and, a line below, "no check
  failed". The wording disagrees with itself.

## Plan

Three phases, each a commit or two, each small enough to screenshot before and after at
1280, 1440 and 1920. Order is by value for effort.

### Phase 1: fix what looks broken (CSS and template markup, about a day)

1. **Versions table** (#1). `white-space: nowrap` on the version, date, model and
   benchmark cells; the date on one line as `Oct 25 · 04:19`; actions as one compact
   button group with **Publish** a real small primary button, not a link. Let the
   Quality column wrap instead, which is prose.
2. **Quality by version** (#2). Drop the cell padding to the table density used
   elsewhere, keep Version sticky on the left, and fold the paired figures ("16 / 13 of
   16", "75 / 52") so the table fits at 1280. The chart's SVG takes the card's width
   through its `viewBox` and `width: 100%` (it is server-drawn; no script).
3. **Small breaks** (#3, #5, #6): label `white-space: nowrap`; remove the inline chips'
   trailing margin before punctuation; add the gap to the onboarding link.
4. **Graph toolbar** (#4). Give the search field a minimum width that fits its
   placeholder and move the related-code hint into Graph controls, where the other
   explanations are, as full text.

### Phase 2: use the screen (layout, about two to three days)

5. **One row less of chrome** (#7). Move the breadcrumb trail and the focus-mode button
   into the application menu row (application name, then the trail, tabs on the right),
   and drop the separate breadcrumb strip on application pages. About 60px back on every
   page, and the graph canvas starts above the fold at 1440x900. The ids
   `navigation.js` and `focus.js` bind to keep their names; the top bar's content-driven
   height (the reason `--h-chrome-total` was removed) is unaffected.
6. **Graph inspector beside the canvas** (#8). At 1280 and wider, `.kb-graph
   .graph-layout` becomes a two-column grid when a node is selected, the same rule
   `.code-graph-layout:has(.code-inspector:not([hidden]))` already uses, with
   `#graph-inspector` scrolling inside the canvas's height. Cap the canvas at about 72vh
   so the whole graph is visible without scrolling. Add a browser test in
   `test_browser.py` that clicks a node and checks the inspector is in view.
7. **Document page with a side rail** (#9). Checks on the left; on the right a sticky
   rail with size, uploaded, status and every action, Delete last and set apart.
8. **Tickets density** (#10). Graph usage on one line ("v5, v4 +3 earlier", the
   disclosure kept), source and masking chips beside the key instead of under the title,
   and the title column given the space. Target about 56px a row.
9. **Code Graph files** (#11). The file list scrolls inside a panel the height of the
   canvas; the meta line aligns with its file name.
10. **Settings** (#12, #13). Settings navigation `position: sticky`. Features as a
    two-column grid of cards inside each group, with a sticky save bar at the bottom of
    the form (the same `<button>`, so posting is unchanged and `features_declared` still
    goes with it).
11. **AI costs table** (#14). Numeric columns right-aligned; token counts with thousands
    separators (`intcomma`); costs to four places with the currency sign; the total as
    `$4.66`; task names through the same labels AI settings shows; table density.
12. **Quality notes** (#15). The four strips become one "Notes on this version" card,
    one line each, same text.

### Phase 3: consistency (about one to two days)

13. **Page titles** (#17). `{% block title %}` on all 47 templates, as
    `<Page> · <Application> · Digital Brain`, matching the pages that already have one.
14. **Breadcrumbs to the page** (#18). Extend the trail past the application with the
    section and the item (Knowledge › Documents › 0004-fhir-profile-scope.md; Code
    Factory › Runs), point the back chevron at the parent, and remove the per-page back
    links it replaces.
15. **One page heading** (#19). A shared include: eyebrow (application), `h1`, one-line
    description, actions on the right. Used by Knowledge, Code Graph, Code Factory, Chat,
    ServiceOps and Settings; the setup strip goes under the trail on every page.
16. **Compact AI plan** (#20). `_graph_ai_plan.html` keeps its sentence but shows as a
    one-line summary ("AI enrichment on · Claude Sonnet 5.5 · charged to acmeadmin")
    with the rest in a native `<details>`. Same answer, same three places.
17. **Buttons and popups** (#21, #22, #23). Send uses the primary colour; dialogs get a
    header × (a real `<button>` that closes, with a link fallback when script is off) and
    a footer row for primary and Cancel; Personal data's two actions sit in one row,
    each with its own line of explanation.
18. **Paging lines** (#24). Hide the pager and the "Showing" line when there is one page
    or nothing to show.

### How each phase is checked

- Before-and-after screenshots at 1280, 1440 and 1920 of every screen it touches.
- `manage.py collectstatic` and a server restart after any `static/` change, because the
  manifest is cached at boot.
- The five gates in `CLAUDE.md`. `test_security` pins the CSP and the browser tests
  measure text contrast, so both guard against a regression here; phase 2 adds browser
  tests for the graph inspector and the sticky settings navigation.
- Each popup still opens as a page with JavaScript off.

## Worth doing, but more than presentation

These need view or query changes, so they are outside this plan:

- **Audit log filters** by action, actor and date. Seventy-one pages, almost all
  `ai.usage_recorded`, with no way to find anything else.
- **AI costs summaries**: spend by task and by day above the receipts.
- **Overview at a glance**: per application, whether a graph version is published, its
  quality, open runs and this month's spend, so the wide Overview earns its space.
- **The Quality tab's first-load 500** above.
