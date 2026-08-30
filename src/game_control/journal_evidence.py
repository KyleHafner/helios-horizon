"""Typed package boundary for fixed journal evidence operations."""

from __future__ import annotations

from pathlib import Path

from ._fixed_helper import load


def _helper():
    return load("horizon_journal.py", "_horizon_journal_evidence")


def capture(invocation_id: str, *, evidence_path: Path | None = None):
    helper = _helper()
    if evidence_path is None:
        return helper.capture(invocation_id)
    return helper.capture(invocation_id, evidence_path=evidence_path)


def verify_zero_suppression(invocation_id: str) -> bool:
    return bool(_helper().verify_zero_suppression(invocation_id))


def finalize(*, evidence_path: Path | None = None, override_path: Path | None = None) -> int:
    helper = _helper()
    kwargs = {}
    if evidence_path is not None:
        kwargs["evidence_path"] = evidence_path
    if override_path is not None:
        kwargs["override_path"] = override_path
    return int(helper.finalize(**kwargs))
