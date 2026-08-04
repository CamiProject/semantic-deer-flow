from __future__ import annotations

import pytest

from app.semantic.config import get_semantic_settings
from deerflow.config.semantic_recall_config import SemanticRecallConfig
from deerflow.runtime.authorization_context import AuthorizationContext
from deerflow.semantic.faiss_recall import OntologyFaissRecaller
from deerflow.semantic.ontology import OntologyRegistry, get_ontology_registry


def _registry() -> OntologyRegistry:
    return OntologyRegistry.from_mapping(
        {
            "version": "1",
            "objects": {
                "Site": {
                    "table": "demo_sites",
                    "id_field": "id",
                    "label": "site",
                    "keywords": ["facility"],
                    "properties": {"id": {"column": "id", "type": "string"}},
                }
            },
            "links": {},
            "metrics": {
                "site.count": {
                    "label": "site total",
                    "object_type": "Site",
                    "aggregation": "count",
                    "keywords": ["number of facilities"],
                }
            },
            "actions": {
                "site.rename": {
                    "label": "rename site display name",
                    "keywords": ["change site display name"],
                    "target_type": "Site",
                    "scope_dimension": "site",
                    "authorization": {"allowed_roles": ["site_admin"]},
                    "parameters": {"name": {"type": "string", "required": True}},
                    "approval": {"required": True},
                    "executor": {
                        "type": "domain_api",
                        "method": "PATCH",
                        "path": "/sites/{target_id}",
                    },
                }
            },
        }
    )


def _authorization(*roles: str) -> AuthorizationContext:
    return AuthorizationContext.from_mapping(
        {
            "principal_id": "public-user-001",
            "tenant_id": "public-tenant-001",
            "tenant_code": "public_demo",
            "system_code": "demo",
            "role_codes": list(roles),
            "scope_mode": "resource_set",
            "allowed_site_ids": ["site-demo-001"],
            "allowed_project_ids": [],
            "permission_version": "1",
        }
    )


def _recaller(registry: OntologyRegistry) -> OntologyFaissRecaller:
    return OntologyFaissRecaller(
        registry,
        SemanticRecallConfig(
            enabled=True,
            embedding_dimension=384,
            top_k=8,
            similarity_threshold=0.45,
            min_votes=1,
        ),
    )


def test_faiss_recall_adds_action_candidate_when_exact_strings_do_not_match():
    registry = _registry()
    question = "renaming the site's displayed name"

    assert registry.resolve(question)["actions"] == []

    recall = _recaller(registry).recall(question)
    context = registry.resolve(question, candidate_ids=recall.candidate_ids)

    assert recall.source == "faiss"
    assert recall.candidate_ids["actions"] == ("site.rename",)
    assert [item["id"] for item in context["actions"]] == ["site.rename"]


def test_faiss_candidates_remain_subject_to_role_authorization():
    registry = _registry()
    question = "renaming the site's displayed name"
    recall = _recaller(registry).recall(question)

    authorized = registry.resolve(
        question,
        authorization=_authorization("site_admin"),
        candidate_ids=recall.candidate_ids,
    )
    denied = registry.resolve(
        question,
        authorization=_authorization("viewer"),
        candidate_ids=recall.candidate_ids,
    )

    assert [item["id"] for item in authorized["actions"]] == ["site.rename"]
    assert denied["actions"] == []


def test_read_question_never_recalls_action_from_shared_object_words():
    recall = OntologyFaissRecaller(
        _registry(),
        SemanticRecallConfig(
            enabled=True,
            similarity_threshold=0.35,
            min_votes=1,
        ),
    ).recall("How many sites are visible?")

    assert recall.candidate_ids["actions"] == ()


def test_default_recall_threshold_separates_read_and_write_intent():
    recaller = OntologyFaissRecaller(
        get_ontology_registry(),
        SemanticRecallConfig(enabled=True),
    )

    read = recaller.recall("What is the current display name of site-demo-001?")
    write = recaller.recall("renaming the site displayed title")

    assert read.candidate_ids["actions"] == ()
    assert write.candidate_ids["actions"] == ("site.update_display_name",)


def test_disabled_recaller_returns_no_candidates_without_loading_faiss():
    recall = OntologyFaissRecaller(
        _registry(),
        SemanticRecallConfig(enabled=False),
    ).recall("renaming the site's displayed name")

    assert recall.source == "disabled"
    assert recall.candidate_ids == {"objects": (), "metrics": (), "actions": ()}


def test_semantic_settings_fall_back_to_disabled_recall_without_root_config(
    monkeypatch: pytest.MonkeyPatch,
):
    def missing_config():
        raise FileNotFoundError("config.yaml missing")

    monkeypatch.setenv("DEER_FLOW_SEMANTIC_SERVICE_TOKEN", "semantic-token")
    monkeypatch.setattr("deerflow.config.get_app_config", missing_config)

    settings = get_semantic_settings()

    assert settings.semantic_recall.enabled is False
