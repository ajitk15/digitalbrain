# ServiceOps test automation verification

Verified 13 October 2026.

- Added the owner-only Test automation tab, closed suite selection, background
  queue, automatic result updates, per-test diagnostics, run history and JSON export.
- Applied migration 0076 and restarted the local managed server at port 8000.
- Executed the actual queued runner for CarePathOps: six browser scenarios passed,
  with zero skips. Receipt: `serviceops-test-run.json`.
- The browser scenarios exercise incident creation, graph-linked evidence, real
  triage queuing/worker execution, live result updates, verified hypotheses,
  human assessment, automation simulation and run history. They also check empty
  evidence, provider outage, changed sources and viewer permissions.
- Six queue/authorization/lifecycle tests passed. The latest browser check also
  verified the Run button and automatic result replacement without a page reload.
- Full project suite: 1,814 tests in 467.808 seconds, OK with two skips. The new
  ServiceOps browser suite had no skips. Log: `.runtime/serviceops-full-tests.log`.
- Ruff, migration consistency, production preflight and scorecard all passed.
  The scorecard reported no regression against the baseline. Production preflight
  validates configuration defaults, not deployed infrastructure.

## Scope

The browser/server/database workflow is exercised in disposable test databases
with synthetic incidents. AI responses are controlled at the provider boundary.
Automation is the application's existing simulated AWX request, not an external
job execution. The test receipt attached to CarePathOps measures this version of
the application code; it does not certify that application's real model settings,
live connector credentials, incident corpus or external remediation system.

Operation guide: `../docs/serviceops-test-automation.md`.
