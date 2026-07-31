# Security Policy

## Scope

Security reports for this downstream belong to the Semantic DeerFlow public
repository, not to ByteDance or the official DeerFlow project. Upstream-only issues
should be reported through the upstream project's policy.

Before public release, replace `<your-github-account>` below with the final repository
owner:

<https://github.com/<your-github-account>/semantic-deer-flow/security/advisories/new>

If private advisories are unavailable, contact the repository owner privately. Do not
open a public issue containing credentials, tenant data, authorization tokens, exploit
details, or unredacted logs.

## Supported Versions

Until the first Semantic DeerFlow release is published, only the latest public `main`
revision is eligible for security fixes. This does not imply support for the official
DeerFlow branches or releases.

## Deployment Boundary

- Expose nginx/Gateway only; keep Semantic API and Action Worker internal.
- Give write credentials only to Action Worker.
- Use short-lived signed authorization context and validate issuer, audience, Scope,
  permission version, approval, and idempotency.
- Set `DEER_FLOW_SEMANTIC_DEMO_DATA=false` before a real SaaS deployment.
- Never reuse eval secrets in production.
- Do not commit `.env`, runtime configs, private keys, credentials, database exports,
  tenant data, support bundles, or private evaluation reports.

