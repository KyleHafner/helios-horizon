#!/usr/bin/env python3
"""Run the Phase 2.1 decision against a JSON fixture (read-only; SKIP_PUSH or INCONCLUSIVE)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.acceptance.performance_thresholds import evaluate_thresholds, samples_from_mapping  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path, help="JSON sample fixture")
    parser.add_argument("--output", type=Path, help="write report atomically")
    args = parser.parse_args()
    data = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = evaluate_thresholds(samples_from_mapping(data))
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        temporary = args.output.with_name(args.output.name + ".tmp")
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(args.output)
    else:
        print(encoded, end="")
    return 0 if report["decision"] != "INCONCLUSIVE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
