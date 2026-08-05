from __future__ import annotations

from typing import Any


class SafeError(Exception):
    """An error whose fields are safe to expose to an operator."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details
        super().__init__(message)

    def __repr__(self) -> str:
        return f"SafeError(code={self.code!r}, message={self.message!r})"
