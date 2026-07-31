from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage


@pytest.mark.asyncio
async def test_saas_query_denied_action_preflight_is_deterministic(monkeypatch):
    from deerflow.agents.saas_query import agent as module

    published = []
    subagent_started = False
    journal_calls = []

    class Writer:
        def __call__(self, event):
            published.append(event)

    class Journal:
        def record_semantic_preflight_denial(self, *, code, semantic_trace_id):
            journal_calls.append({"code": code, "semantic_trace_id": semantic_trace_id})

    async def coverage(_question, _runtime):
        return {
            "ontology_version": "1",
            "objects": [{"id": "Site"}],
            "actions": [],
            "action_authorization": {"status": "denied", "code": "AUTHORIZATION_DENIED"},
            "semantic_trace_id": "semantic-trace-denied",
        }

    async def should_not_start(*_args, **_kwargs):
        nonlocal subagent_started
        subagent_started = True
        raise AssertionError("denied Action preflight must stop before SubAgents")

    monkeypatch.setattr(module, "_resolve_semantic_coverage", coverage)
    monkeypatch.setattr(module, "_run_semantic_subagent", should_not_start)
    monkeypatch.setattr(module, "get_stream_writer", lambda: Writer())

    result = await module._saas_query_node(
        {"messages": [HumanMessage(content="rename site display name")]},
        {
            "context": {
                "run_id": "run-1",
                "thread_id": "thread-1",
                "app_config": object(),
                "__run_journal": Journal(),
            }
        },
    )

    assert subagent_started is False
    assert "未获得授权" in result["messages"][0].content
    assert {
        "type": "saas_query_action_preflight_denied",
        "code": "AUTHORIZATION_DENIED",
        "semantic_trace_id": "semantic-trace-denied",
    } in published
    assert journal_calls == [
        {
            "code": "AUTHORIZATION_DENIED",
            "semantic_trace_id": "semantic-trace-denied",
        }
    ]
