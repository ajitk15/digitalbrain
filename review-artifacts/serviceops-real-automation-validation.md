# Real ServiceOps automation — validation

Verified 13 October 2026.

## Delivered

- Native local Python runbook execution through a separate background worker lane.
- ServiceOps **Automation** tab: invoke, monitor, approve, disable connections,
  inspect execution history and verified output.
- Bearer-authenticated catalog, invocation and run-status REST endpoints.
- Owner-configured AWX execution adapter with separate execute credentials,
  approved template IDs, launch/poll separation and ambiguous-outcome handling.
- Extension boundary for additional cloud/external adapters. Azure Automation and
  Rundeck execution adapters are not implemented in this version.

## Actual execution against CarePathOps

Both registered local runbooks were invoked through the application routes and
succeeded. Re-profiling refreshed one incident profile; the graph contained
18 nodes and 18 edges. Source fingerprints, stored counts and profile digests
passed postconditions. Source records were unchanged, human-confirmed edges were
preserved, and repeated request keys did not create duplicate jobs.

A further graph rebuild was queued and completed by the managed background worker
without calling its execution function from the probe. The real receipts are in
`serviceops-real-execution.json`.

## Verification

- Fourteen local/API/approval/AWX-contract tests passed.
- Chrome invoked a real local runbook in an isolated test database, verified its
  actual effects and observed live results without a reload.
- A further targeted guard test passed: an invalid AWX poll requires reconciliation
  and does not falsely claim that the external job failed or was canceled.
- Full project regression: **1,829 tests in 575.244 seconds, OK with two skips**.
  Log: `.runtime/serviceops-real-full-tests.log`.
- Ruff, migration consistency, production preflight and scorecard passed.
  The scorecard reported no baseline regressions.
- Migrations 0076/0077 were applied locally. The managed application was restarted
  to load the execution routes and worker.

## Boundaries

The local jobs perform actual application maintenance; they do not run arbitrary
scripts or restart host services. No paid model call was needed for execution.
AWX transport behavior was tested using controlled responses; no live external
AWX controller or cloud job was invoked. Controller-job success does not prove
target-service recovery. External execution needs an owner-approved connection,
allowed template IDs and the application's separate execute credential.

Guide: `../docs/serviceops-real-automation.md`.
REST example: `serviceops-invoke.http`.
