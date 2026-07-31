"""Trusted correlation context required by Semantic Platform APIs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from fastapi import Header, HTTPException

_SAFE_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class SemanticRequestContext:
    run_id: str
    thread_id: str
    tool_call_id: str
    semantic_trace_id: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _validated(value: str | None, name: str) -> str:
    candidate = str(value or "").strip()
    if not _SAFE_CORRELATION_ID.fullmatch(candidate):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_SEMANTIC_QUERY",
                "message": f"Missing or invalid {name}",
            },
        )
    return candidate


async def require_semantic_request_context(
    run_id: str | None = Header(default=None, alias="X-DeerFlow-Run-Id"),
    thread_id: str | None = Header(default=None, alias="X-DeerFlow-Thread-Id"),
    tool_call_id: str | None = Header(default=None, alias="X-DeerFlow-Tool-Call-Id"),
    semantic_trace_id: str | None = Header(
        default=None,
        alias="X-DeerFlow-Semantic-Trace-Id",
    ),
) -> SemanticRequestContext:
    return SemanticRequestContext(
        run_id=_validated(run_id, "run_id"),
        thread_id=_validated(thread_id, "thread_id"),
        tool_call_id=_validated(tool_call_id, "tool_call_id"),
        semantic_trace_id=_validated(semantic_trace_id, "semantic_trace_id"),
    )
