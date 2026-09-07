"""Validated configuration for the browser-facing public origin."""

from __future__ import annotations

import os
from urllib.parse import urlsplit


PUBLIC_ORIGIN_ENV = "HORIZON_PUBLIC_ORIGIN"


class PublicOriginConfigError(RuntimeError):
    """Raised when the web process has no safe, explicit public origin."""


def load_public_origin(environ: dict[str, str] | None = None) -> str:
    """Load the exact browser origin required for authenticated mutations.

    There is intentionally no fallback: accepting a guessed origin can leave
    the UI readable while every browser mutation is rejected by CSRF origin
    validation.
    """

    values = os.environ if environ is None else environ
    raw = values.get(PUBLIC_ORIGIN_ENV)
    if not raw or raw != raw.strip() or not raw.isascii() or any(ord(char) < 32 or ord(char) == 127 or char.isspace() for char in raw):
        raise PublicOriginConfigError(
            f"{PUBLIC_ORIGIN_ENV} must be set to an explicit HTTPS origin "
            "(for example, https://games.example.com)"
        )
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        raise PublicOriginConfigError(
            f"{PUBLIC_ORIGIN_ENV} must be an origin-only HTTPS URL "
            "without a path, query, fragment, or credentials"
        ) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise PublicOriginConfigError(
            f"{PUBLIC_ORIGIN_ENV} must be an origin-only HTTPS URL "
            "without a path, query, fragment, or credentials"
        )
    return raw
