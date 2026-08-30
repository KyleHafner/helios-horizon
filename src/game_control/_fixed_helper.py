"""Load a checked-in fixed helper during the Wave5 extraction window.

The filename and module name are constants owned by each package adapter; no
caller input reaches this loader.  Wave6 may remove the remaining legacy
helper files after the package implementations are fully moved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load(filename: str, module_name: str) -> ModuleType:
    package_root = Path(__file__).resolve().parents[2]
    path = package_root / "ops" / "bin" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("fixed Horizon helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
