"""Package API for the independent deployment verifier.

The source script remains the compatibility front door until the Wave5
manifest cutover.  Loading is from this checkout's fixed package root only;
there is no target-root or CWD import fallback.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Sequence


def _module() -> ModuleType:
    package_root = Path(__file__).resolve().parents[2]
    path = package_root / "scripts" / "verify-deployed.py"
    spec = importlib.util.spec_from_file_location("_horizon_deployment_verify", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("deployment verifier is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: Sequence[str] | None = None) -> int:
    return int(_module().main(argv))


def verify_static(target_root: Path, manifest) -> int:
    module = _module()
    module.TARGET_ROOT = Path(target_root)
    module._DEPLOYMENT_MANIFEST = manifest
    return int(module.main(["--static", "--root", str(target_root)]))


def verify_live(live_context=None) -> int:
    del live_context
    return main([])
