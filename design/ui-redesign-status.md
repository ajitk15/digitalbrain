# UI redesign implementation status

Updated 15 September 2026. Implements the approved UI redesign while retaining the existing Django routes, server permissions, form fields, and application actions.

Desktop is the primary design and verification target, per the user's clarification. Responsive support is a fallback; no further mobile-specific redesign is planned.

## Implemented

- A mobile navigation drawer with readable application names, an explicit Close action, Escape handling, keyboard containment, and focus return. Desktop collapse preference remains separate from mobile state.
- Compact global navigation with native Administration and Account disclosures. Existing destinations and role checks remain in place.
- Consistent Knowledge navigation on Graph, Sources, Quality, Versions, Documents, and their detail pages; clearer Overview and return-link labels.
- A wider graph workspace with optional source/quality panels. Graph metrics and instructions use disclosures. The SVG retains its original proportions and graph interaction code is unchanged.
- Collapsible document intake forms, a direct Add source action, and separate empty-search messaging with a clear-search link.
- Responsive Chat history, more readable messages, and preserved composer, mode, graph-version, citation, and streaming controls.
- Individually expandable AI settings forms that automatically open on validation errors. All original fields and independent Save actions remain.
- Full-page connector creation; short creation and deletion flows retain standalone pages and popup enhancement.
- Popups with explicit accessible names, one Cancel control, safe initial focus for deletion, loading/cancellation, stale-request protection, duplicate-submit protection, and retained form contents on submission errors. Unknown outcomes are not automatically retried. Expired-session redirects go to the full sign-in page.
- Destructive confirmation handling also covers dynamically inserted forms.
- Code Factory return navigation and expandable, keyboard-accessible pipeline details.
- Shared typography, field sizing, focus indicators, tab states, and table overflow treatment.

## Verification

- Full Django suite: **564 tests passed** (30.576 seconds), including new regressions for empty source searches, retained source fields, invalid AI settings, and upload errors inside disclosures.
- Ruff: passed. Migration dry run: no changes. Production-default preflight: passed.
- Node syntax checks: passed for modal, navigation, workspace, and copy scripts.
- Static assets collected and project-managed local server restarted successfully.
- Earlier implementation browser checks confirmed expanded graph width, visible mobile application names, drawer opening/closing, Escape order, and focus return. The login expired during that pass; this led to the popup sign-in redirect fix.
- Final signed-in desktop pass completed at 1280×720 and 1440×900 for the available application account. At 1280px the graph canvas is 935px wide (previously about 298px), and opening a side panel leaves a 623px canvas. Source/quality panels switch correctly.
- Delete dialog has one Cancel control, an accessible title, initial focus on Cancel, and correct opener focus restoration. Upload and Add source links open their disclosures.
- All seven Settings destinations render with the correct selected tab and no measured page-wide horizontal overflow at 1440px. AI settings expand to expose their retained fields. Connector creation now opens a full page.
- Desktop Chat retains visible history and a composer ending at y=879 in a 900px viewport. Focus mode exits with Escape and restores focus. Pipeline details expand to show stage, duration, and token information.
- Graph node search, expansion/evidence inspection, Overview reset, Zoom in, and Fit were exercised on final assets. The final browser log query returned no warnings or errors.

The suite still emits an unawaited `sleep` coroutine RuntimeWarning after passing, as it did in the earlier 560-test run. No UI failure was reported by the tests; the warning was not investigated as part of this presentation change.

No live provider call, upload, deletion, approval, publishing action, access change, or repository write was used for browser verification. Business-logic coverage for those operations comes from the existing automated suite; this does not establish live external-service behavior.

## Remaining release checks

The available-account desktop pass is complete. Platform-administrator-only pages, exhaustive role/feature combinations, zoom/display scaling, and live external services were not fully browser-tested. Their existing server-side automated coverage passed. No database or API migrations are required for these changes.

## Manual note creation removed

Per the user's request, manual note creation is no longer available. Removed the Sources page's Add source form and the graph rail's Paste text action. Direct creation POSTs are rejected after the existing access checks. Existing knowledge records and internal import helpers remain intact. Document uploads, link imports, connector ingestion, and knowledge search remain available.

## Unified source library and graph usage

Sources and Documents now share one Sources tab and one inventory. Uploaded files, link imports, and standalone knowledge records appear once each. Existing Documents and Knowledge URLs remain valid; upload and link forms submit to the Documents endpoint. Manual creation remains unavailable.

The inventory searches filenames, URLs, and active searchable content, preserves pagination and document actions, and shows origin, processing/content availability, and graph usage. Version badges match source ID and digest within the application, with Published/Draft labels and expandable earlier versions. Source and document details also show version usage. Knowledge-disabled applications do not expose searchable records through the combined inventory. No data migration or saved-source edits were made.

Validation: 567 tests passed, Ruff passed, no migrations detected, and production-default preflight passed. The existing unawaited sleep warning still appears after the test suite passes. The project server was restarted successfully. Live desktop visual verification is pending because the available browser session is signed out; the sign-in page is ready for the user. No live upload, deletion, graph generation, or publication was performed.
