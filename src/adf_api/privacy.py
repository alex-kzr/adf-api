"""Redact before persistence or export; refuse credentials in registry manifests."""

import hashlib
import json
import re
from typing import Any

from .errors import DomainError

PRIVATE_KEYS = {
    "authorization",
    "password",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "credential",
    "credentials",
    "cookie",
    "env",
    "environment",
}
PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+"),
    re.compile(r"(?i)((?:password|token|api[_-]?key|secret)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})"),
]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {
            str(k): "[REDACTED]"
            if str(k).lower() in PRIVATE_KEYS
            else redact(v, secrets)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) >= 8:
                value = value.replace(secret, "[REDACTED]")
        for pattern in PATTERNS:
            value = pattern.sub(
                lambda match: (
                    (match.group(1) if match.lastindex else "") + "[REDACTED]"
                ),
                value,
            )
        return value
    return value


def reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in PRIVATE_KEYS:
                raise DomainError(
                    "INVALID_MANIFEST",
                    "Credential values and environment maps are prohibited; use local secret references.",
                    422,
                )
            reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            reject_credentials(child)
