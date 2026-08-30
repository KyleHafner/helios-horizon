"""Typed package adapter for root-only capability issuance."""

from __future__ import annotations

import os
from pathlib import Path

from ._fixed_helper import load


def issue() -> int:
    if os.geteuid() != 0:
        return 2
    helper = load("horizon-capability-issue", "_horizon_capability_issue")
    return int(helper.main())


def issue_and_install(store, secret_dir: Path | None = None):
    helper = load("horizon-capability-issue", "_horizon_capability_issue")
    if secret_dir is None:
        return helper.issue_and_install(store)
    return helper.issue_and_install(store, secret_dir)
