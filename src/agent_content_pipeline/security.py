from __future__ import annotations

import re
from typing import Any, Iterable


_SENSITIVE_KEY = re.compile(
    r"(?:secret|token|password|authorization|cookie|base64)",
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)(bearer\s+)[^\s,;]+")
_SET_COOKIE_HEADER = re.compile(r"(?i)set-cookie\s*:[^\r\n]*")


def _redact_string(value: str, secret_values: tuple[str, ...]) -> str:
    redacted = _BEARER_VALUE.sub(r"\1[REDACTED]", value)
    redacted = _SET_COOKIE_HEADER.sub("Set-Cookie: [REDACTED]", redacted)
    for secret in secret_values:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def redact_sensitive_data(
    value: Any,
    *,
    secret_values: Iterable[str] = (),
) -> Any:
    """Return a log-safe structure without mutating the input."""

    secrets = tuple(sorted({item for item in secret_values if item}, key=len, reverse=True))

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else redact(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, tuple):
            return tuple(redact(child) for child in item)
        if isinstance(item, str):
            return _redact_string(item, secrets)
        return item

    return redact(value)
