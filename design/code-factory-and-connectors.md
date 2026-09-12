# Code Factory and application connectors

Design proposal • Application-isolated SaaS • No integrations or code execution implemented

[Full platform narrative](architecture-and-workflows.md) · [Code Factory diagram](code-factory-workflow.html)

## Purpose and boundary

Code Factory consumes one application's persisted knowledge and compares it with an authorized, current snapshot of that application's repositories. It accepts two entry points: a user-requested specification audit or a bug/incident imported through that application's Jira or ServiceNow connector. Both produce findings and a plan before any code mutation.

A defect ticket is evidence of a reported problem, not an approved specification and not execution permission. The system distinguishes a confirmed deviation, a likely defect, a missing requirement, a document/code conflict, an obsolete document, and insufficient evidence. Missing graph links alone do not prove missing implementation.

## End-to-end factory narrative

1. **Select scope.** The application owner chooses one application and a feature, selected document set, ticket, or whole-application audit. The scope contains allowed repositories, paths and branches only.
2. **Establish the baseline.** The system pins graph version, approved specification versions and approval status, ticket revision, repository commits, ontology version and test configuration. If documents are drafts or contradictory, it requests a product decision before treating them as authoritative.
3. **Discover code evidence.** Static analysis identifies symbols, interfaces, dependencies and test links. The analysis agent reads permitted source snippets and proposes requirement-to-code mappings. Read-only analysis can run before approval; repository content cannot execute arbitrary tooling or request broader credentials. Running tests in a sandbox uses an explicitly permitted environment.
4. **Classify deviations.** For every requirement, report implemented-and-evidenced, partial, contradicted, missing-with-evidence, or unknown. Inspect API contracts, validation, error handling, business rules and tests. Security/performance claims require suitable measurements or dedicated tests; a graph cannot establish them by itself.
5. **Prepare the plan.** Group actionable findings into a dependency-ordered plan. Show exact evidence, affected modules, proposed changes, acceptance tests, risk, estimates, unresolved questions and expected repository operations. Separate document corrections and product decisions from code fixes. Present graph paths as evidence with their source strength, not as an automatic proof of correctness.
6. **Obtain approval.** The UI shows a versioned plan with approve/reject/request-changes controls. The approver may approve selected independent items; changed selections create a new plan version. Record approver identity, time, application, plan digest, approved scope, graph/spec/ticket/code versions, allowed tools, cost/time limits, PR permission and expiry. Inconsistent dependency subsets cannot execute.
7. **Dispatch execution.** The durable coordinator checks live grants and baseline freshness, then creates application-isolated sandboxes/worktrees. Specialist agents receive only their assigned tasks and permitted evidence. No Jira/ServiceNow token, production credential or unrestricted repository token enters the prompt or workspace.
8. **Integrate and verify.** Independent work can proceed in parallel when file/module ownership does not conflict. The integration agent combines changes, resolves conflicts within approved scope, and reruns verification. Acceptance tests must trace back to approved requirements and the bug reproduction. Record pre-existing failures separately; do not hide failing checks or delete tests to claim success.
9. **Show the result.** Create a draft/review PR if allowed by the plan. Include the original deviation, approved plan, actual diff, tests, reviewer findings, remaining risks and linked ticket. The system reports incomplete work honestly; timeout and budget exhaustion are visible outcomes.
10. **Apply the merge gate.** Plan approval authorizes bounded fixes and permitted PR creation. Merge/deployment require their own configured repository authority and checks. A changed base commit triggers revalidation and a new approval when the pinned baseline changes under the initial strict policy. No silent force pushes or protection bypass.
11. **Close the loop.** After a permitted merge, ingest the merged commit into the same application's graph, rerun the original deviation checks and update the finding. Ticket resolution depends on its configured workflow; an incident may additionally require deployment or service-restoration evidence. A PR by itself is not proof that a ticket is resolved.

## Multi-agent orchestration design

This describes future product behavior, not agents executing in this design task.

| Agent/service | Responsibility | Write authority |
|---|---|---|
| Coordinator with selected compatible model/runtime | Plan tasks, route tools, track evidence and dependencies | Job/plan state through validated service APIs |
| Specification analyst | Interpret approved requirements and resolve traceability | Proposed findings only |
| Code analyst/implementer with selected compatible model/runtime | Inspect allowed code; implement approved bounded changes | Assigned application worktree paths only |
| Test specialist | Reproduce defect and build requirement-linked verification | Approved test paths and sandbox commands |
| Independent reviewer | Check scope, correctness, security and test adequacy | Review findings; cannot approve its own release |
| Integration worker | Combine compatible patches and prepare PR evidence | Brokered branch/PR operations within approved policy |
| Deterministic policy service | Verify capabilities, approvals, limits and baseline identities | Permit/deny; decisions cannot be overridden by model text |

OpenAI and Claude SDKs communicate through typed task contracts and a durable job service. Model assignments remain configurable; approval enforcement is outside the model loops. Graphify builds/analyzes knowledge and does not replace repository verification.

Scheduler constraints: dependency DAG, per-application concurrency limit, per-path ownership or locks, bounded retries, wall-clock/token budgets, heartbeat and cancellation. Every task has a stable idempotency key. A cancellation revokes capabilities and discards unpublished work after preserving audit evidence. A retry starts from recorded checkpoints and cannot duplicate a PR or external update.

Failure routes: failed tests → bounded repair in the existing approved scope; new scope → revised plan and approval; revoked access or changed baseline → pause; provider outage → retry/backoff; integration conflict beyond approved scope → human decision. After any patch integration, the combined result must pass checks again.

## Deviation and approval records

Each deviation includes application ID, requirement/ticket IDs, classification, severity, source quotation/span references, observed code path/commit, expected versus actual behavior, confidence and uncertainty, proposed change, acceptance criteria, reviewer status and graph/spec/code version tuple.

An example: an approved refund specification requires manager authorization above a threshold, but the pinned handler contains no demonstrated check and the reproduction test succeeds without authorization. The plan identifies the handler, policy function and tests; asks the owner to confirm ambiguous threshold wording; then requests approval for the bounded correction. An agent may not infer a policy value from a vague bug description.

A plan approval is immutable and application-bound. Changing target application, source baselines, approved files, material acceptance criteria or execution permissions invalidates it. Execution may narrow operations but cannot expand them. Approval revocation blocks remaining tasks and repository publication.

## Per-application connector setup

Navigation: Organization → Portfolio → Product → Application → Settings → Integrations. Authorized product managers/application owners with `connectors.manage` can configure a binding. Provider administrators may need to authorize the upstream OAuth integration; that is distinct from SaaS role assignment.

Each application has independent connection credentials, scope rules and synchronization state. Two applications may use the same external Jira site or ServiceNow instance, but bindings and imported knowledge stay separate. Reject ambiguous or overlapping ticket ownership under the initial policy; do not duplicate a ticket's private content across applications automatically. A broad provider account does not grant the downstream application broad knowledge access.

### Configuration fields

| Setting | Jira | ServiceNow |
|---|---|---|
| Endpoint/deployment | Jira Cloud site and cloud ID; Data Center needs a separate adapter | Instance URL and confirmed release/API capabilities |
| Authentication | OAuth authorization-code consent for Jira Cloud; application-specific credential reference | Inbound OAuth configured by instance admin; client credentials where enabled and appropriate |
| Ticket scope | Allowed project IDs plus issue type/component/label or approved filter | Allowed tables plus application/service/CI, assignment group and approved filter |
| Field mapping | Summary, description, acceptance criteria, severity, status, comments and attachments | Short description, description, priority, state, relevant fields and permitted journal fields |
| Repository mapping | Explicit repository, branch and path mapping per component/module | Explicit repository, branch and path mapping per service/component |
| Sync | Initial lookback, schedule, webhook support, reconciliation interval | Initial lookback, polling schedule; optional configured outbound events |
| Eligibility | Imported tickets eligible for analysis; no automatic code execution | Eligible incidents/defects/problems; confirm type-specific semantics |
| Writeback | Optional PR link/comment and allowed status transitions | Optional work-note/link and approved state transitions; preserve journal visibility |
| Governance | Application owner, approvers, credential expiry/rotation, retention | Same, plus any instance table/field ACL requirements |

The setup wizard guides the user through:

1. Choose provider and give the application binding a name.
2. Authenticate in the provider's secure consent/setup flow; credentials are stored only in the secret manager.
3. Select explicit projects/tables and application-specific filters. The backend checks endpoint allowlists, query safety, privileges and scope boundaries.
4. Map fields and repository paths. Unmapped tickets enter a triage inbox; agents must not guess another application or repository.
5. Choose analysis triggers, plan approvers, polling and optional writeback actions. Default is read/import/analysis, with code changes gated by plan approval.
6. Test connection and preview a bounded sample of matching tickets plus the effective scope. Prove that an out-of-scope test ticket is rejected.
7. Save and enable. Show connection health, last successful sync, imported count, rejected count, credential status and a pause/disconnect action.

For Jira Cloud, OAuth 2.0 authorization-code grants are supported for external integrations. Permissions remain constrained by the authorizing account; confirm suitable long-running consent/refresh-token arrangements during setup. [Atlassian OAuth documentation](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/).

Jira's dynamic webhooks support selected filters and require renewal; REST-registered webhooks currently expire after 30 days. Plan renewal and polling reconciliation rather than assuming permanent event delivery. [Jira webhook API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-webhooks/).

ServiceNow supports inbound OAuth client credentials when configured; its documentation says the enabling system property is off by default. Validate the customer's instance instead of assuming availability. Use a restricted API identity with only required table/field/record access. [Client credentials](https://www.servicenow.com/docs/r/platform-security/authentication/client-credentials.html), [API request user and table access](https://developer.servicenow.com/dev.do?_escaped_fragment_=/learn/learning-plans/xanadu/servicenow_application_developer/app_store_learnv2_rest_xanadu_adding_security_to_inbound_requests).

### Ticket ingestion and security

Webhook/event or scheduled poll → resolve binding from trusted server configuration → verify sender where supported → refetch ticket through the binding's credentials → enforce application filter and source access → normalize/version ticket → store privately → propose analysis.

Never trust an application ID embedded in a webhook body. Verify event authenticity using the selected provider's supported mechanism; if it cannot be verified reliably, treat the event only as a hint and authorize a refetch, or use polling. Defend custom endpoints against SSRF, redirects and unintended private-network access; private instances require an explicitly configured private connectivity route.

Use pagination, rate-limit backoff, checkpoints, overlapping update windows and deduplication by binding + source record + revision/event. Reconcile deletions, revoked visibility and tickets that leave the application filter; tombstone inaccessible content and invalidate its derived evidence. Source issue security and restricted comments/journal fields must be mapped conservatively or excluded. Knowledge grants cannot broaden provider restrictions.

Store normalized Ticket records with organization/application/binding, source ID, revision, source URL, permitted fields, effective ACL and sync timestamps. Import attachments through the same scanning pipeline as uploads. Sync/import does not grant permission for execution.

For writeback, use a separate capability and outbox: verify application and live provider access, render the permitted message, deduplicate by run/event, then execute an allowed transition. Do not echo sensitive code into broadly visible ticket comments. Failed writeback is retried without rerunning the code fix. Incoming echoes of the platform's own updates cannot create an execution loop.

Disconnect revokes capabilities, stops ingestion and writeback, and applies configured retention/tombstones to imported data. No external application has been connected during this design task.

## Model flexibility and developer access

[Model selection policy](model-selection.md) applies to every stage and individual agent: users select compatible profiles within application permissions, with explicit fallbacks and audited execution. Code Factory approval pins the model map; embedding changes rebuild the corresponding index. [Application REST and MCP access](developer-access.md) exposes the same authorized retrieval pipeline to developer code and coding assistants. External calls cannot cross application boundaries or bypass source permissions and Factory approval. The consolidated narrative includes both contracts in full.

