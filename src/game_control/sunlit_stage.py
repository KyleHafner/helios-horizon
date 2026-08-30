"""Typed in-process access to the fixed Sunlit staging policy."""

from __future__ import annotations

from argparse import Namespace

from ._fixed_helper import load


def stage(args: Namespace) -> dict:
    return load("horizon-sunlit-stage", "_horizon_sunlit_stage").stage(args)
