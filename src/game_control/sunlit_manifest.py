"""Typed in-process access to the fixed Sunlit manifest policy."""

from __future__ import annotations

from argparse import Namespace

from ._fixed_helper import load


def make_manifest(args: Namespace) -> dict:
    return load("horizon-sunlit-manifest", "_horizon_sunlit_manifest").make_manifest(args)
