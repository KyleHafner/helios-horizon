#!/usr/bin/env python3
"""Source-only entry point for the disposable memory-pressure drill."""

# Contract markers: horizon-memory-drill.v1, MemorySwapMax, OOMPolicy.

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.acceptance.memory_pressure_drill import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
