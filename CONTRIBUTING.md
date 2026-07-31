# Contributing To Semantic DeerFlow

Semantic DeerFlow is an unofficial downstream of ByteDance DeerFlow. Contributions to
this repository are reviewed by the downstream maintainers and are not contributions
to, or endorsements by, ByteDance or the official DeerFlow project.

Before release, replace `<your-github-account>` in documentation with the final public
owner. Downstream issues belong at:

<https://github.com/<your-github-account>/semantic-deer-flow/issues>

Use the [upstream repository](https://github.com/bytedance/deer-flow) only for issues
that reproduce on unmodified upstream DeerFlow.

## Development Setup

```bash
cp .env.example .env
cp config.example.yaml config.yaml
cp extensions_config.example.json extensions_config.json
make doctor
make install
make dev
```

The frontend at <http://localhost:2026> is a development and debugging interface. The
Gateway, Semantic, and Action APIs are the primary downstream integration surfaces.

## Change Requirements

- Read the root and relevant module `AGENTS.md` before editing.
- Add tests for every feature and bug fix. Backend TDD is mandatory.
- Keep `README.md` and the relevant `AGENTS.md` synchronized with behavior.
- Preserve `deerflow.*`, `DEER_FLOW_*`, existing APIs, core data structures, and Docker
  compatibility identifiers unless a reviewed compatibility change explicitly says
  otherwise.
- Do not replace DeerFlow identifiers across the repository indiscriminately.
- Keep Logo and favicon changes out of scope until the product frontend is designed.

## Public Data Rules

Examples and tests must use obvious synthetic identities such as
`public-tenant-001`, `public_demo`, `public-user-001`, `site-demo-001`,
`project-demo-001`, `demo`, `semantic_demo`, `demo_sites`, and `demo_projects`.

Never submit real tenant, user, project, site, database, table, hostname, IP address,
token, key, DSN, IAM/Domain contract, customer prompt, local note, acceptance report,
or architecture record. Do not hash real names and publish the hashes; replace them
with readable fictional examples.

Do not commit `.env`, `config.yaml`, `extensions_config.json`, `.deer-flow/`,
`backend/.deer-flow/`, `.local-notes/`, logs, temp outputs, private keys,
credentials, database exports, support bundles, or real test evidence.

## Verification

```bash
cd backend
uv run ruff check app packages/harness/deerflow tests
uv run ruff format --check app packages/harness/deerflow tests
uv run pytest tests/test_<changed_area>.py -v

cd ../frontend
pnpm test
pnpm check

cd ..
git diff --check
```

Run `make eval-smoke` when the isolated public Fake environment and a test model are
configured. Review the final diff and repeat the sensitive-data scan before asking a
maintainer to commit or publish.

## Pull Requests

Describe the behavior, compatibility impact, tests run, and any intentionally retained
scan hits. Never attach private logs or data. Security-sensitive findings must follow
[SECURITY.md](./SECURITY.md), not a public issue or pull request.

