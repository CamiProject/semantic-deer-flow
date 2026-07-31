from __future__ import annotations

import pytest

from app.semantic.audit import SemanticAuditRepository
from app.semantic.database import (
    create_semantic_engine,
    create_semantic_session_factory,
    initialize_semantic_database,
)
from app.semantic.request_context import SemanticRequestContext
from deerflow.runtime.authorization_context import AuthorizationContext


def _authorization():
    return AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": [],
            "permission_version": "1",
        }
    )


@pytest.mark.asyncio
async def test_semantic_audit_persists_correlation_but_drops_secrets_and_rows(tmp_path):
    engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}")
    await initialize_semantic_database(engine)
    repository = SemanticAuditRepository(create_semantic_session_factory(engine))
    context = SemanticRequestContext(
        run_id="run-1",
        thread_id="thread-1",
        tool_call_id="tool-1",
        semantic_trace_id="trace-1",
    )

    await repository.record(
        request_context=context,
        authorization=_authorization(),
        event_type="metric.query",
        decision="allow",
        details={
            "metric_ids": ["site.count"],
            "row_count": 1,
            "rows": [{"secret": "must-not-persist"}],
            "authorization_token": "must-not-persist",
            "sql": "must-not-persist",
            "source_refs": ["demo_sites"],
            "nested": [
                {
                    "name": "safe",
                    "password": "must-not-persist",
                    "child": {"token": "must-not-persist", "status": "ok"},
                }
            ],
        },
    )
    rows = await repository.list_for_trace("trace-1", authorization=_authorization())
    await engine.dispose()

    assert rows == [
        {
            "id": rows[0]["id"],
            "semantic_trace_id": "trace-1",
            "run_id": "run-1",
            "thread_id": "thread-1",
            "tool_call_id": "tool-1",
            "event_type": "metric.query",
            "decision": "allow",
            "details": {
                "metric_ids": ["site.count"],
                "row_count": 1,
                "source_refs": ["demo_sites"],
                "nested": [{"name": "safe", "child": {"status": "ok"}}],
            },
            "scope_hash": _authorization().scope_hash,
            "permission_version": "1",
            "created_at": rows[0]["created_at"],
        }
    ]


@pytest.mark.asyncio
async def test_semantic_audit_trace_read_is_scoped_to_full_authorization(tmp_path):
    engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}")
    await initialize_semantic_database(engine)
    repository = SemanticAuditRepository(create_semantic_session_factory(engine))
    authorization = _authorization()
    await repository.record(
        request_context=SemanticRequestContext(
            run_id="run-1",
            thread_id="thread-1",
            tool_call_id="tool-1",
            semantic_trace_id="trace-1",
        ),
        authorization=authorization,
        event_type="metric.query",
        decision="allow",
        details={"metric_ids": ["site.count"]},
    )
    changed_mapping = authorization.to_runtime_dict()
    changed_mapping.pop("scope_hash")
    changed_mapping["allowed_site_ids"] = ["site-demo-002"]
    changed_scope = AuthorizationContext.from_mapping(changed_mapping)

    rows = await repository.list_for_trace("trace-1", authorization=changed_scope)
    await engine.dispose()

    assert rows == []
