# Digital Brain UI redesign plan

Date: 14 September 2026. Scope: presentation and interaction redesign, preserving all existing capabilities.

## Review basis

Reviewed the shared shell, navigation builder, dashboard, Knowledge graph/sources/versions, Documents, Chat, Code Factory, AI settings, tokens, responsive styles, and navigation/modal/focus/polling scripts. Following user sign-in, inspected the available application in the browser at its default narrow size and at 1440×900, 1280×720, and 390×844. The live audit below supersedes the initial sign-in limitation. This document changes no application behavior.

## Direction

Build a calm, readable workspace where the current application, current task, and next action are immediately clear. Keep the existing brand and Django server-rendered architecture. Use the existing token system and cascade layers; avoid another stylesheet of overrides or a frontend framework migration.

## Prioritized findings and proposed changes

| Priority | Evidence in current implementation | Redesign |
| --- | --- | --- |
| P0 | `base.html` stacks global links and account actions above application navigation; pages then add breadcrumbs and section tabs. Several administration links lack current-page attributes. | Keep one compact global header, group administration destinations in an accessible menu, and keep application navigation together. Show one context breadcrumb and a consistent active state at each level. Retain every destination and its existing visibility rules. |
| P0 | `graph.html` combines headings, metrics, source rail, graph tools, nested evidence inspector, and quality rail. `redesign.css` reserves 560px for the two rails below 1400px, before the 256px global sidebar, gaps, or padding. | Give the graph most of the workspace. Make Sources and Quality explicitly toggled panels; open the evidence inspector on selection. Keep full Quality and Versions pages. Default secondary panels closed when space is limited, without removing their contents or actions. |
| P0 | Narrow layouts stack the organization sidebar before the page; graph panes use multiple independent scroll regions and height floors. | Use a mobile navigation drawer with focus return and Escape support. Let forms and ordinary pages scroll naturally. Use a single visible graph panel at a time on narrow screens, with clear Graph/Sources/Quality controls. Preserve a normal-flow fallback without JavaScript. |
| P1 | Document metadata, deletion links, panel labels, and source help frequently use 10–11px text. | Use 14–16px body/form text, 12–13px supporting text, and a clear heading scale. Increase contrast of meaningful metadata; reserve faint colors for decoration. Make icon controls comfortably operable with larger hit areas. |
| P1 | Documents places expanded upload and link-import forms before search and the document list; intake also appears in the graph source rail. | Lead with the document list and search. Put Upload and Import link in a compact action area that reveals the existing forms. Keep the source rail as a shortcut to the same intake capabilities, with consistent labels and feedback. |
| P1 | Chat header combines title, rename/delete, provider details, answer-mode forms, graph-version form, and setup notices. | Keep title and primary conversation controls in one row. Put mode and graph version in a compact, labeled context row; make supporting configuration details expandable. Preserve visible mode/version, cost disclosure, setup blockers, citations, and stream status. Give messages and composer most of the height. |
| P1 | AI settings displays five full model forms and credential instructions together; page headings and form treatments vary. | Use summary sections with expandable per-purpose forms and explicit per-section Save. Separate credential status from operator instructions. Standardize field labels, help text, validation errors, spacing, and action placement. Preserve every field, price, provider, and custom-model option. |
| P1 | Code Factory combines analysis, runs, AI drafting, manual proposals, and plan lists. | Separate Runs and Change plans visually. Show each run's stage, status, failed step, and review action consistently. Use progressive disclosure for evidence and technical outputs, while keeping approval and delivery decisions explicit. |
| P2 | Dashboard includes an access-model metric alongside counts and application cards. | Prioritize application selection and clear organization/product context. Replace the metric-like access explanation with concise supporting text. Keep onboarding and no-access states useful for each role. Do not invent activity data or imply access. |
| P2 | Long names, empty results, unavailable sources, and feature-disabled states use varied treatments. | Establish consistent loading, success, validation, blocked, empty, and failure patterns. Use text plus icon for status; distinguish no data from no search results. Wrap long content and contain wide tables within their own scroll region. |

## Proposed page structure

- Global header: workspace navigation, Overview, Audit log, role-gated Administration menu, account menu containing Password and Sign out.
- Left sidebar: existing organization → portfolio → product → application hierarchy and authorized creation controls; collapsible on desktop and a drawer on mobile.
- Application header: current application and Knowledge / Chat / Code Factory / Settings, generated from existing feature and permission rules.
- Content header: one title, brief context, primary action, optional secondary actions.
- Knowledge: Graph / Sources / Quality / Versions remain available. Sources should clearly expose the existing Documents management destination as well as searchable knowledge entries; do not conflate documents and immutable knowledge records.
- Work area: graph or conversation uses the available height; forms, lists, and administration pages use normal page flow.

## Functionality preservation contract

1. Keep URL names, deep links, query parameters, redirect destinations, and browser back/forward behavior. Preserve graph `version`, quality `panel`, pagination, document/source searches, and conversation/history/message selections.
2. Keep form action/method/enctype, CSRF tokens, field names, hidden action values, named submit buttons, and external form associations. In particular, retain `features_declared`, conversation mode/version forms, file upload behavior, and per-purpose AI saves.
3. Inventory JavaScript selectors before markup edits. Preserve IDs and data attributes used by `chat.js`, `graph.js`, `documents.js`, `modal.js`, `copy.js`, `focus.js`, `navigation.js`, and `ai-settings.js`, or update both sides together with behavior verification. Avoid duplicate controls with duplicate IDs.
4. Keep server-side role checks and feature gating authoritative. Test platform admin, organization admin, owner, contributor, viewer, and no-grant users; independent approval is a separate permission. Platform administration must not imply application access.
5. Preserve chat send/stop, streaming placeholders, server-rendered final messages, citations, rename/delete, pagination, retention information, and explicit answer modes. Do not change provider calls or billing behavior as part of styling.
6. Preserve graph generate → draft → publish, version selection/export, source verification, retry rules, search/filter/expand/pan/zoom/pinning, and source inspection. Paid generation remains an explicit action; never couple it to opening a panel or refreshing a page.
7. Preserve document conversion states, exports, deletion scope, imports, and polling's protections against reloading while someone edits, selects files, or explores the graph. New panel controls must not inadvertently disable updates permanently or discard user state.
8. Preserve Code Factory evidence, independent approval/rejection, repository confirmation, export, and existing delivery gates. Inventory `plan_detail.html` and its server handlers before redesigning that screen; do not infer its capabilities from older README descriptions.
9. Retain progressive enhancement: forms and modal target pages work without JavaScript; graph export remains available. Keep CSP intact, external static assets, and safe server rendering. No inline event handlers or styles.
10. Preserve sidebar/focus preferences, reliable Escape behavior, keyboard focus return, and an always-reachable exit from focus mode. Add accessible names to dialogs and panel controls.

## Delivery sequence and gates

| Phase | Deliverable | Required gate |
| --- | --- | --- |
| 0 — Baseline | Signed-in screenshots and workflow inventory; route/form/selector map; existing test results. Cover representative populated, empty, failure, disabled, and restricted states. | Agree which screenshots demonstrate the reported flaws; record existing failures separately. |
| 1 — Foundations and shell | Refine tokens, typography, shared buttons/forms/notices, global navigation, responsive sidebar, active states. | Every existing destination remains reachable for the same roles; keyboard navigation and focus mode pass. |
| 2 — Knowledge and Documents | Graph-first layout, panel controls, consistent intake, readable source lists and version actions. | Graph interactions, selected versions, publishing/export, conversion updates, uploads/imports, and deletion rules pass. |
| 3 — Chat | Simplified header, message typography, composer layout, responsive history. | Send/stop and synchronous fallback, mode/version changes, citations, history, rename/delete, and errors pass. |
| 4 — Administration and Code Factory | Consistent settings/forms/tables, clearer runs and plan review. | All settings fields/actions and authorization, approval, repository, and delivery gates pass. |
| 5 — Release validation | Full behavior/visual review with collected production static assets. | No unexplained regression; keep each phase independently revertible. |

Implement small, reviewable changes per phase. Do not delete the legacy layer wholesale; remove superseded rules only after their affected screens pass. Avoid database migrations and backend business-logic changes in this redesign. Any necessary interaction repair should be a separately identifiable change with its own behavioral check.

## Acceptance criteria

- Check 1440×900, 1280×720, 1024×768, 768×1024, and 390×844, plus 200% zoom and Windows display scaling. Long names and large datasets must remain usable. No page-wide horizontal scrolling; deliberately wide tables and graph canvases may have bounded overflow.
- On desktop, the graph remains the dominant pane and chat input remains reachable; on short/narrow screens scrolling must expose all controls without overlap. Repeated sidebar/focus toggles must not cause oscillating layout or trap navigation.
- All controls work by keyboard, focus is visible, dialogs/drawers restore focus, and new UI respects reduced motion. Measure text contrast rather than assuming token colors pass; target 4.5:1 for normal text and 3:1 for large text. Aim for 44px touch targets.
- Verify validation errors retain entered values, uploads survive unrelated UI interaction, pending/failed states remain accurate, and action success is distinguishable from a saved draft or background job still running.
- Exercise existing automated coverage for security, creation, configuration, documents, graph versions/quality, chat/streaming, connectors, and Code Factory. Add focused interaction tests for changed behavior, not tests that merely assert CSS class names.
- Before releasing implementation, run the repository checks: Ruff, `manage.py makemigrations --check --dry-run`, `manage.py test`, and `scripts/production_preflight.py`. After static changes, run `collectstatic` and restart the server before the final browser pass, as documented in `CLAUDE.md`.
- Sign off each changed workflow against its baseline. A visual review alone cannot establish that functionality is preserved.

## Expanded interaction audit: tabs, links, popups, and controls

The scope includes every shared interaction pattern and every template family, including authentication, errors, organization administration, membership, branding, features, API access, connectors, and plan delivery. The following table records source findings; the subsequent live audit identifies which were reproduced. Other roles and state-changing workflows remain separate verification work.

| Item | Finding and evidence | Planned correction / verification |
| --- | --- | --- |
| Global active links | `base.html` marks Overview/Audit current, but not Administration, Users, Branding, or Features. | Apply consistent current-page styling and semantics, including nested administration screens. |
| Misleading destinations | `application_nav.html` calls the dashboard link Organizations; `organization_tree.html` calls it All organizations. The dashboard is an application launcher. | Use Overview consistently for this destination. Name the organization back link by its destination; do not imply browser-history back. |
| Section continuity | Documents and document detail/delete omit Knowledge subnavigation, while source detail includes it. | Keep the same section navigation on relevant list/detail pages, with an unambiguous current section and a visible return to the list. |
| Navigation versus local tabs | Knowledge/Settings and Health/Evaluation are real URL links. | Preserve link semantics, open-in-new-tab, back/forward, deep links, and current-page attributes. Use ARIA tab patterns only for actual in-page panels with their required keyboard behavior. |
| Context retention | Graph/Quality links retain selected revision, while Sources/Versions intentionally navigate to broader views. | Define which controls retain version/search/page and which reset them. Label resets clearly; verify history and direct loads rather than appending all parameters indiscriminately. |
| Delete popup Cancel | `document_delete.html` has an ordinary Cancel link without `data-modal-close`. `modal.js` adds a second Cancel button because it only recognizes that attribute. | Give the existing Cancel control the shared close behavior; keep its href for standalone fallback. Exactly one Cancel per dialog, returning focus to the opener. |
| Connector popup continuity | `connectors.html` opens the chooser with `data-modal`, but chooser cards are ordinary links and navigate to a full setup page. | Prefer a full-page connector setup flow, given its credential instructions, or implement a complete dialog step flow. Do not open a temporary popup for only the first step. |
| Accessible popup name | The native dialog in `modal.js` has no `aria-labelledby` or `aria-label`; an embedded heading does not automatically name it. | Bind the dialog to its visible heading, with stable unique IDs; expose validation and loading status accessibly. |
| Popup initial focus | `modal.js` selects the first input/select/textarea/button, which can be the destructive Delete button. It also focuses before `showModal()` on first open. | Set deliberate initial focus after opening: safe Cancel or explanatory heading for destructive dialogs, first useful field for short creation forms. Verify native focus behavior and restoration. |
| Popup loading/races | Fetches have no visible pending state, cancellation, or latest-request guard. | Provide loading/retry feedback and prevent stale responses or rapid clicks from opening the wrong content. Avoid duplicate submissions. |
| Popup failed submission | A non-OK response navigates to the POST action as a GET. This may abandon entered values or an error explanation; network failure after submission has an uncertain outcome. | Preserve the current form and error context, distinguish rejected requests from unknown outcomes, and never automatically repeat consequential POSTs. Keep standalone fallback for initial loading failure. |
| Dynamic controls | `copy.js` binds clipboard and confirmation handlers once to initial DOM. Dialog content is inserted later. Current popup forms mostly avoid these hooks, but a universal-popup redesign would expose the gap. | Use shared initialization or delegated handling for injected controls if expanding popup scope. Verify each confirmation fires exactly once and named submit actions remain intact. |
| Tree affordances | Summary rows combine disclosure, organization links, and small creation links. | Distinguish expand/collapse from navigation and creation with separate hit areas and visible focus. Test Enter/Space and touch without accidental expansion or navigation. |
| Destructive action consistency | Document delete uses a dedicated page/dialog; chat/token/connector removal uses native confirmation; access revoke sits beside Save. | Establish one consistent action hierarchy, clear object-specific wording, safe cancellation, and explicit consequences. Preserve current business rules and confirmation protection during migration. |
| Tooltip-only details | Pipeline agent/cost/error metadata is in `title` attributes; some statuses also rely on tooltips. | Make relevant detail expandable by keyboard and touch. Keep failure explanations visible and actionable. |
| Plan delivery copy | `plan_detail.html` exposes draft-PR delivery while its footer says automation requires an isolated worker. | Align explanatory copy with the actual configured delivery path, preserving separate repository confirmation, approval, and delivery actions. |
| Copy and download links | API tokens/endpoints/snippets, chat copy, document exports, graph JSON, and approved-plan export have different handlers. | Verify correct content, availability, feedback, filenames, selected-version scope, and permission failures separately. Never send test provider requests or deliver PRs merely to verify a link. |

### Complete interaction checklist for implementation

For every rendered link, record its label, destination, allowed role, selected state, context parameters, and return path. Check breadcrumbs, cards, inline links, pagination, evidence/source links, exports, external PR links, and error-page recovery. A URL existing in `urls.py` alone does not prove its destination is valid for the user.

For every button/form, record its action, success result, validation state, pending state, double-submit behavior, permission/feature-disabled behavior, and no-JavaScript fallback. Cover search/filter/reset, uploads/imports, graph generation/publishing/retry, chat send/stop/regenerate/rename/delete, approvals/delivery, configuration saves, membership/access changes, account status, branding, tokens, and sign-in/password/sign-out flows.

For every popup/disclosure/menu, verify opening, loading, readable title, initial focus, keyboard traversal, Escape, backdrop policy, Cancel, validation without losing values, successful redirect, focus restoration, narrow-screen scrolling, and repeated opening. Test modal creation from organization, portfolio, and product contexts; document deletion from detail; and connector creation from both populated and empty states. Use disposable fixtures for state-changing tests, with mocked external calls.

Track each case as **source-reviewed**, **browser-passed**, **failed**, or **blocked**, with viewport, role, and reproduction steps. Do not mark all tabs/links/popups verified until the complete role and workflow matrix has run.

## Live audit results after sign-in

Scope: the supplied signed-in account, which exposes organization creation controls and application Settings but does not expose platform administration. No application data was created, deleted, published, imported, or saved. No provider calls or repository writes were triggered. Temporary viewport overrides were reset and the browser was returned to Overview. Tests below establish specific read-only behavior, not end-to-end functionality of every action.

### Confirmed defects and usability problems

| Priority | Reproduction and observed result | Required redesign response |
| --- | --- | --- |
| P0 | At 390px, open an application and expand its organization tree: the selected application is a blank highlighted row, with no visible or accessible link name in the snapshot. `app.css` hides all `.sidebar nav a span` below 700px, including the application name. | Narrow that rule to decorative elements. Preserve readable application names at every viewport. Add a responsive regression check for link names. |
| P0 | At 1440×900, the graph canvas measures approximately 378×227px while Sources and Quality take 300px and 340px. At 1280×720 the canvas measures about 298×179px and begins around y=770, below the initial viewport. | Reduce stacked page furniture and make secondary panes collapsible. Size the graph according to available content width, with a useful minimum working area. |
| P1 | Open a document, then Delete document: the popup contains a Cancel link and a second Cancel button. Initial focus is on Delete document. | One cancellation control; safe initial focus; preserve standalone navigation fallback. |
| P1 | Inspect the open deletion dialog: no explicit accessible title association. | Associate the visible heading with the native dialog. Verify the same fix across all creation dialogs. |
| P1 | Open Add connector, then select GitHub: the chooser is a popup but the next step replaces the full page. | Make connector setup a consistent full-page flow, or provide complete modal navigation. |
| P1 | Search Knowledge sources for `ui-audit-no-match-987`: the query is retained and results correctly become empty, but the message says “No sources yet” despite existing sources. | Separate no-results copy from an empty application. Show a clear way to clear the search. |
| P1 | Navigate Knowledge → Sources: the section tabs move above the page title, whereas Graph/Quality place them below. Open Documents or document detail: those section tabs disappear. | Use one stable header/tab position across Knowledge list and detail screens. |
| P1 | At 390px, the sidebar occupies the top portion of the page and the application menu requires horizontal scrolling to reach Settings. Chat's heading begins around y=448 and its composer begins around y=1326. | Use a mobile navigation drawer and compact context header; reduce pre-conversation content. Keep all destinations visibly discoverable. |
| P2 | Open a Code Factory run's Review link: its detail page has no direct Back to plans/list link; the prominent back arrow leads to the organization. | Add a correctly named return to Code Factory and preserve the list context. |

### Browser checks that passed within their stated scope

- Overview application card opens Knowledge; Graph, Sources, Quality, and Versions destinations render. Quality → Evaluation retains graph version 19 and updates its current-tab state. Versions lists 19 entries.
- All seven Settings destinations render with the correct selected section: AI settings, AI costs, Connectors, People & access, Features, Chat history, and API access. No page-wide horizontal overflow was measured on those pages at 1440px.
- Document rail link opens document detail; Back to documents opens the listing. Export/download destinations are present; actual downloaded contents were not validated.
- Graph node search finds `node_dump.csv`; selecting and expanding it updates the evidence inspector and count. The UI reports the 400-node display limit. Overview reset is operable. Pan, zoom, drag pinning, and every evidence link still need dedicated interaction checks.
- Chat opens with explicit answer-mode and published graph-version controls. Focus mode opens, Escape exits, and focus returns to its toggle. Sending/streaming/stopping messages was not exercised.
- Portfolio, product, and application creation popups open. Portfolio Cancel closes the popup and restores opener focus; product/application Escape closes and application opener focus is restored. No creation form was submitted.
- The injected Cancel button in document deletion closes the popup and restores focus. This does not remove the duplicate-Cancel defect.
- Code Factory run Review opens its plan detail. No approval, repository confirmation, or delivery action was submitted.
- Audit log Next reaches page 2 of 5. Password navigation opens the change-password screen without entering credentials. Organization navigation opens its application/hierarchy page.
- Sidebar collapse updates both visibility and `aria-expanded`; expansion restores it. No error/warning entries were returned by the final browser log query; this is not proof that every earlier page was error-free.

### Still required before implementation sign-off

Use disposable fixtures and mocked external calls for save/validation/retry/error paths, uploads/imports, confirmations, deletion/revocation, graph publishing/export, chat streaming and history actions, and Code Factory delivery. Verify platform-admin-only pages with an appropriately authorized session; the current account does not expose them. Complete remaining keyboard, touch, browser-history, accessibility/contrast, short-window, zoom/scaling, disabled-feature, and role-specific checks. Neither this audit nor a styling-only diff guarantees zero regressions without those checks.

## Recommended starting point

Start with the mobile application-name defect, shared shell, Knowledge workspace, and popup cancellation/accessibility. Establish the visual system there, then apply it to Documents, Chat, settings, and Code Factory. The live audit establishes concrete starting evidence; complete the remaining role and workflow coverage as each phase is implemented.
