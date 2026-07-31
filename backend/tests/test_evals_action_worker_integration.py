from __future__ import annotations

import httpx
import pytest

from app.evals.fixture_api import EvalFixtureSettings, create_fixture_app
from app.semantic.actions import ActionRepository
from app.semantic.database import (
    create_semantic_engine,
    create_semantic_session_factory,
    initialize_semantic_database,
)
from app.semantic.worker import (
    ActionWorker,
    DomainApiActionExecutor,
    SaasAuthorizationRevalidator,
)
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import OntologyRegistry

JWT_KEY = "eval-jwt-secret-at-least-thirty-two-bytes"


def _ontology() -> OntologyRegistry:
    return OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "properties": {
                        "id": {"column": "id", "type": "string"},
                        "display_name": {"column": "display_name", "type": "string"},
                        "version": {"column": "version", "type": "string"},
                    },
                }
            },
            "links": {},
            "metrics": {},
            "actions": {
                "site.update_display_name": {
                    "version": "1",
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "parameters": {"name": {"type": "string", "required": True}},
                    "approval": {"required": True},
                    "authorization": {"allowed_roles": ["site_admin"]},
                    "executor": {
                        "type": "domain_api",
                        "method": "PATCH",
                        "path": "/api/internal/sites/{target_id}/display-name",
                        "result_fields": ["updated", "version"],
                    },
                }
            },
        }
    )


def _authorization() -> AuthorizationContext:
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


class _TargetReader:
    async def get_target(self, *, action, target_id, authorization):
        return {"id": target_id, "display_name": "Old Display Name", "version": "1"}


async def _reset_fixture(client: httpx.AsyncClient, authorization: AuthorizationContext, *, scenario: str):
    response = await client.post(
        "/v1/evals/trials/trial-1/reset",
        headers={"X-Evals-Token": "fixture-control-secret"},
        json={
            "eval_run_id": "eval-run-1",
            "trial_id": "trial-1",
            "thread_id": "thread-1",
            "case_id": "action-site-rename",
            "scenario": scenario,
            "tenant_id": authorization.tenant_id,
            "tenant_code": authorization.tenant_code,
            "tenant_name": "Eval Tenant",
            "principal_id": authorization.principal_id,
            "system_code": authorization.system_code,
            "permission_version": authorization.permission_version,
            "role_codes": list(authorization.role_codes),
            "scope": {
                "mode": authorization.scope_mode,
                "site_ids": list(authorization.allowed_site_ids),
                "project_ids": list(authorization.allowed_project_ids),
            },
            "scope_hash": authorization.scope_hash,
        },
    )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expected_status", "expected_name"),
    [
        ("site_display_name_old", "SUCCEEDED", "New Display Name"),
        ("action_iam_denied", "FAILED", "Old Display Name"),
    ],
)
async def test_real_action_worker_chain_uses_isolated_fixture(
    monkeypatch,
    tmp_path,
    scenario,
    expected_status,
    expected_name,
):
    monkeypatch.setenv("DEER_FLOW_ENV", "eval")
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_KEY", JWT_KEY)
    monkeypatch.setenv("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_KEY", JWT_KEY)
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ALGORITHMS", "HS256")
    monkeypatch.setenv("SAAS_AUTHORIZATION_JWT_ISSUER", "saas-gateway")
    fixture_app = create_fixture_app(
        settings=EvalFixtureSettings(
            environment="eval",
            control_token="fixture-control-secret",
            saas_internal_token="fixture-worker-secret",
            authorization_jwt_key=JWT_KEY,
        )
    )
    transport = httpx.ASGITransport(app=fixture_app)
    authorization = _authorization()
    async with httpx.AsyncClient(transport=transport, base_url="http://eval-fixture") as fixture_client:
        await _reset_fixture(fixture_client, authorization, scenario=scenario)

        engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}")
        await initialize_semantic_database(engine)
        repository = ActionRepository(create_semantic_session_factory(engine), _ontology())
        proposal = await repository.propose(
            authorization=authorization,
            action_id="site.update_display_name",
            target_id="site-demo-001",
            parameters={"name": "New Display Name"},
            reason="Eval write",
            idempotency_key="eval-idempotency-1",
            expected_object_version="1",
            request_context={
                "run_id": "run-1",
                "thread_id": "thread-1",
                "tool_call_id": "tool-1",
                "semantic_trace_id": "trace-1",
            },
        )
        await repository.preview(
            proposal["proposal_id"],
            authorization=authorization,
            target_snapshot={"id": "site-demo-001", "display_name": "Old Display Name", "version": "1"},
        )
        await repository.approve(
            proposal["proposal_id"],
            authorization=authorization,
            approved_by="eval-runner",
        )
        execution = await repository.enqueue_execution(
            proposal["proposal_id"],
            authorization=authorization,
        )
        worker = ActionWorker(
            repository=repository,
            ontology=_ontology(),
            executor=DomainApiActionExecutor(
                base_url="http://eval-fixture",
                service_token="fixture-worker-secret",
                transport=transport,
            ),
            authorization_revalidator=SaasAuthorizationRevalidator(
                url="http://eval-fixture/api/authorization/actions/revalidate",
                service_token="fixture-worker-secret",
                audience="action-worker",
                transport=transport,
            ),
            target_reader=_TargetReader(),
            worker_id="eval-worker-1",
            lease_seconds=30,
        )

        assert await worker.run_once() is True
        final = await repository.get_execution(
            execution["execution_id"],
            authorization=authorization,
        )
        fixture_state = await fixture_client.get(
            "/v1/evals/trials/trial-1/state",
            headers={"X-Evals-Token": "fixture-control-secret"},
        )
        await engine.dispose()

    assert final is not None
    assert final["status"] == expected_status, (final.get("error_code"), final.get("error_detail"), final)
    assert fixture_state.json()["state"]["sites"]["site-demo-001"]["display_name"] == expected_name
