"""Environment-backed configuration for the semantic platform."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SemanticSettings:
    database_url: str
    service_token: str
    authorization_audience: str
    action_worker_poll_seconds: float
    action_lease_seconds: int
    scope_resolver_url: str = ""
    scope_resolver_token: str = ""
    evals_evidence_enabled: bool = False


def get_semantic_settings() -> SemanticSettings:
    from deerflow.config import get_app_config

    database_url = os.environ.get(
        "DEER_FLOW_SEMANTIC_DATABASE_URL",
        "sqlite+aiosqlite:///./.deer-flow/data/semantic.db",
    ).strip()
    service_token = os.environ.get("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", "").strip()
    audience = os.environ.get("SAAS_AUTHORIZATION_SEMANTIC_AUDIENCE", "semantic-platform").strip()
    try:
        poll_seconds = float(os.environ.get("DEER_FLOW_ACTION_WORKER_POLL_SECONDS", "1"))
        lease_seconds = int(os.environ.get("DEER_FLOW_ACTION_LEASE_SECONDS", "30"))
    except ValueError as exc:
        raise RuntimeError("Invalid semantic platform worker configuration") from exc
    if not database_url or not service_token or not audience:
        raise RuntimeError("DEER_FLOW_SEMANTIC_DATABASE_URL, DEER_FLOW_SEMANTIC_SERVICE_TOKEN and semantic audience are required")
    explicit_evals = os.environ.get("DEER_FLOW_EVALS_EVIDENCE_ENABLED")
    if explicit_evals is not None:
        evals_requested = explicit_evals.strip().lower() in {"1", "true", "yes", "on"}
    else:
        try:
            evals_requested = get_app_config().evals.enabled
        except Exception:
            evals_requested = False
    evals_enabled = os.environ.get("DEER_FLOW_ENV", "").strip() == "eval" and evals_requested
    return SemanticSettings(
        database_url=database_url,
        service_token=service_token,
        authorization_audience=audience,
        action_worker_poll_seconds=max(0.1, poll_seconds),
        action_lease_seconds=max(5, lease_seconds),
        scope_resolver_url=os.environ.get(
            "SAAS_AUTHORIZATION_SCOPE_RESOLVER_URL",
            "",
        ).strip(),
        scope_resolver_token=os.environ.get(
            "DEER_FLOW_SEMANTIC_SCOPE_RESOLVER_TOKEN",
            "",
        ).strip(),
        evals_evidence_enabled=evals_enabled,
    )
