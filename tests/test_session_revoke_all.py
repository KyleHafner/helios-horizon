from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
HELPER = ROOT / "ops/bin/horizon-session-revoke-all"


def _load():
    loader = SourceFileLoader("horizon_session_revoke_all", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    spec.loader.exec_module(module)
    return module


def test_audit_append_is_fixed_root_only_and_secret_free(tmp_path, monkeypatch):
    helper = _load()
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    audit = parent / "session-revoke-all.jsonl"
    monkeypatch.setattr(helper, "AUDIT_PATH", audit)

    helper._append_audit(
        {
            "event": "horizon_session_revoke_all",
            "revoked_sessions": 2,
            "timestamp_utc": "2026-08-05T23:00:00Z",
        }
    )

    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert audit.stat().st_uid == 0
    assert json.loads(audit.read_text())["revoked_sessions"] == 2
    assert "session_hash" not in audit.read_text()


def test_audit_append_rejects_symlink(tmp_path, monkeypatch):
    helper = _load()
    parent = tmp_path / "state"
    parent.mkdir(mode=0o700)
    target = parent / "target"
    target.write_text("untouched")
    audit = parent / "session-revoke-all.jsonl"
    audit.symlink_to(target)
    monkeypatch.setattr(helper, "AUDIT_PATH", audit)

    with pytest.raises(OSError):
        helper._append_audit({"event": "horizon_session_revoke_all"})
    assert target.read_text() == "untouched"
