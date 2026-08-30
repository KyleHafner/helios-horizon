"""Typed in-process access to the fixed Sunlit promotion policy."""

from __future__ import annotations

from ._fixed_helper import load


def promote() -> dict:
    return load("horizon-sunlit-promote", "_horizon_sunlit_promote").promote()
