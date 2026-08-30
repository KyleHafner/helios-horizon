from __future__ import annotations

import importlib.util
import sqlite3
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from importlib.machinery import SourceFileLoader

import pytest

from game_control.capability import CapabilityTokenStore


ROOT = Path(__file__).parents[1]
LOADER = SourceFileLoader(
    "horizon_capability_issue", str(ROOT / "ops" / "bin" / "horizon-capability-issue")
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
ISSUER = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(ISSUER)


def test_root_issuer_installs_three_fixed_tokens_without_output(tmp_path, capsys):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))

    report = ISSUER.issue_and_install(store, secret_dir)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert report["state"] == "issued"
    assert report["operational_ttl_days"] == 30
    assert len(report["expires_at"]) == 3
    expected = {
        "lazymc-waker.token",
        "helios-mcp-observer.token",
        "helios-mcp-waker.token",
    }
    assert {path.name for path in secret_dir.iterdir()} == expected
    for path in secret_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_text().startswith("hc_")
    rows = store.db.execute("SELECT role,audience,profile_id FROM capability_tokens ORDER BY audience,role").fetchall()
    assert rows == [
        ("observer", "helios-mcp", None),
        ("waker", "helios-mcp", "minecraft-sunlit-cobblemon"),
        ("waker", "lazymc", "minecraft-sunlit-cobblemon"),
    ]
    audit = store.db.execute("SELECT detail FROM capability_audit").fetchall()
    assert all("hc_" not in str(row) for row in audit)


def test_root_issuer_is_noop_for_three_valid_files_and_active_grants(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    first = ISSUER.issue_and_install(store, secret_dir)
    before_files = {path.name: path.read_bytes() for path in secret_dir.iterdir()}
    before_rows = store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0]

    second = ISSUER.issue_and_install(store, secret_dir)

    assert second["state"] == "unchanged"
    assert second["operational_ttl_days"] == first["operational_ttl_days"] == 30
    assert {path.name: path.read_bytes() for path in secret_dir.iterdir()} == before_files
    assert store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0] == before_rows == 3


def test_root_issuer_refuses_partial_state_without_touching_existing_file(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    partial = secret_dir / "lazymc-waker.token"
    partial.write_text("hc_existing-token\n", encoding="utf-8")
    partial.chmod(0o600)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))

    with pytest.raises(RuntimeError, match="partial"):
        ISSUER.issue_and_install(store, secret_dir)

    assert partial.read_text(encoding="utf-8") == "hc_existing-token\n"
    assert store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0] == 0


def test_root_issuer_refuses_partial_state_without_deleting_previous_valid_file(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    ISSUER.issue_and_install(store, secret_dir)
    valid_path = secret_dir / "lazymc-waker.token"
    valid_bytes = valid_path.read_bytes()
    (secret_dir / "helios-mcp-observer.token").unlink()
    (secret_dir / "helios-mcp-waker.token").unlink()

    with pytest.raises(RuntimeError, match="partial"):
        ISSUER.issue_and_install(store, secret_dir)

    assert valid_path.read_bytes() == valid_bytes
    assert store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0] == 3


def test_root_issuer_refuses_mismatched_complete_state_without_overwrite(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    ISSUER.issue_and_install(store, secret_dir)
    original = (secret_dir / "helios-mcp-observer.token").read_bytes()
    (secret_dir / "helios-mcp-observer.token").write_bytes(b"hc_wrong-token\n")

    with pytest.raises(Exception):
        ISSUER.issue_and_install(store, secret_dir)

    assert (secret_dir / "helios-mcp-observer.token").read_bytes() == b"hc_wrong-token\n"
    assert store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0] == 3
    assert original != b"hc_wrong-token\n"


def test_root_issuer_refuses_orphan_active_grant_when_files_are_absent(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    store.issue(
        role="waker",
        audience="lazymc",
        profile_id="minecraft-sunlit-cobblemon",
        ttl=ISSUER.OPERATIONAL_TTL,
    )

    with pytest.raises(RuntimeError, match="active capability grant"):
        ISSUER.issue_and_install(store, secret_dir)

    assert not list(secret_dir.iterdir())
    assert store.db.execute("SELECT COUNT(*) FROM capability_tokens").fetchone()[0] == 1


def test_initial_install_failure_rolls_back_only_new_grants_and_files(tmp_path, monkeypatch):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    store = CapabilityTokenStore(sqlite3.connect(":memory:"))
    original_install = ISSUER._install_secret
    calls = 0

    def fail_second(path, token):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic install failure")
        return original_install(path, token)

    monkeypatch.setattr(ISSUER, "_install_secret", fail_second)
    with pytest.raises(OSError, match="synthetic"):
        ISSUER.issue_and_install(store, secret_dir)

    assert not list(secret_dir.iterdir())
    rows = store.db.execute("SELECT revoked_at FROM capability_tokens").fetchall()
    assert len(rows) == 3
    assert all(row[0] is not None for row in rows)


def test_expired_complete_state_is_not_silently_reissued(tmp_path):
    secret_dir = tmp_path / "secrets.d"
    secret_dir.mkdir(mode=0o700)
    now = [datetime(2026, 8, 6, tzinfo=timezone.utc)]
    store = CapabilityTokenStore(sqlite3.connect(":memory:"), clock=lambda: now[0])
    ISSUER.issue_and_install(store, secret_dir)
    before = {path.name: path.read_bytes() for path in secret_dir.iterdir()}
    now[0] += timedelta(days=31)

    with pytest.raises(Exception):
        ISSUER.issue_and_install(store, secret_dir)

    assert {path.name: path.read_bytes() for path in secret_dir.iterdir()} == before


def test_root_issuer_rejects_nonroot_without_output(monkeypatch, capsys):
    monkeypatch.setattr(ISSUER.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(ISSUER.sys, "argv", ["horizon-capability-issue"])
    assert ISSUER.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_root_issuer_main_surfaces_expiry_metadata_without_tokens(monkeypatch, capsys):
    class Database:
        connection = sqlite3.connect(":memory:")

        def close(self):
            self.connection.close()

    monkeypatch.setattr(ISSUER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ISSUER.sys, "argv", ["horizon-capability-issue"])
    monkeypatch.setattr(ISSUER.WebDatabase, "open", lambda _path: Database())
    monkeypatch.setattr(
        ISSUER,
        "issue_and_install",
        lambda _store: {"state": "unchanged", "expires_at": {"lazymc-waker": "2026-09-05T00:00:00Z"}},
    )

    assert ISSUER.main() == 0
    output = capsys.readouterr()
    assert "expires_at" in output.out
    assert "hc_" not in output.out
    assert output.err == ""
