"""Control-plane client for isolated synthetic SaaS evaluation fixtures."""

from __future__ import annotations

from typing import Any

import httpx

from app.evals.contracts import EvalCase


class FixtureError(RuntimeError):
    pass


class FixtureClient:
    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._base_url = settings.fixture_url.rstrip("/")
        self._token = settings.fixture_token
        self._transport = transport

    async def reset(
        self,
        *,
        case: EvalCase,
        eval_run_id: str,
        trial_id: str,
        thread_id: str,
        expected_scope_hash: str,
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            f"/v1/evals/trials/{trial_id}/reset",
            json={
                "eval_run_id": eval_run_id,
                "trial_id": trial_id,
                "thread_id": thread_id,
                "case_id": case.case_id,
                "scenario": case.fixture.scenario,
                "tenant_id": case.fixture.tenant_id,
                "tenant_code": case.fixture.tenant_code,
                "tenant_name": case.fixture.tenant_name,
                "principal_id": case.fixture.principal_id,
                "system_code": case.fixture.system_code,
                "permission_version": case.fixture.permission_version,
                "role_codes": list(case.fixture.role_codes),
                "scope": case.fixture.scope.model_dump(mode="json"),
                "scope_hash": expected_scope_hash,
                "expected_after": (case.expect.action.expected_after if case.expect.action is not None else {}),
            },
        )
        state = payload.get("state")
        if not isinstance(state, dict):
            raise FixtureError("Fixture reset response has no state object")
        return state

    async def state(self, *, trial_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/v1/evals/trials/{trial_id}/state")

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            headers={"X-Evals-Token": self._token},
            timeout=30,
            transport=self._transport,
            trust_env=False,
        ) as client:
            response = await client.request(method, path, **kwargs)
        if response.is_error:
            raise FixtureError(f"Fixture request failed with HTTP {response.status_code}: {response.text[:1000]}")
        payload = response.json()
        if not isinstance(payload, dict):
            raise FixtureError("Fixture response must be a JSON object")
        return payload
