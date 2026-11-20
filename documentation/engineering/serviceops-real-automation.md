# ServiceOps real automation

Open **ServiceOps → Automation** as an application owner. This is the execution
platform; **Test automation** runs isolated regression suites instead.

## Local platform

Press **Enable local runner**, then invoke one of the registered runbooks:

| Runbook | Actual effects | Postconditions |
|---|---|---|
| Rebuild ServiceOps graph | Calls the real graph builder; replaces machine-built relationships for this application and preserves human-confirmed edges. | Source fingerprint and stored node/edge counts match. |
| Re-profile incidents | Updates each valid incident's stored profile, then rebuilds the graph. | Current source digests match their profiles and graph checks pass. |

These are actual application maintenance jobs, not a simulated request. They do
not restart host services or execute arbitrary scripts. Failed local postconditions
roll back the runbook's database transaction. The job receipt remains in execution
history with status, timing, effects and verification. The browser updates the
result panel automatically while leaving invocation forms editable.

## Invoke with REST

Use a token belonging to an application owner. Browser cookies cannot authenticate
these endpoints; tokens follow the same grants as browser access.

1. GET `/api/v1/applications/<application UUID>/automations/catalog/` to discover
   permitted `automation_id` and `connection_id` pairs.
2. POST `/api/v1/applications/<application UUID>/automations/invoke/` with
   `Authorization: Bearer <token>` and `Content-Type: application/json`:

```json
{
  "automation_id": "<catalog automation UUID>",
  "connection_id": "<catalog connection UUID>",
  "request_key": "<a new UUID for this invocation>",
  "incident_id": "<optional incident UUID from this application>"
}
```

Omit `incident_id` for application maintenance. The response is HTTP 202 with a
run ID and its current status. Repeating the same request key and inputs returns
that same run, including after completion. A different invocation needs a new key.
GET `/api/v1/applications/<application UUID>/automations/runs/<run UUID>/` for the
receipt. POSTing modified inputs under an existing key is rejected.

## External AWX

The AWX adapter is implemented, but a live controller is not provisioned by this
project. Add an owner-configured HTTPS origin and an explicit allowlist of numeric
template IDs in **Automation → Connect external Ansible AWX**. Import those job
templates through the existing AWX connector. Store a separate **AWX execute**
token in **Credentials** or the mounted file `awx_execute_<application UUID>`;
the existing AWX read credential is not used for execution.

The adapter POSTs to `/api/v2/job_templates/<ID>/launch/` and monitors the returned
job through `/api/v2/jobs/<job ID>/`. Configure templates to accept the supplied
`incident`, `ci` and `service` extra variables on launch. Ignored variables are
reported. The response is checked against the allowed template; remote stdout is
not copied into the application because it can contain secrets. Controller success
establishes job completion, not recovery of the target service. See the primary
[AWX API reference](https://docs.ansible.com/projects/awx/en/latest/rest_api/api_ref.html).

All outbound calls use the existing hardened fetcher: validated and pinned network
addresses, TLS verification, bounded responses and no redirects. Internal hosts
require the operator's `fetch_allow_hosts` configuration. An ambiguous launch
response or interrupted launcher becomes **Needs reconciliation**; no automatic
POST retry occurs. Polling can retry safely. Monitoring stops after thirty minutes
without asserting that the external job stopped. Disabling a connection or revoking
an owner's grant blocks further work; it does not cancel an already-running remote job.

## Approval and extension

Only owners can request real execution. If the execution connection or source
runbook requires approval, a different current application owner must approve it.
Both grants, the connection configuration and source digests are checked again by
the worker. Only one queued/launching/running execution is allowed per application.

`platform_core/automation_adapters.py` defines the adapter boundary: `launch` and
`poll` return a `JobResult` containing status, external ID and minimized output.
Local and AWX are implemented today. Azure Automation, Rundeck or other cloud
runners can be added using this boundary, with their own connection validation,
file-backed execution credential, permissions and result checks. They are not
currently executable providers in this version. No imported description can choose
a shell command, executable, arbitrary local path or outbound destination.

## Verification

Tests invoke the actual local runbooks and assert their database effects and
postconditions. A Chrome journey verifies invocation and live results. API tests
cover bearer authentication, catalog discovery, execution and receipt retrieval.
AWX tests cover the transport contract with controlled responses, including an
ambiguous launch that is not retried; they do not certify a live AWX deployment.

Apply migrations and restart the managed application to load the `automations`
worker lane. Test automation's browser suite now also invokes a real local runbook
inside its disposable test database.
