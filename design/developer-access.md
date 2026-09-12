# Application knowledge endpoints and MCP

## Architecture and security boundary

Each application can expose its own knowledge as an authenticated REST API and remote MCP server for developer code and coding assistants. The proposed routes are `/v1/applications/{applicationId}/knowledge/...` and `/mcp/applications/{applicationId}`. These are interface designs, not live endpoints. A shared service may host many application routes, but every request resolves exactly one application and never federates knowledge across applications.

The request path is **developer code / coding assistant → REST or MCP adapter → identity and application policy → shared Knowledge Retrieval Service → application graph, lexical/vector/hierarchy indexes and evidence store → evidence authorization and response filter → caller**. Optional answer synthesis calls the model gateway with that application's allowed profile. REST and MCP are adapters over the same retrieval implementation used by Chat; neither exposes direct database access.

Authentication identifies the user or workload. Authorization then binds the principal to the organization, application, operation and source permissions. The application ID in a URL, tool argument, resource URI or session is a selector, never a grant. Even a user with access to applications A and B must invoke them separately; an A request cannot traverse B. Database queries, object fetches, caches, traces and background tasks carry that verified context.

## Developer onboarding and who does what

1. The organization administrator defines approved external clients, export classifications and identity policy. This grants no application content access.
2. An application owner with `developer_access.manage` enables REST and/or MCP under **Application Settings → Developer Access**, chooses allowed operations and sets usage limits.
3. The owner grants named users or service identities application-specific scopes and source access. A developer can use these grants but cannot expand them. Service identities have an explicit owner, expiry and revocation path.
4. The developer registers an approved client using the supported authorization flow. Interactive MCP clients use OAuth; unattended REST workloads use approved workload authentication or a named, expiring application-scoped token. Secrets are kept in a secret manager or client credential store, never committed into source code.
5. The setup page presents the application endpoint, capabilities, granted scopes, knowledge version and permitted model profiles. It provides configuration examples tailored to supported clients; remote MCP compatibility must be tested per client.
6. A scoped test query verifies identity, knowledge access, citations and limits. The owner can inspect audit events, rotate credentials or disable access without deleting knowledge.

## Proposed REST contract

| Operation | Illustrative route under the application knowledge prefix | Required scope and behavior |
|---|---|---|
| Retrieve evidence | `POST /retrieve` | `knowledge.query`; query, strategy and budgets; returns supported passages, facts and paths |
| Generate an answer | `POST /answer` | `knowledge.answer` plus query access; optional allowed model profile; returns citations and unknowns |
| Read an entity | `GET /entities/{entityId}` | `knowledge.read`; only authorized properties and claims |
| Traverse relationships | `POST /traverse` | `knowledge.traverse`; bounded typed edges, depth and result count |
| Read supporting evidence | `GET /evidence/{evidenceId}` | `knowledge.evidence`; source ACL checked on every request |
| Inspect release and capabilities | `GET /manifest` | `knowledge.read`; authorized version/index metadata and available operations |

Request fields can include a question, allowed retrieval strategy, knowledge version, maximum results and registered `model_profile_id`. Server policy decides whether overrides are permitted; callers cannot submit arbitrary provider endpoints or credentials. Evidence-only retrieval is the default for developers who want to run their own synthesis model. Answer generation is a separate metered operation.

Responses identify the application, knowledge release, request ID, source versions, evidence spans, graph paths, quality flags, missing evidence and truncation. Answer responses also identify the actual permitted model profile used. Confidence fields distinguish extraction scores from independently verified evidence. Pagination tokens bind application, principal/authorization context, query and release; they cannot become transferable grants. Deleted or revoked sources remain inaccessible even when an older release is requested.

## Proposed MCP contract

Use remote Streamable HTTP with an explicitly supported protocol version and client compatibility matrix. Authenticate HTTP requests and validate token audience for the intended MCP resource. Inbound MCP credentials are never forwarded to model providers, Jira or ServiceNow; downstream services use their own scoped identities. These requirements follow the [MCP authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization); implementation should confirm the selected release and supported client behavior before adopting newer protocol changes.

Expose read-only tools: `search_knowledge`, `get_entity`, `traverse_graph`, `get_evidence`, `get_knowledge_version`, and optionally `answer_question`. Each tool defines a bounded input/output schema and maps to the corresponding Knowledge Retrieval Service operation. `tools/list` only advertises permitted capabilities; every `tools/call` repeats authorization. If resources are offered, URIs such as `knowledge://applications/{id}/evidence/{id}` are identifiers with the same permission checks. Session identifiers never substitute for authentication.

An assistant asks about the Refunds approval process, calls `search_knowledge` against its configured Refunds endpoint, receives cited claims and passages, and may follow a permitted relation using `traverse_graph`. `get_evidence` retrieves the authorized source span. The assistant can synthesize with its own model, or call the optional managed `answer_question` using an allowed profile. None of these tools can edit repositories, approve a Factory plan or mutate the graph. Future write interfaces require separate scopes and existing approval workflows.

## Retrieval internals for external callers

1. Validate credentials, expiry, audience, application membership, scopes and export policy; reject mismatched resource ownership before retrieval.
2. Pin the active permitted knowledge manifest and resolve any authorized retrieval/model profile override.
3. Route the request to lexical/vector retrieval, NaviRAG hierarchy navigation, graph traversal or a hybrid according to its task and available indexes.
4. Resolve graph entry points from exact IDs, lexical matches or semantic candidates. Traverse only authorized edges/nodes within hop and result budgets; enforce source permissions before candidates enter model context.
5. Fetch supporting Markdown spans and merge/deduplicate evidence. A graph claim with no permitted support cannot be exposed merely because its node exists. Hidden relations must not leak through counts, summaries or error messages.
6. Return an evidence packet, or synthesize through the allowed model and verify citation support. Unsupported claims become explicit unknowns.
7. Recheck current revocations before delivery. Audit the principal, application, operations, source IDs, release, actual model usage and export outcome without putting sensitive content in shared logs.

Rate limits, traversal budgets, response-size caps, timeouts and cancellation bound cost. Cache keys include application, authorization context/policy version, knowledge release, query and model profile; changed grants invalidate cached access. Raw Cypher, SQL, arbitrary filesystem paths and unrestricted source download are not part of this API. Application administrators can revoke a client immediately for subsequent requests; already exported information cannot be recalled.

## External models and data export

When a coding assistant receives evidence, information leaves the platform's control. The platform controls whether and what to export, but cannot enforce the assistant's later model choice, retention or onward sharing. An asserted client model name is not proof of a safe destination. Owners must allow the client and data classification explicitly; otherwise deny export. A restricted answer-only interface can reduce disclosed detail but is still an export.

Managed answer calls honor the platform's model gateway and permitted overrides. Evidence-only calls intentionally allow the developer to use their own model outside that gateway, within the organization's approved export agreement. This distinction preserves model flexibility without falsely claiming control over external software. No API or MCP client receives knowledge from another application through shared history, shared caches or broad service credentials.
