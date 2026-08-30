"""Typed package boundary for the fixed Sunlit update workflow."""

from __future__ import annotations

from ._fixed_helper import load


def run(*, check_only: bool) -> dict:
    """Run the fixed updater; values are selected by policy, never argv."""

    return load("horizon-sunlit-auto-update", "_horizon_sunlit_update").run(check_only=check_only)
