---
name: mysql-query
description: Query a configured MySQL data source for read-only analysis, reporting, and schema exploration. Use SQL discovery and validation tools, honor tenant Scope when trusted SaaS context is present, and never invent database or table names.
---

# MySQL Query

Use this skill for read-only MySQL exploration and reporting. Delegate database work to a general-purpose subagent with the SQL tools, and keep the query within the data source and Scope supplied by the runtime.

## Configuration

For local development, configure an explicitly public or local database:

```bash
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your-readonly-user
MYSQL_PASSWORD=your-readonly-password
MYSQL_DATABASE=semantic_demo
```

For trusted SaaS requests, the runtime supplies signed tenant and permission context. The data-source registry connection uses `SAAS_CONFIG_DB_*` environment variables. Registry table and column names are configurable with `SAAS_CONFIG_DB_TABLE` and the related `SAAS_CONFIG_DB_*_COLUMN` variables. Never ask a user to provide or override tenant IDs, data-source URLs, passwords, internal tokens, or signed Scope claims.

The committed public examples use these clearly synthetic values:

| Field | Public example |
| --- | --- |
| Tenant | `public-tenant-001` / `public_demo` |
| Principal | `public-user-001` |
| System | `demo` |
| Database | `semantic_demo` |
| Tables | `demo_sites`, `demo_projects` |

## Workflow

1. Identify the requested metric, dimensions, filters, and time range.
2. If the schema is unknown, use `sql_show_databases`, `sql_list_tables`, and `sql_schema` once each as needed.
3. Generate one bounded `SELECT` query. Use `sql_query_checker` for complex joins or aggregations.
4. Execute with `sql_query`, then present the result and any important limitations.
5. On an error, make at most one evidence-based correction. Otherwise report the error without guessing alternate tenant databases, tables, or columns.

## Delegation Example

```json
{
  "description": "Count visible demo projects",
  "prompt": "In the configured public demo data source, count projects grouped by site. First inspect demo_projects if its schema is not already known, then execute one bounded SELECT query. Honor the signed tenant Scope and do not try alternate databases.",
  "subagent_type": "general-purpose"
}
```

## Public SQL Examples

Inspect a schema before assuming columns:

```text
sql_schema(table_names="demo_sites,demo_projects")
```

An illustrative query for the committed public schema is:

```sql
SELECT
  s.id AS site_id,
  s.name AS site_name,
  COUNT(p.id) AS project_count
FROM semantic_demo.demo_sites AS s
LEFT JOIN semantic_demo.demo_projects AS p ON p.site_id = s.id
GROUP BY s.id, s.name
ORDER BY s.id
LIMIT 100;
```

Runtime Scope enforcement may add predicates and bound parameters to this query. Do not bypass or duplicate those controls.

## Safety

- Only `SELECT`, `SHOW`, and `DESCRIBE` operations are allowed by default.
- Do not enable writes for a user request or place write SQL in examples.
- Do not enumerate unrelated databases or schemas.
- Never reveal connection strings, credentials, hidden table names, or out-of-scope object IDs.
- Use bounded result sets and aggregate sensitive measures where the policy requires it.
- Treat the Ontology and SQL Scope Policy as authoritative when they are available.
