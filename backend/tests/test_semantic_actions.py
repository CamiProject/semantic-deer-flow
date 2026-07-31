from __future__ import annotations

import pytest
import pytest_asyncio

from app.semantic.actions import ActionError, ActionRepository
from app.semantic.database import create_semantic_engine, create_semantic_session_factory, initialize_semantic_database
from app.semantic.worker import ActionWorker
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.ontology import OntologyRegistry


def _ontology(*, action_version="1", with_compensation=False):
    action = {
        "version": action_version,
        "target_type": "Site",
        "scope_dimension": "site",
        "parameters": {"name": {"type": "string", "required": True}},
        "approval": {"required": True},
        "executor": {"type": "domain_api", "path": "/sites/{target_id}"},
    }
    if with_compensation:
        action["compensation"] = {
            "type": "domain_api",
            "method": "POST",
            "path": "/sites/{target_id}/compensate-rename",
        }
    return OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "properties": {"id": {"column": "id", "type": "string"}},
                }
            },
            "links": {},
            "metrics": {},
            "actions": {
                "site.rename": action,
            },
        }
    )


def _authorization(site_id="site-demo-001", permission_version="1"):
    return AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": ["site_admin"],
            "scope_mode": "resource_set",
            "allowed_site_ids": [site_id],
            "allowed_project_ids": [],
            "permission_version": permission_version,
        }
    )


def _request_context():
    return {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "tool_call_id": "tool-1",
        "semantic_trace_id": "trace-1",
    }


@pytest_asyncio.fixture()
async def repository(tmp_path):
    engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'semantic.db'}")
    await initialize_semantic_database(engine)
    repo = ActionRepository(create_semantic_session_factory(engine), _ontology())
    yield repo
    await engine.dispose()


@pytest.mark.asyncio
async def test_action_requires_scope_approval_and_is_idempotent(repository):
    authorization = _authorization()
    first = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason="rename",
        idempotency_key="key-1",
        expected_object_version="3",
        request_context=_request_context(),
    )
    second = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason="rename",
        idempotency_key="key-1",
        expected_object_version="3",
        request_context=_request_context(),
    )

    assert first["proposal_id"] == second["proposal_id"]
    assert first["status"] == "PROPOSED"
    with pytest.raises(ActionError) as preview_required:
        await repository.enqueue_execution(first["proposal_id"], authorization=authorization)
    assert preview_required.value.code == "ACTION_CONFLICT"

    preview = await repository.preview(
        first["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001", "version": "3"},
    )
    assert preview["status"] == "PENDING_APPROVAL"

    with pytest.raises(ActionError) as approval_required:
        await repository.enqueue_execution(first["proposal_id"], authorization=authorization)
    assert approval_required.value.code == "ACTION_APPROVAL_REQUIRED"

    approved = await repository.approve(first["proposal_id"], authorization=authorization, approved_by="public-user-001")
    execution = await repository.enqueue_execution(first["proposal_id"], authorization=authorization)
    duplicate = await repository.enqueue_execution(first["proposal_id"], authorization=authorization)

    assert approved["status"] == "READY"
    assert execution["execution_id"] == duplicate["execution_id"]
    assert await repository.list_transitions(
        entity_type="proposal",
        entity_id=first["proposal_id"],
    ) == [
        "PROPOSED",
        "VALIDATED",
        "PREVIEWED",
        "PENDING_APPROVAL",
        "READY",
    ]
    assert await repository.list_transitions(
        entity_type="execution",
        entity_id=execution["execution_id"],
    ) == ["READY"]


@pytest.mark.asyncio
async def test_action_idempotency_key_cannot_be_reused_for_different_payload(repository):
    authorization = _authorization()
    await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "First"},
        reason=None,
        idempotency_key="same-key",
        expected_object_version=None,
        request_context=_request_context(),
    )

    with pytest.raises(ActionError) as conflict:
        await repository.propose(
            authorization=authorization,
            action_id="site.rename",
            target_id="site-demo-001",
            parameters={"name": "Different"},
            reason=None,
            idempotency_key="same-key",
            expected_object_version=None,
            request_context=_request_context(),
        )

    assert conflict.value.code == "ACTION_CONFLICT"


@pytest.mark.asyncio
async def test_action_rejects_out_of_scope_and_scope_change(repository):
    with pytest.raises(ActionError) as denied:
        await repository.propose(
            authorization=_authorization(),
            action_id="site.rename",
            target_id="site-demo-002",
            parameters={"name": "New"},
            reason=None,
            idempotency_key="denied",
            expected_object_version=None,
            request_context=_request_context(),
        )
    assert denied.value.code == "AUTHORIZATION_DENIED"

    proposal = await repository.propose(
        authorization=_authorization(),
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="scope-change",
        expected_object_version=None,
        request_context=_request_context(),
    )
    with pytest.raises(ActionError) as changed:
        await repository.approve(
            proposal["proposal_id"],
            authorization=_authorization(permission_version="2"),
            approved_by="public-user-001",
        )
    assert changed.value.code == "SCOPE_CHANGED"


@pytest.mark.asyncio
async def test_worker_claim_and_finish_updates_execution(repository):
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker",
        expected_object_version=None,
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001"},
    )
    await repository.approve(proposal["proposal_id"], authorization=authorization, approved_by="public-user-001")
    execution = await repository.enqueue_execution(proposal["proposal_id"], authorization=authorization)

    claimed = await repository.claim_ready(worker_id="worker-1", lease_seconds=30)
    assert claimed is not None
    assert claimed[1]["execution_id"] == execution["execution_id"]

    await repository.finish(
        execution_id=execution["execution_id"],
        worker_id="worker-1",
        result={"updated": True},
    )
    completed = await repository.get_execution(execution["execution_id"], authorization=authorization)
    assert completed["status"] == "SUCCEEDED"
    assert completed["result"] == {"updated": True}
    assert (
        await repository.get_execution(
            execution["execution_id"],
            authorization=_authorization(permission_version="2"),
        )
        is None
    )
    assert await repository.list_transitions(
        entity_type="execution",
        entity_id=execution["execution_id"],
    ) == ["READY", "EXECUTING", "SUCCEEDED"]


class _RecordingExecutor:
    def __init__(self):
        self.proposals = []

    async def execute(self, proposal):
        self.proposals.append(proposal)
        return {"updated": proposal["target_id"]}


class _CompensatingExecutor:
    def __init__(self):
        self.compensations = []

    async def execute(self, proposal):
        raise RuntimeError("upstream response contained a sensitive diagnostic")

    async def compensate(self, proposal, compensation, *, error_code):
        self.compensations.append((proposal["proposal_id"], compensation["path"], error_code))
        return {"compensated": proposal["target_id"]}


class _StaticRevalidator:
    def __init__(self, authorization):
        self.authorization = authorization
        self.proposals = []

    async def revalidate(self, proposal):
        self.proposals.append(proposal)
        return self.authorization


class _StaticTargetReader:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or {"id": "site-demo-001"}
        self.calls = []

    async def get_target(self, *, action, target_id, authorization):
        self.calls.append((action.name, target_id, authorization.scope_hash))
        return self.snapshot


@pytest.mark.asyncio
async def test_action_worker_revalidates_action_version_before_execution(repository):
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker-version",
        expected_object_version=None,
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001"},
    )
    await repository.approve(proposal["proposal_id"], authorization=authorization, approved_by="public-user-001")
    execution = await repository.enqueue_execution(proposal["proposal_id"], authorization=authorization)
    executor = _RecordingExecutor()
    worker = ActionWorker(
        repository=repository,
        ontology=_ontology(action_version="2"),
        executor=executor,
        authorization_revalidator=_StaticRevalidator(authorization),
        target_reader=_StaticTargetReader(),
        worker_id="worker-version",
        lease_seconds=30,
    )

    assert await worker.run_once() is True
    completed = await repository.get_execution(execution["execution_id"], authorization=authorization)

    assert completed["status"] == "FAILED"
    assert completed["error_code"] == "ONTOLOGY_VERSION_CONFLICT"
    assert executor.proposals == []


@pytest.mark.asyncio
async def test_action_worker_fails_closed_when_current_scope_changed(repository):
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker-scope-change",
        expected_object_version=None,
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001"},
    )
    await repository.approve(proposal["proposal_id"], authorization=authorization, approved_by="public-user-001")
    execution = await repository.enqueue_execution(proposal["proposal_id"], authorization=authorization)
    executor = _RecordingExecutor()
    worker = ActionWorker(
        repository=repository,
        ontology=_ontology(),
        executor=executor,
        authorization_revalidator=_StaticRevalidator(_authorization(permission_version="2")),
        target_reader=_StaticTargetReader(),
        worker_id="worker-scope-change",
        lease_seconds=30,
    )

    assert await worker.run_once() is True
    completed = await repository.get_execution(execution["execution_id"], authorization=authorization)

    assert completed["status"] == "FAILED"
    assert completed["error_code"] == "SCOPE_CHANGED"
    assert executor.proposals == []


@pytest.mark.asyncio
async def test_action_worker_rechecks_target_version_before_write(repository):
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker-object-change",
        expected_object_version="3",
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001", "version": "3"},
    )
    await repository.approve(proposal["proposal_id"], authorization=authorization, approved_by="public-user-001")
    execution = await repository.enqueue_execution(proposal["proposal_id"], authorization=authorization)
    executor = _RecordingExecutor()
    worker = ActionWorker(
        repository=repository,
        ontology=_ontology(),
        executor=executor,
        authorization_revalidator=_StaticRevalidator(authorization),
        target_reader=_StaticTargetReader({"id": "site-demo-001", "version": "4"}),
        worker_id="worker-object-change",
        lease_seconds=30,
    )

    assert await worker.run_once() is True
    completed = await repository.get_execution(execution["execution_id"], authorization=authorization)

    assert completed["status"] == "FAILED"
    assert completed["error_code"] == "ACTION_CONFLICT"
    assert executor.proposals == []


@pytest.mark.asyncio
async def test_action_worker_runs_only_declared_compensation(tmp_path):
    ontology = _ontology(with_compensation=True)
    engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'compensation.db'}")
    await initialize_semantic_database(engine)
    repository = ActionRepository(create_semantic_session_factory(engine), ontology)
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker-compensation",
        expected_object_version=None,
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001"},
    )
    await repository.approve(
        proposal["proposal_id"],
        authorization=authorization,
        approved_by="public-user-001",
    )
    execution = await repository.enqueue_execution(
        proposal["proposal_id"],
        authorization=authorization,
    )
    executor = _CompensatingExecutor()
    worker = ActionWorker(
        repository=repository,
        ontology=ontology,
        executor=executor,
        authorization_revalidator=_StaticRevalidator(authorization),
        target_reader=_StaticTargetReader(),
        worker_id="worker-compensation",
        lease_seconds=30,
    )

    try:
        assert await worker.run_once() is True
        completed = await repository.get_execution(
            execution["execution_id"],
            authorization=authorization,
        )

        assert completed["status"] == "COMPENSATED"
        assert completed["error_code"] == "EXECUTION_FAILED"
        assert completed["error_detail"] == "Action execution failed before compensation"
        assert completed["result"] == {"compensated": "site-demo-001"}
        assert executor.compensations == [
            (
                proposal["proposal_id"],
                "/sites/{target_id}/compensate-rename",
                "EXECUTION_FAILED",
            )
        ]
        assert await repository.list_transitions(
            entity_type="execution",
            entity_id=execution["execution_id"],
        ) == ["READY", "EXECUTING", "COMPENSATING", "COMPENSATED"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_compensation_lease_can_be_recovered_without_replaying_action(tmp_path):
    ontology = _ontology(with_compensation=True)
    engine = create_semantic_engine(f"sqlite+aiosqlite:///{tmp_path / 'compensation-recovery.db'}")
    await initialize_semantic_database(engine)
    repository = ActionRepository(create_semantic_session_factory(engine), ontology)
    authorization = _authorization()
    proposal = await repository.propose(
        authorization=authorization,
        action_id="site.rename",
        target_id="site-demo-001",
        parameters={"name": "New"},
        reason=None,
        idempotency_key="worker-compensation-recovery",
        expected_object_version=None,
        request_context=_request_context(),
    )
    await repository.preview(
        proposal["proposal_id"],
        authorization=authorization,
        target_snapshot={"id": "site-demo-001"},
    )
    await repository.approve(
        proposal["proposal_id"],
        authorization=authorization,
        approved_by="public-user-001",
    )
    execution = await repository.enqueue_execution(
        proposal["proposal_id"],
        authorization=authorization,
    )

    try:
        claimed = await repository.claim_ready(worker_id="worker-crashed", lease_seconds=-1)
        assert claimed is not None
        await repository.begin_compensation(
            execution_id=execution["execution_id"],
            worker_id="worker-crashed",
            error_code="EXECUTION_FAILED",
            error_detail="Action execution failed before compensation",
        )

        executor = _CompensatingExecutor()
        worker = ActionWorker(
            repository=repository,
            ontology=ontology,
            executor=executor,
            authorization_revalidator=_StaticRevalidator(authorization),
            target_reader=_StaticTargetReader(),
            worker_id="worker-recovery",
            lease_seconds=30,
        )

        assert await worker.run_once() is True
        completed = await repository.get_execution(
            execution["execution_id"],
            authorization=authorization,
        )
        assert completed["status"] == "COMPENSATED"
        assert executor.compensations == [
            (
                proposal["proposal_id"],
                "/sites/{target_id}/compensate-rename",
                "EXECUTION_FAILED",
            )
        ]
    finally:
        await engine.dispose()
