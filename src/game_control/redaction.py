"""Fail-safe redaction for all externally visible game-control text."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from itertools import islice

_MAX_TEXT = 512 * 1024


class SecretRegistry:
    """Small root-owned secret registry.

    The registry intentionally stores only values and never exposes a mapping
    suitable for serialisation.  Callers provide a snapshot to a Redactor.
    """

    def __init__(self, secrets: Mapping[str, str] | Iterable[str] = ()):
        if isinstance(secrets, Mapping):
            values = secrets.values()
        else:
            values = secrets
        self._values = {value for value in values if isinstance(value, str) and value}

    def add(self, value: str) -> None:
        if isinstance(value, str) and value:
            self._values.add(value)

    def values(self) -> tuple[str, ...]:
        return tuple(self._values)


_PATTERNS = (
    # Keep these deliberately conservative around the value and replace the
    # whole credential, rather than attempting to preserve a prefix.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9._-]+"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),  # Telegram bot token
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
        r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)"
    ),
    re.compile(
        r"(?i)\b(?:password|passwd|pass|secret|token|api[_-]?key|authorization|cookie)"
        r"\s*[:=]\s*[^\s,;&]+"
    ),
    re.compile(r"(?i)\b(?:session|connect\.sid|auth|jwt)[_-]?(?:cookie|token)?\s*=\s*[^\s;]+"),
)
_PEM_MARKER = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|-----END [A-Z0-9 ]*PRIVATE KEY-----"
)


class Redactor:
    def __init__(self, secrets: SecretRegistry | Mapping[str, str] | Iterable[str] = ()):
        self.registry = secrets if isinstance(secrets, SecretRegistry) else SecretRegistry(secrets)
        configured = sorted(self.registry.values(), key=len, reverse=True)
        self._configured = tuple(re.compile(re.escape(value)) for value in configured if value)

    def redact(self, text: str) -> str:
        """Redact configured and recognizable credentials.

        Redaction is fail-safe: non-text or oversized input is replaced with a
        marker rather than being returned to a caller, and a regex failure
        cannot expose the original value.
        """

        if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > _MAX_TEXT:
            return "[REDACTED]"
        try:
            result = text
            for pattern in self._configured:
                result = pattern.sub("[REDACTED]", result)
            for pattern in _PATTERNS:
                result = pattern.sub("[REDACTED]", result)
            return result
        except (re.error, UnicodeError):
            return "[REDACTED]"

    def redact_lines(self, lines: Iterable[str]) -> list[str]:
        return self.redact_records(islice(lines, 5000))

    def redact_records(self, lines: Iterable[str]) -> list[str]:
        """Redact a bounded line sequence while preserving record cardinality."""

        values = [line if isinstance(line, str) else str(line) for line in lines]
        output: list[str] = []
        in_pem = False
        for index, value in enumerate(values):
            markers = tuple(_PEM_MARKER.finditer(value))
            record_redacted = in_pem
            unmatched_end = False
            for marker in markers:
                if marker.group().startswith("-----BEGIN"):
                    record_redacted = True
                    in_pem = True
                else:
                    if not in_pem:
                        unmatched_end = True
                    record_redacted = True
                    in_pem = False
            if unmatched_end:
                # A page can begin mid-credential.  Fail closed for every
                # preceding record through the unmatched END marker.
                output = ["[REDACTED]"] * index
            if record_redacted:
                output.append("[REDACTED]")
                continue
            output.append(self.redact(value)[:8192])
        return output


__all__ = ["SecretRegistry", "Redactor"]
