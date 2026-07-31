"""Last-mile report redaction independent from evidence-source filtering."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE_FRAGMENTS = (
    "authorization",
    "connection",
    "cookie",
    "credential",
    "jdbc",
    "password",
    "secret",
    "token",
)


def redact_report_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= 12:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if any(fragment in key.lower() for fragment in _SENSITIVE_FRAGMENTS):
                result[key] = "[redacted]"
            else:
                result[key] = redact_report_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [redact_report_value(item, depth=depth + 1) for item in value]
    return value
