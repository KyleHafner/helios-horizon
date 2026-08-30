"""Typed package adapter for fixed root session revocation."""

from __future__ import annotations

from ._fixed_helper import load


def revoke_all() -> int:
    return int(load("horizon-session-revoke-all", "_horizon_session_revoke").main())
