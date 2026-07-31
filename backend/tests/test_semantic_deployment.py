from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose(name: str):
    return yaml.safe_load((ROOT / "docker" / name).read_text(encoding="utf-8"))


def _environment(service: dict) -> dict[str, str]:
    values = {}
    for item in service.get("environment") or []:
        key, _, value = str(item).partition("=")
        values[key] = value
    return values


def test_production_compose_isolates_semantic_api_and_action_worker_credentials():
    compose = _compose("docker-compose.yaml")
    services = compose["services"]

    assert "ports" not in services["semantic-api"]
    assert "ports" not in services["action-worker"]
    assert "semantic-data:/var/lib/deerflow-semantic" in services["semantic-api"]["volumes"]
    assert "semantic-data:/var/lib/deerflow-semantic" in services["action-worker"]["volumes"]

    worker_env = _environment(services["action-worker"])
    for service_name in ("gateway", "semantic-api", "provisioner"):
        service_env = _environment(services[service_name])
        assert service_env["DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"] == ""
        assert service_env["DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN"] == ""
    assert worker_env["DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"] != ""
    assert worker_env["DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN"] != ""
    assert _environment(services["gateway"])["DEER_FLOW_SEMANTIC_API_URL"] == "http://semantic-api:8003"


def test_development_compose_isolates_action_worker_credentials():
    services = _compose("docker-compose-dev.yaml")["services"]

    for service_name in ("gateway", "semantic-api", "provisioner"):
        service_env = _environment(services[service_name])
        assert service_env["DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"] == ""
        assert service_env["DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN"] == ""
    worker_env = _environment(services["action-worker"])
    assert worker_env["DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"] != ""
    assert worker_env["DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN"] != ""


def test_development_compose_passes_model_routing_extra_to_gateway_build_and_runtime():
    gateway = _compose("docker-compose-dev.yaml")["services"]["gateway"]

    assert gateway["build"]["args"]["UV_EXTRAS"] == "${UV_EXTRAS:-}"
    assert "UV_EXTRAS=${UV_EXTRAS:-}" in gateway["environment"]


def test_semantic_api_healthchecks_use_runtime_python():
    for compose_name in ("docker-compose.yaml", "docker-compose-dev.yaml"):
        healthcheck = _compose(compose_name)["services"]["semantic-api"]["healthcheck"]["test"]

        assert healthcheck[:2] == ["CMD", "python"]
        assert "urllib.request.urlopen" in healthcheck[3]
        assert "http://localhost:8003/health" in healthcheck[3]


def test_local_launcher_starts_internal_semantic_api_and_optional_worker():
    script = (ROOT / "scripts" / "serve.sh").read_text(encoding="utf-8")

    assert "app.semantic.api:create_app --factory" in script
    assert "--host 127.0.0.1 --port 8003" in script
    assert "DEER_FLOW_ACTIONS_ENABLED:-false" in script
    assert "python -m app.semantic.worker" in script
    worker_start = script.index("python -m app.semantic.worker")
    credential_scrub = script.index("unset DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN")
    semantic_start = script.index("app.semantic.api:create_app --factory")
    assert worker_start < credential_scrub < semantic_start
    assert "unset DEER_FLOW_ACTION_WORKER_AUTHORIZATION_TOKEN" in script
    assert "unset SAAS_DOMAIN_API_TOKEN" in script


def test_evals_compose_overlay_is_local_only_and_uses_dedicated_eval_credentials():
    services = _compose("docker-compose-evals.yaml")["services"]

    assert services["gateway"]["ports"] == ["127.0.0.1:8001:8001"]
    assert services["semantic-api"]["ports"] == ["127.0.0.1:8003:8003"]
    assert services["eval-fixture"]["ports"] == ["127.0.0.1:8004:8004"]
    assert services["eval-fixture"]["restart"] == "no"
    for service_name in ("gateway", "semantic-api", "action-worker"):
        environment = _environment(services[service_name])
        assert environment["DEER_FLOW_ENV"] == "eval"
        assert environment["SAAS_AUTHORIZATION_JWT_KEY"] == "${EVALS_AUTHORIZATION_JWT_KEY}"
    worker_environment = _environment(services["action-worker"])
    assert "eval-fixture" in worker_environment["NO_PROXY"]
    assert worker_environment["DEER_FLOW_ACTION_WORKER_DOMAIN_API_TOKEN"] == "${EVALS_SAAS_INTERNAL_TOKEN}"
