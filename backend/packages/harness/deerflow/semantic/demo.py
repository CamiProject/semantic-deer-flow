"""Public in-memory data source for local demos and isolated Evals."""

from __future__ import annotations

import os
from functools import lru_cache

from langchain_community.utilities import SQLDatabase
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from deerflow.runtime.tenant_context import TenantContext
from deerflow.semantic.ontology import OntologyRegistry
from deerflow.semantic.runtime import SemanticQueryRuntime
from deerflow.semantic.sql_scope import SqlScopePolicyRegistry
from deerflow.tools.builtins.tenant_datasource import TenantDataSource, TenantDataSourceError

PUBLIC_TENANT_ID = "public-tenant-001"
PUBLIC_TENANT_CODE = "public_demo"
PUBLIC_PRINCIPAL_ID = "public-user-001"
PUBLIC_SYSTEM_CODE = "demo"
PUBLIC_DATABASE = "semantic_demo"


def public_demo_enabled() -> bool:
    return os.getenv("DEER_FLOW_SEMANTIC_DEMO_DATA", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def resolve_public_demo_datasource(context: TenantContext) -> TenantDataSource:
    if context.tenant_id != PUBLIC_TENANT_ID or context.tenant_code != PUBLIC_TENANT_CODE or context.system_code != PUBLIC_SYSTEM_CODE:
        raise TenantDataSourceError("Public demo mode only accepts the committed synthetic tenant context")
    return TenantDataSource(
        code=PUBLIC_DATABASE,
        host="localhost",
        port=0,
        database=PUBLIC_DATABASE,
        username="public_demo",
        password="",
        driver_class="sqlite",
        allowed_databases=(PUBLIC_DATABASE,),
    )


@lru_cache(maxsize=1)
def get_public_demo_database() -> SQLDatabase:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE demo_sites (id TEXT PRIMARY KEY, name TEXT NOT NULL)"))
        connection.execute(text("CREATE TABLE demo_projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, site_id TEXT NOT NULL)"))
        connection.execute(
            text("INSERT INTO demo_sites (id, name) VALUES (:id, :name)"),
            [
                {"id": "site-demo-001", "name": "Public Demo Site A"},
                {"id": "site-demo-002", "name": "Public Demo Site B"},
                {"id": "site-demo-003", "name": "Public Demo Site C"},
            ],
        )
        connection.execute(
            text("INSERT INTO demo_projects (id, name, site_id) VALUES (:id, :name, :site_id)"),
            [
                {
                    "id": "project-demo-001",
                    "name": "Public Demo Project A",
                    "site_id": "site-demo-001",
                },
                {
                    "id": "project-demo-002",
                    "name": "Public Demo Project B",
                    "site_id": "site-demo-002",
                },
            ],
        )
    return SQLDatabase(engine, sample_rows_in_table_info=0)


def create_public_demo_runtime(
    *,
    ontology: OntologyRegistry,
    sql_policy: SqlScopePolicyRegistry,
) -> SemanticQueryRuntime:
    database = get_public_demo_database()
    return SemanticQueryRuntime(
        ontology=ontology,
        sql_policy=sql_policy,
        datasource_resolver=resolve_public_demo_datasource,
        database_factory=lambda _uri: database,
    )
