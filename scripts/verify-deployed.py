#!/usr/bin/env python3
"""Fixed compatibility front door for the package deployment verifier."""

from __future__ import annotations

import sys
from pathlib import Path


_PACKAGE_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_SRC))

from game_control.deployment_verify import *  # noqa: F401,F403,E402
from game_control.deployment_verify import main


if __name__ == "__main__":
    raise SystemExit(main())
