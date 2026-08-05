# Upstream Source And Attribution

Semantic DeerFlow is an unofficial downstream of
[ByteDance DeerFlow](https://github.com/bytedance/deer-flow). ByteDance and the
official DeerFlow maintainers do not maintain, sponsor, or endorse this downstream.

The workspace was prepared from an accepted source snapshot without importing
the private fork's Git history. The exact accepted snapshot commit was not present in
this archive and must be recorded here by the release owner before release:

```text
Accepted source snapshot: <commit-to-be-confirmed>
```

The upstream project remains the authoritative source for DeerFlow itself:

- Source: <https://github.com/bytedance/deer-flow>
- Upstream documentation: <https://github.com/bytedance/deer-flow/tree/main/docs>
- Upstream license: <https://github.com/bytedance/deer-flow/blob/main/LICENSE>

## Downstream Scope

Semantic DeerFlow keeps the upstream super-agent harness and adds a multi-service
SaaS semantic layer: signed authorization context, Ontology and metric resolution,
SQL Scope Policy, semantic-first query routes, approval-gated Actions, an isolated
Action Worker, Fake IAM/Domain services, SQLite demo data, and offline Evals. The
Gateway remains the primary integration surface; the included frontend is intended
for development and debugging.

Start with [README.md](./README.md) or [README_zh.md](./README_zh.md) for features,
architecture, configuration, startup modes, API examples, Python Client/TUI usage,
scheduled tasks, channels, and the Evals workflow.

This repository preserves the upstream `deerflow.*` package namespace,
`DEER_FLOW_*` environment variables, APIs, data structures, and internal Docker
compatibility identifiers so downstream integrations remain compatible. Project
identity, SaaS semantic governance, Fake services, and downstream release
support belong to Semantic DeerFlow.

The MIT License in this repository retains the original ByteDance and DeerFlow
Authors copyright notices. Thanks to the upstream maintainers and contributors whose
work provides the agent harness on which this downstream is built.
