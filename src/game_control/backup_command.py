"""Typed package boundary for fixed backup reconciliation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Sequence

from .backup_reconcile import apply_replacement_plan, build_fixed_plan, plan_json
from .backups import B2CommandTransport
from .errors import SafeError
from .state_db import StateDatabase


STATE_DB_URI = "file:/var/lib/game-control/state.db?mode=ro"
STATE_DB_PATH = Path("/var/lib/game-control/state.db")


def reconcile(argv: Sequence[str] = ()) -> int:
    values = list(argv)
    if values == ["--help"]:
        print("usage: horizon backup reconcile [--apply]")
        return 0
    if any(value != "--apply" for value in values) or values.count("--apply") > 1:
        print('{"status":"HOLD","refusals":["only --apply is permitted"]}')
        return 2
    applying = values == ["--apply"]
    connection: sqlite3.Connection | None = None
    database: StateDatabase | None = None
    try:
        if applying:
            database = StateDatabase.open(STATE_DB_PATH)
            connection = database.connection
        else:
            connection = sqlite3.connect(STATE_DB_URI, uri=True, timeout=5.0)
            connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        transport = B2CommandTransport()
        plan = build_fixed_plan(connection, transport)
        if applying and plan.ready:
            apply_replacement_plan(connection, transport, plan)
        print(plan_json(plan))
        return 0 if plan.ready else 2
    except (SafeError, OSError, sqlite3.Error):
        print('{"status":"HOLD","refusals":["fixed evidence could not be verified"]}')
        return 2
    finally:
        if database is not None:
            database.close()
        elif connection is not None:
            connection.close()
