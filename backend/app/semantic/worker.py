"""Isolated Action worker; the only semantic component with write credentials."""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.auth.saas_authorization import SaasAuthorizationError, decode_authorization_token
from app.semantic.actions import (
    ActionError,
    ActionRepository,
    validate_action_target,
)
from app.semantic.config import get_semantic_settings
from app.semantic.database import (
    create_semantic_engine,
    create_semantic_session_factory,
    initialize_semantic_database,
)
from deerflow.runtime.authorization_context import AuthorizationContext, AuthorizationContextError
from deerflow.semantic.demo import create_public_demo_runtime, public_demo_enabled
from deerflow.semantic.ontology import ActionDefinition, OntologyError, OntologyRegistry, get_ontology_registry
from deerflow.semantic.runtime import SemanticQueryRuntime
from deerflow.semantic.sql_scope import get_sql_scope_policy_registry

_MAX_ACTION_RESPONSE_BYTES = 1_000_000
_MAX_ACTION_RESULT_DEPTH = 4
_SENSITIVE_RESULT_KEY_FRAGMENTS = (
    "authorization",
    "connection",
    "credential",
    "jdbc",
    "password",
    "secret",
    "sql",
    "token",
    "url",
)


def _bounded_action_result(value: Any, *, depth: int = 0) -> Any:
    if depth >= _MAX_ACTION_RESULT_DEPTH:
        return "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in list(value.items())[:50]:
            key = str(raw_key)[:128]
            if any(fragment in key.lower() for fragment in _SENSITIVE_RESULT_KEY_FRAGMENTS):
                continue
            result[key] = _bounded_action_result(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_action_result(item, depth=depth + 1) for item in value[:100]]
    return str(value)[:2000]


class ActionExecutor(Protocol):
    async def execute(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def compensate(
        self,
        proposal: Mapping[str, Any],
        compensation: Mapping[str, Any],
        *,
        error_code: str,
    ) -> Mapping[str, Any]: ...


class ActionAuthorizationRevalidator(Protocol):
    async def revalidate(self, proposal: Mapping[str, Any]) -> AuthorizationContext: ...


class ActionTargetReader(Protocol):
    async def get_target(
        self,
        *,
        action: ActionDefinition,
        target_id: str,
        authorization: AuthorizationContext,
    ) -> Mapping[str, Any]: ...


class SaasAuthorizationRevalidator:
    """Obtain and verify fresh Action Worker authorization from SaaS IAM."""

    def __init__(
        self,
        *,
        url: str,
        service_token: str,
        audience: str = "action-worker",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url.strip()
        self._service_token = service_token.strip()
        self._audience = audience
        self._transport = transport

    async def revalidate(self, proposal: Mapping[str, Any]) -> AuthorizationContext:
        parsed = urlsplit(self._url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or not self._service_token:
            raise ActionError(
                "POLICY_UNAVAILABLE",
                "Action authorization revalidation is not configured",
            )
        body = {
            "principal_id": proposal["authorization_snapshot"]["principal_id"],
            "tenant_id": proposal["authorization_snapshot"]["tenant_id"],
            "system_code": proposal["authorization_snapshot"]["system_code"],
            "permission_version": proposal["permission_version"],
            "scope_hash": proposal["scope_hash"],
            "action_id": proposal["action_id"],
            "action_version": proposal["action_version"],
            "target_type": proposal["target_type"],
            "target_id": proposal["target_id"],
        }
        try:
            async with httpx.AsyncClient(
                timeout=15,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.post(
                    self._url,
                    headers={
                        "X-SaaS-Internal-Token": self._service_token,
                        "X-DeerFlow-Run-Id": str(proposal["run_id"]),
                        "X-DeerFlow-Thread-Id": str(proposal["thread_id"]),
                        "X-DeerFlow-Semantic-Trace-Id": str(proposal["semantic_trace_id"]),
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ActionError(
                "POLICY_UNAVAILABLE",
                "Action authorization service is unavailable",
            ) from exc
        if response.status_code >= 500:
            raise ActionError(
                "POLICY_UNAVAILABLE",
                "Action authorization service is unavailable",
            )
        if response.is_error:
            raise ActionError(
                "AUTHORIZATION_DENIED",
                "Action authorization was denied",
                status_code=403,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActionError(
                "POLICY_UNAVAILABLE",
                "Action authorization service returned an invalid response",
            ) from exc
        token = payload.get("authorization_token") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise ActionError(
                "POLICY_UNAVAILABLE",
                "Action authorization service returned no authorization token",
            )
        try:
            authorization, _tenant_name = decode_authorization_token(
                token,
                audience=self._audience,
            )
        except SaasAuthorizationError as exc:
            raise ActionError(
                "AUTHORIZATION_DENIED",
                "Fresh Action authorization token is invalid",
                status_code=403,
            ) from exc
        return authorization


class SemanticTargetReader:
    def __init__(self, runtime: SemanticQueryRuntime) -> None:
        self._runtime = runtime

    async def get_target(
        self,
        *,
        action: ActionDefinition,
        target_id: str,
        authorization: AuthorizationContext,
    ) -> Mapping[str, Any]:
        result = await asyncio.to_thread(
            self._runtime.get_object,
            authorization=authorization,
            object_type=action.target_type,
            object_id=target_id,
        )
        if not result.rows:
            raise ActionError(
                "AUTHORIZATION_DENIED",
                "Action target is not visible in the current scope",
                status_code=403,
            )
        return result.rows[0]


class DomainApiActionExecutor:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._transport = transport

    async def execute(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._invoke(
            proposal,
            proposal.get("executor") or {},
            idempotency_key=str(proposal["idempotency_key"]),
        )

    async def compensate(
        self,
        proposal: Mapping[str, Any],
        compensation: Mapping[str, Any],
        *,
        error_code: str,
    ) -> Mapping[str, Any]:
        compensation_key = hashlib.sha256(f"{proposal['idempotency_key']}\0compensation".encode()).hexdigest()
        return await self._invoke(
            proposal,
            compensation,
            idempotency_key=compensation_key,
            extra_payload={
                "compensation": True,
                "compensation_for_error": error_code,
            },
        )

    async def _invoke(
        self,
        proposal: Mapping[str, Any],
        executor: Mapping[str, Any],
        *,
        idempotency_key: str,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        parsed = urlsplit(self._base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ActionError("EXECUTION_FAILED", "Action domain API is not configured")
        if executor.get("type") != "domain_api":
            raise ActionError("EXECUTION_FAILED", "Unsupported Action executor")
        method = str(executor.get("method") or "POST").upper()
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            raise ActionError("EXECUTION_FAILED", "Unsupported domain API method")
        path_template = str(executor.get("path") or "")
        if (
            not path_template.startswith("/")
            or path_template.startswith("//")
            or ".." in path_template
            or "?" in path_template
            or "#" in path_template
            or "\\" in path_template
            or path_template.count("{target_id}") != 1
            or path_template.replace("{target_id}", "").find("{") >= 0
            or path_template.replace("{target_id}", "").find("}") >= 0
        ):
            raise ActionError("EXECUTION_FAILED", "Unsafe domain API path")
        path = path_template.format(target_id=quote(str(proposal["target_id"]), safe=""))
        headers = {
            "Idempotency-Key": idempotency_key,
            "X-DeerFlow-Run-Id": proposal["run_id"],
            "X-DeerFlow-Thread-Id": proposal["thread_id"],
            "X-DeerFlow-Tool-Call-Id": proposal["tool_call_id"],
            "X-DeerFlow-Semantic-Trace-Id": proposal["semantic_trace_id"],
        }
        if proposal.get("expected_object_version"):
            headers["If-Match"] = str(proposal["expected_object_version"])
        if self._service_token:
            headers["X-SaaS-Internal-Token"] = self._service_token
        payload = {
            "target_type": proposal["target_type"],
            "target_id": proposal["target_id"],
            "parameters": proposal["parameters"],
            "reason": proposal.get("reason"),
            "expected_object_version": proposal.get("expected_object_version"),
            "actor": proposal["authorization_snapshot"]["principal_id"],
            "scope_hash": proposal["scope_hash"],
            "action_id": proposal["action_id"],
            "action_version": proposal["action_version"],
            **dict(extra_payload or {}),
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=30,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.request(method, path, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ActionError("EXECUTION_FAILED", "Action domain API is unavailable") from exc
        if response.is_error:
            raise ActionError("EXECUTION_FAILED", "Action domain API rejected the request")
        if not response.content:
            return {"status_code": response.status_code}
        if len(response.content) > _MAX_ACTION_RESPONSE_BYTES:
            raise ActionError("EXECUTION_FAILED", "Action domain API response exceeds the allowed size")
        try:
            data = response.json()
        except ValueError as exc:
            raise ActionError("EXECUTION_FAILED", "Action domain API returned an invalid response") from exc
        result_fields = executor.get("result_fields") or []
        if not isinstance(result_fields, list):
            raise ActionError("EXECUTION_FAILED", "Action result policy is invalid")
        if not isinstance(data, Mapping) or not result_fields:
            return {"status_code": response.status_code}
        projected = {field: _bounded_action_result(data[field]) for field in result_fields if isinstance(field, str) and field in data}
        return projected or {"status_code": response.status_code}


class ActionWorker:
    def __init__(
        self,
        *,
        repository: ActionRepository,
        ontology: OntologyRegistry,
        executor: ActionExecutor,
        authorization_revalidator: ActionAuthorizationRevalidator,
        target_reader: ActionTargetReader,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        self._repository = repository
        self._ontology = ontology
        self._executor = executor
        self._authorization_revalidator = authorization_revalidator
        self._target_reader = target_reader
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    async def run_once(self) -> bool:
        claimed = await self._repository.claim_ready(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return False
        proposal, execution = claimed
        try:
            stored_authorization = AuthorizationContext.from_mapping(proposal["authorization_snapshot"])
            action = self._ontology.action(proposal["action_id"])
            if action.version != proposal["action_version"]:
                raise ActionError("ONTOLOGY_VERSION_CONFLICT", "Action definition changed")
            action.validate_parameters(proposal["parameters"])
            if stored_authorization.scope_hash != proposal["scope_hash"]:
                raise ActionError("SCOPE_CHANGED", "Stored scope snapshot is invalid")
            authorization = await self._authorization_revalidator.revalidate(proposal)
            if (
                authorization.principal_id != stored_authorization.principal_id
                or authorization.tenant_id != stored_authorization.tenant_id
                or authorization.tenant_code != stored_authorization.tenant_code
                or authorization.system_code != stored_authorization.system_code
                or authorization.scope_hash != proposal["scope_hash"]
                or authorization.permission_version != proposal["permission_version"]
            ):
                raise ActionError("SCOPE_CHANGED", "Action authorization scope changed")
            try:
                action = self._ontology.authorize_action(proposal["action_id"], authorization)
            except OntologyError as exc:
                raise ActionError(
                    "AUTHORIZATION_DENIED",
                    str(exc),
                    status_code=403,
                ) from exc
            if authorization.scope_mode != "tenant_all":
                resources = authorization.allowed_site_ids if action.scope_dimension == "site" else authorization.allowed_project_ids
                if proposal["target_id"] not in resources:
                    raise ActionError("AUTHORIZATION_DENIED", "Action target is outside the stored scope")
            target_snapshot = await self._target_reader.get_target(
                action=action,
                target_id=proposal["target_id"],
                authorization=authorization,
            )
            target_type = self._ontology.object(action.target_type)
            validate_action_target(
                action=action,
                target_id=proposal["target_id"],
                target_id_field=target_type.id_field,
                expected_object_version=proposal.get("expected_object_version"),
                target_snapshot=target_snapshot,
            )
            if execution["status"] == "COMPENSATING":
                if action.compensation is None:
                    await self._repository.finish_compensation(
                        execution_id=execution["execution_id"],
                        worker_id=self._worker_id,
                        compensation_error=True,
                    )
                    return True
                try:
                    compensation_result = await self._executor.compensate(
                        proposal,
                        action.compensation,
                        error_code=str(execution.get("error_code") or "EXECUTION_FAILED"),
                    )
                except Exception:
                    await self._repository.finish_compensation(
                        execution_id=execution["execution_id"],
                        worker_id=self._worker_id,
                        compensation_error=True,
                    )
                else:
                    await self._repository.finish_compensation(
                        execution_id=execution["execution_id"],
                        worker_id=self._worker_id,
                        result=compensation_result,
                    )
                return True
            try:
                result = await self._executor.execute(proposal)
            except Exception as execution_error:
                if action.compensation is None:
                    raise
                error_code = execution_error.code if isinstance(execution_error, ActionError) else "EXECUTION_FAILED"
                error_detail = execution_error.detail if isinstance(execution_error, ActionError) else "Action execution failed before compensation"
                await self._repository.begin_compensation(
                    execution_id=execution["execution_id"],
                    worker_id=self._worker_id,
                    error_code=error_code,
                    error_detail=error_detail,
                )
                try:
                    compensation_result = await self._executor.compensate(
                        proposal,
                        action.compensation,
                        error_code=error_code,
                    )
                except Exception:
                    await self._repository.finish_compensation(
                        execution_id=execution["execution_id"],
                        worker_id=self._worker_id,
                        compensation_error=True,
                    )
                else:
                    await self._repository.finish_compensation(
                        execution_id=execution["execution_id"],
                        worker_id=self._worker_id,
                        result=compensation_result,
                    )
                return True
            await self._repository.finish(
                execution_id=execution["execution_id"],
                worker_id=self._worker_id,
                result=result,
            )
        except (ActionError, AuthorizationContextError) as exc:
            code = exc.code if isinstance(exc, ActionError) else "AUTHORIZATION_DENIED"
            await self._repository.finish(
                execution_id=execution["execution_id"],
                worker_id=self._worker_id,
                error_code=code,
                error_detail=str(exc),
            )
        except Exception:
            await self._repository.finish(
                execution_id=execution["execution_id"],
                worker_id=self._worker_id,
                error_code="EXECUTION_FAILED",
                error_detail="Action execution failed",
            )
        return True


async def run_worker() -> None:
    settings = get_semantic_settings()
    ontology = get_ontology_registry()
    engine = create_semantic_engine(settings.database_url)
    await initialize_semantic_database(engine)
    repository = ActionRepository(create_semantic_session_factory(engine), ontology)
    sql_policy = get_sql_scope_policy_registry()
    runtime = create_public_demo_runtime(ontology=ontology, sql_policy=sql_policy) if public_demo_enabled() else SemanticQueryRuntime(ontology=ontology, sql_policy=sql_policy)
    executor = DomainApiActionExecutor(
        base_url=os.environ.get("SAAS_DOMAIN_API_BASE_URL", ""),
        service_token=os.environ.get("DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"),
    )
    authorization_revalidator = SaasAuthorizationRevalidator(
        url=os.environ.get("SAAS_ACTION_AUTHORIZATION_REVALIDATION_URL", ""),
        service_token=os.environ.get(
            "DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN",
            "",
        ),
        audience=os.environ.get("SAAS_ACTION_AUTHORIZATION_AUDIENCE", "action-worker"),
    )
    worker = ActionWorker(
        repository=repository,
        ontology=ontology,
        executor=executor,
        authorization_revalidator=authorization_revalidator,
        target_reader=SemanticTargetReader(runtime),
        worker_id=os.environ.get("DEER_FLOW_ACTION_WORKER_ID", f"action-worker-{uuid.uuid4()}"),
        lease_seconds=settings.action_lease_seconds,
    )
    try:
        while True:
            worked = await worker.run_once()
            if not worked:
                await asyncio.sleep(settings.action_worker_poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run_worker())
