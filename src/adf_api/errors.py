"""Stable bounded error envelopes, without exception or database configuration leaks."""

from typing import Any


class DomainError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status: int = 409,
        details: dict[str, Any] | None = None,
        *,
        persist: bool = False,
    ):
        super().__init__(message)
        self.code, self.message, self.status, self.details = (
            code,
            message,
            status,
            details or {},
        )
        self.persist = persist
