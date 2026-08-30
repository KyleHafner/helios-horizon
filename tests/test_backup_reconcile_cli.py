from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]
LOADER = SourceFileLoader(
    "horizon_backup_reconcile_cli",
    str(ROOT / "ops" / "bin" / "horizon-backup-reconcile"),
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
CLI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(CLI)


def test_apply_uses_configured_state_database_connection(monkeypatch):
    calls = []

    class _Connection:
        def execute(self, statement):
            calls.append(("execute", statement))
            return self

    class _Database:
        connection = _Connection()

        def close(self):
            calls.append(("close",))

    database = _Database()
    monkeypatch.setattr(CLI.StateDatabase, "open", lambda path: calls.append(("open", path)) or database)
    monkeypatch.setattr(CLI, "B2CommandTransport", lambda: "transport")
    monkeypatch.setattr(CLI, "build_fixed_plan", lambda connection, transport: SimpleNamespace(ready=True))
    monkeypatch.setattr(
        CLI,
        "apply_replacement_plan",
        lambda connection, transport, plan: calls.append(("apply", connection, transport, plan.ready)),
    )
    monkeypatch.setattr(CLI, "plan_json", lambda _plan: '{"status":"READY"}')

    assert CLI.main(["--apply"]) == 0
    assert calls[0] == ("open", CLI.STATE_DB_PATH)
    assert ("apply", database.connection, "transport", True) in calls
    assert calls[-1] == ("close",)
