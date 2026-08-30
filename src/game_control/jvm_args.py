"""Typed package adapter for fixed Sunlit JVM tuning operations."""

from __future__ import annotations

from ._fixed_helper import load


def _helper():
    return load("horizon-jvm-args", "_horizon_jvm_args")


def verify() -> int:
    return int(_helper().main(["verify"]))


def activate() -> int:
    return int(_helper().main(["activate"]))


def rollback() -> int:
    return int(_helper().main(["rollback"]))
