"""Synthetic IAM and Domain API used only by isolated Evals environments."""

from __future__ import annotations

import asyncio
import copy
import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import jwt
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvalFixtureSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: str
    control_token: str = Field(min_length=1)
    saas_internal_token: str = Field(min_length=1)
    authorization_jwt_key: str = Field(min_length=32)
    authorization_algorithm: str = "HS256"
    authorization_issuer: str = "saas-gateway"
    action_worker_audience: str = "action-worker"
    authorization_ttl_seconds: int = Field(default=300, ge=30, le=300)

    @model_validator(mode="after")
    def require_eval_environment(self) -> EvalFixtureSettings:
        if self.environment != "eval":
            raise ValueError("Evals fixture requires DEER_FLOW_ENV=eval")
        return self

    @classmethod
    def from_env(cls) -> EvalFixtureSettings:
        return cls(
            environment=os.environ.get("DEER_FLOW_ENV", ""),
            control_token=os.environ.get("EVALS_FIXTURE_TOKEN", ""),
            saas_internal_token=os.environ.get("EVALS_SAAS_INTERNAL_TOKEN", ""),
            authorization_jwt_key=(os.environ.get("EVALS_AUTHORIZATION_JWT_KEY") or os.environ.get("SAAS_AUTHORIZATION_JWT_KEY", "")),
            authorization_algorithm=os.environ.get("EVALS_AUTHORIZATION_JWT_ALGORITHM", "HS256"),
            authorization_issuer=os.environ.get("SAAS_AUTHORIZATION_JWT_ISSUER", "saas-gateway"),
            action_worker_audience=os.environ.get("SAAS_ACTION_AUTHORIZATION_AUDIENCE", "action-worker"),
        )


class FixtureScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["tenant_all", "resource_set"]
    site_ids: list[str] = Field(default_factory=list)
    project_ids: list[str] = Field(default_factory=list)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str
    trial_id: str
    thread_id: str
    case_id: str
    scenario: str
    tenant_id: str
    tenant_code: str
    tenant_name: str = "Public Demo Tenant"
    principal_id: str
    system_code: str
    permission_version: str
    role_codes: list[str] = Field(default_factory=list)
    scope: FixtureScope
    scope_hash: str
    expected_after: dict[str, Any] = Field(default_factory=dict)


class RevalidationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_id: str
    tenant_id: str
    system_code: str
    permission_version: str
    scope_hash: str
    action_id: str
    action_version: str
    target_type: str
    target_id: str


class DomainActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    target_type: str
    target_id: str
    parameters: dict[str, Any]
    reason: str | None = None
    expected_object_version: str | None = None
    actor: str
    scope_hash: str
    action_id: str
    action_version: str
    compensation: bool = False


def _seed_state(scenario: str) -> dict[str, Any]:
    site_count = {
        "one_visible_one_hidden": 2,
        "two_visible_one_hidden": 3,
        "three_tenant_sites": 3,
        "two_visible_projects": 2,
    }.get(scenario, 1)
    sites = {
        f"site-demo-{index:03d}": {
            "id": f"site-demo-{index:03d}",
            "display_name": "Old Display Name" if index == 1 else f"Site {index}",
            "version": "1",
        }
        for index in range(1, site_count + 1)
    }
    project_count = 2 if scenario == "two_visible_projects" else 1 if scenario == "one_project_visible" else 0
    projects = {
        f"project-demo-{index:03d}": {
            "id": f"project-demo-{index:03d}",
            "name": f"Public Demo Project {index}",
            "site_id": f"site-demo-{index:03d}",
            "version": "1",
        }
        for index in range(1, project_count + 1)
    }
    return {"sites": sites, "projects": projects}


def _leaf_paths(value: Any, prefix: str = "") -> set[str]:
    if isinstance(value, Mapping):
        paths: set[str] = set()
        for key, nested in value.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_leaf_paths(nested, child))
        return paths
    return {prefix} if prefix else set()


def _changed_paths(before: Any, after: Any, prefix: str = "") -> set[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: set[str] = set()
        for key in sorted(set(before) | set(after), key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before:
                paths.update(_leaf_paths(after[key], child) or {child})
            elif key not in after:
                paths.update(_leaf_paths(before[key], child) or {child})
            else:
                paths.update(_changed_paths(before[key], after[key], child))
        return paths
    if before != after:
        return {prefix} if prefix else set()
    return set()


@dataclass
class FixtureTrial:
    request: ResetRequest
    initial_state: dict[str, Any]
    state: dict[str, Any]
    idempotency_results: dict[str, tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=dict)


class FixtureStore:
    def __init__(self) -> None:
        self._trials: dict[str, FixtureTrial] = {}
        self._trial_by_thread: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def reset(self, request: ResetRequest) -> dict[str, Any]:
        state = _seed_state(request.scenario)
        trial = FixtureTrial(
            request=request,
            initial_state=copy.deepcopy(state),
            state=state,
        )
        async with self._lock:
            previous_id = self._trial_by_thread.get(request.thread_id)
            if previous_id and previous_id != request.trial_id:
                raise HTTPException(status_code=409, detail="Thread is already bound to another Eval Trial")
            self._trials[request.trial_id] = trial
            self._trial_by_thread[request.thread_id] = request.trial_id
        return copy.deepcopy(state)

    async def get(self, trial_id: str) -> FixtureTrial | None:
        async with self._lock:
            return self._trials.get(trial_id)

    async def get_by_thread(self, thread_id: str) -> FixtureTrial | None:
        async with self._lock:
            trial_id = self._trial_by_thread.get(thread_id)
            return self._trials.get(trial_id) if trial_id else None

    async def update_display_name(
        self,
        *,
        thread_id: str,
        site_id: str,
        idempotency_key: str,
        request: DomainActionRequest,
    ) -> dict[str, Any]:
        async with self._lock:
            trial_id = self._trial_by_thread.get(thread_id)
            trial = self._trials.get(trial_id) if trial_id else None
            if trial is None:
                raise HTTPException(status_code=404, detail="Eval Trial not found")
            request_payload = request.model_dump(mode="json")
            prior = trial.idempotency_results.get(idempotency_key)
            if prior is not None:
                previous_payload, previous_result = prior
                if previous_payload != request_payload:
                    raise HTTPException(status_code=409, detail="Idempotency key payload changed")
                return copy.deepcopy(previous_result)
            if trial.request.scenario == "action_domain_rejected":
                raise HTTPException(status_code=409, detail="Synthetic Domain API rejection")
            site = trial.state["sites"].get(site_id)
            if site is None:
                raise HTTPException(status_code=404, detail="Site not found")
            if (
                request.target_type != "Site"
                or request.target_id != site_id
                or request.actor != trial.request.principal_id
                or request.scope_hash != trial.request.scope_hash
                or request.action_id != "site.update_display_name"
                or site_id not in trial.request.scope.site_ids
            ):
                raise HTTPException(status_code=403, detail="Synthetic Domain API authorization mismatch")
            name = request.parameters.get("name")
            if not isinstance(name, str) or not name.strip():
                raise HTTPException(status_code=400, detail="Invalid display name")
            site["display_name"] = name.strip()
            site["version"] = str(int(site["version"]) + 1)
            result = {"updated": True, "version": site["version"]}
            trial.idempotency_results[idempotency_key] = (request_payload, result)
            return copy.deepcopy(result)


def create_fixture_app(*, settings: EvalFixtureSettings | None = None) -> FastAPI:
    resolved = settings or EvalFixtureSettings.from_env()
    app = FastAPI(title="Semantic DeerFlow Evals Fixture", version="1")
    app.state.settings = resolved
    app.state.fixture_store = FixtureStore()

    def require_control(token: str | None) -> None:
        if token != resolved.control_token:
            raise HTTPException(status_code=401, detail="Invalid Evals control token")

    def require_worker(token: str | None) -> None:
        if token != resolved.saas_internal_token:
            raise HTTPException(status_code=401, detail="Invalid synthetic SaaS service token")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "environment": "eval"}

    @app.post("/v1/evals/trials/{trial_id}/reset")
    async def reset_trial(
        trial_id: str,
        body: ResetRequest,
        evals_token: str | None = Header(default=None, alias="X-Evals-Token"),
    ) -> dict[str, Any]:
        require_control(evals_token)
        if trial_id != body.trial_id:
            raise HTTPException(status_code=400, detail="Trial id mismatch")
        state = await app.state.fixture_store.reset(body)
        return {"trial_id": trial_id, "state": state}

    @app.get("/v1/evals/trials/{trial_id}/state")
    async def trial_state(
        trial_id: str,
        evals_token: str | None = Header(default=None, alias="X-Evals-Token"),
    ) -> dict[str, Any]:
        require_control(evals_token)
        trial = await app.state.fixture_store.get(trial_id)
        if trial is None:
            raise HTTPException(status_code=404, detail="Eval Trial not found")
        return {
            "trial_id": trial_id,
            "state": copy.deepcopy(trial.state),
            "unexpected_changes": sorted(_changed_paths(trial.initial_state, trial.state) - _leaf_paths(trial.request.expected_after)),
        }

    @app.post("/api/authorization/actions/revalidate")
    async def revalidate_action(
        body: RevalidationRequest,
        service_token: str | None = Header(default=None, alias="X-SaaS-Internal-Token"),
        thread_id: str | None = Header(default=None, alias="X-DeerFlow-Thread-Id"),
    ) -> dict[str, str]:
        require_worker(service_token)
        trial = await app.state.fixture_store.get_by_thread(str(thread_id or ""))
        if trial is None:
            raise HTTPException(status_code=404, detail="Eval Trial not found")
        fixture = trial.request
        if fixture.scenario == "action_iam_denied":
            raise HTTPException(status_code=403, detail="Synthetic IAM denial")
        if (
            body.principal_id != fixture.principal_id
            or body.tenant_id != fixture.tenant_id
            or body.system_code != fixture.system_code
            or body.permission_version != fixture.permission_version
            or body.scope_hash != fixture.scope_hash
            or body.target_id not in fixture.scope.site_ids
        ):
            raise HTTPException(status_code=403, detail="Synthetic IAM context mismatch")
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": resolved.authorization_issuer,
                "aud": resolved.action_worker_audience,
                "sub": fixture.principal_id,
                "tenant_id": fixture.tenant_id,
                "tenant_code": fixture.tenant_code,
                "tenant_name": fixture.tenant_name,
                "system_code": fixture.system_code,
                "role_codes": fixture.role_codes,
                "scope": fixture.scope.model_dump(mode="json"),
                "permission_version": fixture.permission_version,
                "iat": now,
                "exp": now + resolved.authorization_ttl_seconds,
                "jti": str(uuid.uuid4()),
            },
            resolved.authorization_jwt_key,
            algorithm=resolved.authorization_algorithm,
        )
        return {"authorization_token": token}

    @app.patch("/api/internal/sites/{site_id}/display-name")
    async def update_site_display_name(
        site_id: str,
        body: DomainActionRequest,
        service_token: str | None = Header(default=None, alias="X-SaaS-Internal-Token"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        thread_id: str | None = Header(default=None, alias="X-DeerFlow-Thread-Id"),
    ) -> dict[str, Any]:
        require_worker(service_token)
        if not idempotency_key or not thread_id:
            raise HTTPException(status_code=400, detail="Missing Action correlation headers")
        return await app.state.fixture_store.update_display_name(
            thread_id=thread_id,
            site_id=site_id,
            idempotency_key=idempotency_key,
            request=body,
        )

    return app
