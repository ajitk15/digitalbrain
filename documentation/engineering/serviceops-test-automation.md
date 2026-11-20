# ServiceOps end-to-end test workbench

An application owner opens **ServiceOps → Test automation** and runs either the
browser journey or the full ServiceOps regression. Results appear in run history,
with per-test timing, diagnostics and a downloadable JSON report. Results update
in place while the background worker runs; Refresh also works without JavaScript.
Only one suite executes at once;
requests are limited to one per application per minute.

The browser journey creates a library checkout timeout incident, finds a similar
resolved incident, queues triage through the page, executes the real triage worker,
watches the JavaScript update, checks a verified hypothesis, records a human
assessment, simulates an AWX request and checks run history. Additional browser
cases cover a provider failure, empty evidence, changed source and viewer access.
The regression suite also runs the existing ServiceOps and knowledge/automation
tests, including connector mapping and application isolation.

## What is real, and what is controlled

Chrome, Playwright, the server routes, JavaScript, graph/evidence retrieval,
validation, persistence and audit events are real. Each suite runs in a separate
Python process with an ephemeral SQLite test database and synthetic records.
Configured AI responses are mocked at the provider boundary. Automation execution
is the application's existing simulation: it records an AWX launch request without
sending it. This is functional end-to-end coverage, not an assertion that a live
model, ServiceNow import or external remediation job has succeeded. External
connector mapping is covered using controlled responses in the regression suite.

The dashboard associates the code test receipt with the requesting application;
it does not inspect that application's live incidents, sources or model settings.
There is no arbitrary URL, script, command or repository execution field.

## Installation and operation

Use the project's development dependencies (`uv sync --group dev`) and an installed
Google Chrome. Apply migrations with `python manage.py migrate`, then restart the
web server and background worker. The existing document worker starts a separate
`serviceops-tests` lane. Execution is disabled in production mode; run suites in
a development or CI environment.

For CI, require the browser so unavailable coverage cannot silently pass:

```powershell
$env:DIGITAL_BRAIN_REQUIRE_BROWSER = '1'
.venv/Scripts/python.exe manage.py test platform_core.tests.test_serviceops_e2e
```

A missing browser, skipped test, timeout, failing assertion or revoked owner grant
cannot produce a passing dashboard run. Interrupted workers become blocked after
twelve minutes. Tests have a ten-minute execution deadline. Reports are owner-only
and scoped to the application, and queued access is checked again before execution.
