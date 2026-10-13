# Digital Brain — latest issue revalidation

**Revision:** `11dfd8c` · **Date:** 12 October 2026 (IST)

**Status:** **All five reported defects and the remaining R01 stage-order issue are verified fixed within the tested scope. All five repository verification gates pass.**

| Finding | Latest status |
|---|---|
| D01 — saved triage evidence disclosure | Fixed; excluded-reader and restricted-source requests return 409 without the excerpt; regression tests cover REST, MCP and browser readers. |
| D02 — missing Code Factory review stage | Fixed; stage arrival and newly inserted selection controls have browser regression coverage. |
| D03 — desktop chat overlap | Fixed; desktop geometry and latest-answer checks cover laptop and larger desktop widths. |
| D04 — interrupted chat stream | Fixed for tested interruption; the UI reports loss of connection and reconciles the saved stopped answer without reopening paid work. |
| D05 — malformed API mode | Fixed; arrays/objects return JSON 400 rather than HTML 500. |
| R01 — several stages inserted in reverse order | Fixed in `11dfd8c`; inserted stages are now added to the current-node map. The actual refresh function produces 1→2→3→4→5→6→7→cost when stages 5–7 arrive together. A new browser test compares multi-stage live insertion with a reload. |
| O01 — source reading policy | Clarified in the connector UI. Direct source reading remains application-wide by explicit design; this is not source-level confidentiality. |
| O02 — sidebar type label wrapping | Fixed; desktop browser coverage checks the label stays on one line. |

## Verification

| Gate | Result |
|---|---|
| Ruff | **PASS** |
| Migration drift | **PASS — no changes detected** |
| Full suite with `DIGITAL_BRAIN_REQUIRE_BROWSER=1` | **PASS — 1789 tests run, 1787 passed, 2 skipped; 298.256 seconds** |
| Production-default preflight | **PASS — no issues** |
| Scorecard `--check`, after the suite | **PASS — no regressions; graph median 7.0 ms, keyword median 4.4 ms** |

The skips are unchanged: PostgreSQL-only full-text behavior under SQLite, and symlink creation unavailable to this account. Required Chrome tests ran and passed, including the new multi-stage order test, review-control initialization, desktop chat geometry, stream interruption recovery, sidebar wrapping and the formerly timed-out live-progress test.

Evidence: [full suite output](C:/Workspace/digitalbrian/.runtime/revalidation-latest-tests.log), [scorecard](C:/Workspace/digitalbrian/review-artifacts/revalidation-latest-scorecard.log), [API probes](C:/Workspace/digitalbrian/review-artifacts/revalidation-latest-api-probes.log), [saved-brief probes](C:/Workspace/digitalbrian/review-artifacts/revalidation-latest-triage-probes.log).

Evidence for the previously remaining R01: [actual refresh-function output](C:/Workspace/digitalbrian/review-artifacts/revalidation-latest-stage-order.json). The harness runs the repository's `liveKeys`, `refreshLive` and `neighbour` functions against a synthetic DOM; it does not reimplement their insertion logic. Expected stage order is asserted independently.

The earlier full-suite selector timeout at `3b3f46a` passed on the 78-test focused rerun. The latest full-suite result above is the current evidence, rather than assuming that earlier timeout was harmless.

## Boundaries

Revalidation used synthetic fixtures, mocked model calls, isolated test databases, and required Chrome coverage. Collected static assets were refreshed. No application source code was changed by this revalidation, and no live AI calls, real connector imports, repository writes, or external pull requests were performed.

Automatic approval review rejected the optional manual browser sign-in, treating its synthetic fixture password as an untrusted credential. I did not retry or bypass that sign-in. Automated Chrome tests were already authorized and running; they and the independent refresh-function harness provide the validation evidence.

This closes the reported code defects only to the tested scope. Direct source-reading policy, production PostgreSQL coverage, real integrations, deployment restarts, backup/restore and production load remain the previously stated boundaries.
