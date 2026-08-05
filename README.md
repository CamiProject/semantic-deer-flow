# Semantic DeerFlow

[English](./README.md) | [中文](./README_zh.md) | [日本語](./README_ja.md) |
[Français](./README_fr.md) | [Русский](./README_ru.md)

Semantic DeerFlow is an unofficial downstream project based on
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow). It is not affiliated
with, maintained by, or endorsed by ByteDance or the official DeerFlow project.

This downstream focuses on governed semantic queries and controlled actions for
multi-tenant SaaS backends. Its primary integration surface is the Gateway,
Semantic, and Action APIs. The bundled Next.js frontend is currently a development
and debugging interface; a separate product frontend has not yet been completed.

[Upstream source and attribution](./ORIGINAL_README.md)

## Key Capabilities

### Agent Runtime

- LangGraph-based lead agent with streaming runs, thread persistence, checkpoints,
  long-term memory, and context compaction.
- Dynamic subagent delegation for research, coding, data analysis, and other
  specialized tasks, with isolated context and controlled tool access.
- Sandboxed file and command execution through local, container, provisioner, or
  cloud sandbox providers.
- Extensible built-in, community, MCP, and skill tools, including deferred tool
  discovery and request-scoped secret handling.
- Configurable model providers, model routing, token accounting, tracing,
  guardrails, and circuit-breaker behavior.
- Web, API, scheduled-task, and IM-channel entry points sharing the same thread and
  run lifecycle.

### Semantic Governance And Actions

- Signed tenant and principal authorization context with role, Scope, audience,
  expiry, and permission-version checks.
- Ontology-backed objects, relationships, metrics, dimensions, and explainable
  metric definitions.
- SQL Scope Policy compilation that constrains databases, tables, fields, and row
  predicates before a query reaches the data source.
- Semantic-first SaaS query routing with controlled scoped-SQL fallback and
  fail-closed behavior when policy services are unavailable.
- Approval-gated Action proposals with IAM revalidation, correlation metadata,
  idempotency, audit trails, and an isolated Action Worker that alone receives write
  credentials.

### Development And Evaluation

- Fake IAM and Domain APIs that exercise JWT, role, Scope, permission-version,
  approval, worker-token, and `Idempotency-Key` boundaries.
- An in-memory SQLite data source for local demos without a tenant configuration
  database or MySQL service.
- Offline SaaS agent Evals covering semantic reads, routing, authorization denial,
  approval, execution, idempotency, trajectory constraints, and forbidden side
  effects.

The upstream DeerFlow agent harness remains intact, including `deerflow.*` imports,
`DEER_FLOW_*` environment variables, Gateway APIs, runtime data structures, and
Docker compatibility names.

## Demo Data

The bundled SaaS examples use intentionally fictional values:

| Field            | Demo value                           |
| ---------------- | ------------------------------------ |
| Tenant ID / code | `public-tenant-001` / `public_demo`  |
| Principal ID     | `public-user-001`                    |
| Site / project   | `site-demo-001` / `project-demo-001` |
| System code      | `demo`                               |
| Database         | `semantic_demo`                      |
| Tables           | `demo_sites`, `demo_projects`        |

`DEER_FLOW_SEMANTIC_DEMO_DATA=true` selects the bundled in-memory SQLite demo.
Disable it before connecting a real tenant data-source registry.

## Architecture

The runtime is split into a user-facing Gateway and internal semantic services:

```text
Browser / API client / IM / Scheduler
                 |
              Nginx :2026
                 |
       +---------+---------+
       |                   |
  Frontend :3000     Gateway :8001
                         |
              +----------+----------+
              |                     |
       Semantic API :8003     Action Worker
              |                     |
       Ontology + Scope       Fake/real Domain API
       Policy + query path
```

The Gateway owns authentication, threads, runs, streaming, model selection, memory,
skills, MCP, uploads, artifacts, scheduled tasks, and channel adapters. Semantic API
is responsible for ontology resolution, authorization Scope, semantic query planning,
SQL policy enforcement, and Action preflight. The Action Worker is the only component
that should receive write credentials. The frontend is a development/debugging client
of these APIs, not a second agent runtime.

| Service       | Port   | Purpose                                                           |
| ------------- | ------ | ----------------------------------------------------------------- |
| Nginx         | `2026` | Unified browser and API entry point                               |
| Gateway API   | `8001` | REST API and LangGraph-compatible agent runtime                   |
| Semantic API  | `8003` | Internal Ontology, scoped query, policy, and Action orchestration |
| Action Worker | none   | Isolated approved-write executor                                  |
| Frontend      | `3000` | Development and debugging UI                                      |
| Eval Fixture  | `8004` | Opt-in synthetic IAM and Domain API                               |

Semantic API and Action Worker are internal services and are not exposed by nginx.
The Action Worker is the only semantic service that should receive Domain API write
credentials.

## Quick Start

Requirements: Python 3.12+, Node.js 22+, pnpm 10.26.2+, `uv`, and GNU Make in a
bash-compatible environment. Docker is optional.

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make setup
make dev
```

Open <http://localhost:2026>. Configure at least one model provider before starting
an agent run. The Semantic demo itself requires no tenant configuration
database or MySQL server.

### Configuration

| File                     | Responsibility                                                                                     |
| ------------------------ | -------------------------------------------------------------------------------------------------- |
| `.env`                   | API keys, service tokens, JWT verification keys, database URLs, and deployment secrets             |
| `config.yaml`            | Models, sandbox, persistence, auth, scheduler, channels, guardrails, tracing, and runtime behavior |
| `extensions_config.json` | MCP servers, tool extensions, and enabled skills                                                   |

`make setup` creates a minimal configuration interactively. Use `make config` to
copy the full examples and `make doctor` to validate tools and configuration. A
minimal OpenAI-compatible model entry looks like this:

```yaml
models:
  - name: primary-model
    display_name: Primary Model
    use: langchain_openai:ChatOpenAI
    model: provider-model-id
    api_key: $OPENAI_API_KEY
    base_url: https://api.openai.com/v1
    supports_thinking: false
    supports_vision: false

sandbox:
  use: deerflow.sandbox.local:LocalSandboxProvider
```

Use a model with reliable tool calling for agent and semantic workflows. Provider,
local-model, Responses API, CLI-backed model, model-routing, sandbox, MCP, tracing,
memory, and channel examples are documented in
[config.example.yaml](./config.example.yaml) and
[extensions_config.example.json](./extensions_config.example.json).

### Startup Modes

Choose one mode for a local checkout:

| Mode                  | Command                                 | Use case                                                     |
| --------------------- | --------------------------------------- | ------------------------------------------------------------ |
| Local development     | `make dev`                              | Hot reload for Gateway, Semantic API, frontend, and Nginx    |
| Local production-like | `make start`                            | Optimized local processes without hot reload                 |
| Local background      | `make dev-daemon` / `make start-daemon` | Keep the stack running in the background                     |
| Docker development    | `make docker-init && make docker-start` | Reproducible services with source mounts                     |
| Docker production     | `make up`                               | Built images and persistent service volumes                  |
| Single module         | `cd backend && make dev`                | Gateway-only backend work; run frontend separately if needed |

Stop local processes with `make stop`, Docker development with `make docker-stop`,
and production containers with `make down`. The browser entry remains
`http://localhost:2026`; direct service ports are primarily for development and
internal service checks.

## SaaS API Integration

Trusted callers authenticate to Gateway and pass short-lived signed authorization
context containing tenant, principal, roles, Scope, and permission version. Gateway
forwards the verified context to Semantic API. Semantic queries compile through the
Ontology and SQL Scope Policy; Actions require proposal, approval, revalidation, and
idempotent execution by Action Worker.

Demo mode is deliberately narrow and accepts only the bundled synthetic
tenant. For a real integration:

1. Set `DEER_FLOW_SEMANTIC_DEMO_DATA=false`.
2. Configure JWT verification, internal service tokens, and the tenant data-source
   registry through environment variables.
3. Supply deployment-specific Ontology and SQL Scope Policy files.
4. Configure IAM revalidation and Domain API endpoints for Action Worker.
5. Keep read and write credentials separate, and never expose Semantic API or Action
   Worker directly through the public reverse proxy.

See [.env.example](./.env.example), [config.example.yaml](./config.example.yaml), and
[backend/AGENTS.md](./backend/AGENTS.md) for the complete configuration boundaries.

## API Usage

The examples below use the unified Nginx entry point. Add the deployment's local
session authentication or a signed `X-SaaS-Authorization-Context` header as required;
the placeholder values below are not credentials.

### Health And Models

```bash
curl http://localhost:2026/health
curl http://localhost:2026/api/models
```

### LangGraph-Compatible Thread And Run

```bash
BASE=http://localhost:2026/api/langgraph
THREAD_ID=$(curl -s -X POST "$BASE/threads" \
  -H 'Content-Type: application/json' \
  -d '{"metadata":{"source":"demo"}}' | jq -r .thread_id)

curl -N -X POST "$BASE/threads/$THREAD_ID/runs/stream" \
  -H 'Content-Type: application/json' \
  -d '{
    "assistant_id":"lead_agent",
    "input":{"messages":[{"role":"user","content":"Summarize the demo workspace."}]},
    "context":{"thinking_enabled":false},
    "stream_mode":["values","messages-tuple","custom"]
  }'
```

The stream uses Server-Sent Events. Supported modes include `values`,
`messages-tuple`, `custom`, `updates`, `events`, `debug`, `tasks`, and `checkpoints`;
the deprecated `tools` mode should not be sent.

### Semantic-First SaaS Query

For a stateless run:

```bash
curl -s -X POST http://localhost:2026/api/runs/saas-query/wait \
  -H 'Content-Type: application/json' \
  -H 'X-SaaS-Authorization-Context: <signed-jwt>' \
  -d '{
    "input":{"messages":[{"role":"user","content":"Count my visible sites."}]},
    "context":{"model_name":"configured-model"}
  }'
```

Use `/api/runs/saas-query/stream` for SSE, or
`/api/threads/{thread_id}/runs/saas-query/{wait,stream}` when the run must remain
attached to an existing thread. The signed context supplies the tenant, principal,
roles, Scope, and permission version; callers must not put database or Scope values in
the user message or use client-supplied context to override them.

The complete endpoint reference is in [backend/docs/API.md](./backend/docs/API.md).

## Python Client And TUI

The embedded harness can be used without the web frontend after configuring a model:

```python
from deerflow.client import DeerFlowClient

client = DeerFlowClient()
print(client.chat("Explain the capabilities of this workspace."))

for event in client.stream("List the next steps", thread_id="demo-thread"):
    print(event)
```

The optional terminal workbench uses the same client and persistence layer:

```bash
cd backend
uv run deerflow --tui
uv run deerflow --print "Give me a short status report"
uv run deerflow --json "List configured tools"
```

The `deerflow` console name and Python import namespace remain unchanged for existing
integrations.

## Scheduled Tasks And Channels

Enable `scheduler.enabled` in `config.yaml` to create recurring tasks through the
Gateway scheduled-task API. Scheduled runs use the normal run lifecycle but are
non-interactive: clarification prompts are excluded from scheduler-launched runs.
Configure the task service only after setting a model and persistent run storage.

Gateway also supports Telegram, Slack, Feishu/Lark, DingTalk, Discord, WeChat/WeCom,
and other configured channels. Long-polling or WebSocket transports can avoid a
public callback endpoint; credentials stay in `.env` and channel configuration.
See [backend/docs/IM_CHANNEL_CONNECTIONS.md](./backend/docs/IM_CHANNEL_CONNECTIONS.md).

## Evals

The Fake IAM/Domain service validates control and worker tokens, signed JWT claims,
roles, Scope, permission versions, approval flow, Action correlation headers, and
`Idempotency-Key` behavior.

```bash
make eval-fixture
make eval-smoke
```

Evals are opt-in and run as a one-shot CLI, not as a permanent service. The committed
suite contains 12 cases for semantic reads, routing, Scope denial, Action approval,
Action execution, idempotency, and forbidden side effects. The Fixture exposes only
synthetic IAM/Domain state and the eval overlay enables the in-memory SQLite data.

For a local process setup, configure the eval-only variables in `.env`, start the
Fixture in one shell, start Gateway/Semantic/Worker with `DEER_FLOW_ENV=eval`, then
run the suite from another shell:

```bash
make eval-fixture
make eval-smoke EVALS_GATEWAY_URL=http://127.0.0.1:8001
```

For Docker, use the base compose file with `docker/docker-compose-evals.yaml`; the
overlay starts the Fixture, Semantic API, and Action Worker on loopback ports and
enables `DEER_FLOW_SEMANTIC_DEMO_DATA=true`. Evals require a configured model and
short-lived eval tokens. Never reuse eval keys, fixture tokens, or demo data settings
in a production environment.

## Development

```bash
make doctor
make install

cd backend
make test
make lint
make format

cd ../frontend
pnpm test
pnpm check
```

Read [AGENTS.md](./AGENTS.md), [backend/AGENTS.md](./backend/AGENTS.md), and
[frontend/AGENTS.md](./frontend/AGENTS.md) before changing a module. Contributions
must include tests and keep user-facing and architecture documentation synchronized.

## Security

Do not commit `.env`, runtime configuration, `.deer-flow/`, credentials, database
exports, tenant data, support bundles, or private evaluation evidence. Review
[SECURITY.md](./SECURITY.md) before deploying or reporting a vulnerability.

## License And Attribution

Semantic DeerFlow is distributed under the [MIT License](./LICENSE). The original
ByteDance and DeerFlow Authors copyright notices are preserved. See
[ORIGINAL_README.md](./ORIGINAL_README.md) for upstream provenance and acknowledgments.
