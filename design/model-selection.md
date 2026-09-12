# Model selection and execution policy

## User experience and configuration ownership

Every application exposes **Settings → AI Models & Providers**. Its authorized owner can register an approved provider connection, choose models for each capability, and inspect inherited defaults. A connection stores a secret reference, endpoint, region, supported capabilities and permitted applications; credentials never enter documents, graphs or prompts. Hosted, private and self-hosted models are supported through compatible adapters, subject to organizational data policy.

The configuration screen shows the effective model, provider, runtime, parameter limits, estimated usage and fallback for each slot. Chat users can select an allowed answer model before a conversation; graph contributors can select an allowed generation profile before a build; Code Factory plans show the complete per-agent model map before approval. Developer callers may supply an authorized profile ID. These are proposed product capabilities, not deployed settings.

| Responsibility | Permission and boundary |
|---|---|
| SaaS administrator | Maintain the supported adapter/catalog metadata and platform limits; no automatic application knowledge access |
| Organization administrator | Set provider, residency, retention and spending constraints; delegate configuration rights |
| Portfolio/product manager | Set capability-specific defaults for their scope where delegated; cannot bypass organization policy or read application content through model configuration |
| Application owner/model administrator | `models.configure`: choose application, feature and agent defaults; `providers.manage`: bind permitted secret references |
| Application user/developer | `models.select`: override a permitted profile for an authorized invocation; cannot change persistent defaults or reveal secrets |
| Approver | Approve the Code Factory plan including models, permitted fallback choices and data destinations; approval does not itself grant execution access |

## Independent model slots

| Capability | Selectable model roles |
|---|---|
| Document preparation | Optional OCR/vision model for difficult scans; ordinary MarkItDown conversion does not require an LLM |
| Graph generation | Extraction, semantic entity resolution, summaries and independent quality judge |
| Search indexes | Embedding model; optional learned reranker; hierarchy summary model |
| Retrieval | Query rewrite/planning, entity linking, NaviRAG navigation, reranking and final answer are separate slots |
| Chat | Answer/synthesis model and permitted retrieval profile |
| Code Factory | Coordinator, specification analyst, code analyst, implementer, test specialist and reviewer each have their own model |
| Future applications/agents | Register required input/output and tool capabilities, then bind a compatible profile using the same mechanism |

Graph traversal, lexical search, authorization and schema validation are deterministic services. “Choose a retrieval model” does not mean database traversal itself requires an LLM. A graph build may use one model for extraction and another for quality review; Chat can use a third without rebuilding that graph.

## Resolution and runtime architecture

The selection flow is: **application-scoped invocation → resolve defaults and overrides → validate capabilities and policy → freeze execution profile → compatible SDK/runtime → model gateway → chosen endpoint → verify result → record actual usage**. The diagram specification is [model-routing.json](model-routing.json); its rendered diagram is pending validation.

For each capability, precedence is explicit invocation override, agent default, task-purpose default, feature default, application default, product default, portfolio default, organization default, then platform default. Skip unset levels. Resolve only models with the required capability: a chat default must never accidentally become an embedding model. Personal preferences become invocation overrides only when the user has permission. Hard policy constraints apply after resolution and cannot be overridden by a more specific setting.

A model profile identifies provider connection, exact model/deployment, version where available, compatible runtime adapter, context limits, structured-output/tool support, parameters, budgets and explicit fallback list. Unsupported choices fail before execution with an explanation. Provider aliases that can change are recorded as aliases together with the actual served version when exposed; reproducibility is limited when the provider does not expose or pin a version.

Both requested SDKs remain available. The OpenAI Agents SDK and Claude Agent SDK participate through typed task contracts owned by the durable orchestrator. Runtime and model are separate choices: use only combinations actually supported by the SDK/provider adapter. Claude Agent SDK's model configuration is for supported Claude models; it is not a universal arbitrary-model switch. An incompatible provider needs a supported alternate adapter/runtime, not a relabeled SDK. The Graphify adapter must similarly be checked for configurable model support before implementation; if unavailable, expose only supported profiles and keep external extraction integration as a separately validated adapter contract. See [OpenAI model configuration](https://developers.openai.com/api/docs/guides/agents/models) and [Claude SDK configuration](https://code.claude.com/docs/en/agent-sdk/python).

The model gateway enforces application-specific credential use, destination policy, quotas and redacted telemetry. SDK runtimes that cannot route through that gateway must use an equivalently controlled provider adapter with restricted egress. Hidden helper, compaction or subagent calls must be configured and audited too; unsupported runtime behavior must block registration for restricted workloads. Provider sessions, prompt caches and traces are application-scoped. Shared model infrastructure never implies shared application knowledge.

## Failures, approval and version changes

Before a run, freeze its resolved profile map with the knowledge version, prompt/schema versions, model parameters and allowed fallbacks. Record the actual provider/model for each call, cost, latency and output verification results. Do not record secrets or unrestricted evidence in shared logs.

Unavailable models fail or wait unless the owner explicitly configured an allowed, compatible fallback. Never silently send application data to another provider. Factory approval binds the model and fallback map alongside repository baseline, task scope and tools. An unlisted model or destination requires an amended plan and renewed approval; a listed fallback can execute under the existing approval. Retrying model calls cannot repeat already completed side effects.

An embedding change requires rebuilding all affected vectors with compatible preprocessing, model/version, dimensions and distance metric. Equal dimensions alone do not make two embedding spaces compatible. Queries use the embedding profile pinned to the active index manifest. Build and validate the new index in isolation, then atomically switch the application release; retain a permitted rollback version while honoring current revocations.

Changing extraction models produces a new candidate graph release and repeats quality gates. Changing hierarchy summaries rebuilds the affected hierarchy index. Changing only the answer model usually needs no graph rebuild, but it still needs retrieval/answer evaluation. Compare candidate profiles on the application's approved evaluation set for evidence coverage, factual support, isolation, cost and latency; model self-confidence is not a quality score.
