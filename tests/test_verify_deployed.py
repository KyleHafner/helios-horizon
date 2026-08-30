from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.install import Installer


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_deployed", ROOT / "scripts" / "verify-deployed.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


def _profiles():
    return {
        profile_id: {
            "id": profile_id,
            "ports": [],
            "adapter": "systemd",
            "systemd_unit": VERIFY.PROFILE_UNITS[profile_id],
        }
        for profile_id in VERIFY.PROFILE_IDS
    }


def _listener_profiles():
    profiles = _profiles()
    profiles["minecraft-sunlit-cobblemon"]["ports"] = [{"protocol": "tcp", "port": 25566}]
    profiles["terraria-tmod"]["ports"] = [{"protocol": "tcp", "port": 7777}]
    profiles["terraria-vanilla"]["ports"] = [{"protocol": "tcp", "port": 7777}]
    return profiles


def _run_status(monkeypatch, active_units, owner):
    monkeypatch.setattr(VERIFY, "_systemd_active", lambda unit: unit in active_units)
    monkeypatch.setattr(VERIFY, "_controller_owned_profile", lambda: owner)
    checks = VERIFY.Checks()
    VERIFY._check_profile_status(checks, _profiles(), set())
    return checks


def _run_relay_state(monkeypatch, mode, states):
    monkeypatch.setattr(VERIFY, "_systemd_active", lambda unit: states.get(unit))
    checks = VERIFY.Checks()
    VERIFY._check_relay_state(checks, mode)
    return checks


def _relay_check(checks, unit):
    return next(
        item
        for item in checks.items
        if item["id"] == "relay." + unit.removesuffix(".service")
    )


def test_production_relay_state_requires_bore_and_disarms_terraria(monkeypatch):
    checks = _run_relay_state(
        monkeypatch,
        "production",
        {
            "bore-minecraft-fenced.service": True,
            "horizon-terraria-relay.service": False,
        },
    )

    assert not checks.failed
    assert _relay_check(checks, "bore-minecraft-fenced.service")["expected"] == "active"
    assert _relay_check(checks, "horizon-terraria-relay.service")["expected"] == "inactive"


def test_production_relay_state_rejects_bore_down(monkeypatch):
    checks = _run_relay_state(
        monkeypatch,
        "production",
        {
            "bore-minecraft-fenced.service": False,
            "horizon-terraria-relay.service": False,
        },
    )

    check = _relay_check(checks, "bore-minecraft-fenced.service")
    assert check["ok"] is False
    assert check["reason"] == "active_required"
    assert check["actual"] == "inactive"


def test_production_relay_state_rejects_unexpected_terraria_relay(monkeypatch):
    checks = _run_relay_state(
        monkeypatch,
        "production",
        {
            "bore-minecraft-fenced.service": True,
            "horizon-terraria-relay.service": True,
        },
    )

    check = _relay_check(checks, "horizon-terraria-relay.service")
    assert check["ok"] is False
    assert check["reason"] == "inactive_required"
    assert check["actual"] == "active"


def test_private_relay_state_requires_both_relays_inactive(monkeypatch):
    passing = _run_relay_state(
        monkeypatch,
        "private",
        {
            "bore-minecraft-fenced.service": False,
            "horizon-terraria-relay.service": False,
        },
    )
    failing = _run_relay_state(
        monkeypatch,
        "private",
        {
            "bore-minecraft-fenced.service": True,
            "horizon-terraria-relay.service": False,
        },
    )

    assert not passing.failed
    assert failing.failed
    assert _relay_check(failing, "bore-minecraft-fenced.service")["reason"] == "inactive_required"


def test_relay_state_fails_closed_when_status_probe_is_unavailable(monkeypatch):
    checks = _run_relay_state(
        monkeypatch,
        "production",
        {
            "bore-minecraft-fenced.service": None,
            "horizon-terraria-relay.service": False,
        },
    )

    check = _relay_check(checks, "bore-minecraft-fenced.service")
    assert check["ok"] is False
    assert check["reason"] == "status_probe_unavailable"
    assert check["expected"] == "active"
    assert check["actual"] == "unknown"


def test_verifier_accepts_empty_slot(monkeypatch):
    checks = _run_status(monkeypatch, set(), None)
    assert not checks.failed


def test_verifier_accepts_one_controller_owned_active_profile(monkeypatch):
    checks = _run_status(monkeypatch, {"terraria-tmod.service"}, "terraria-tmod")
    assert not checks.failed


def test_verifier_rejects_one_foreign_active_profile(monkeypatch):
    checks = _run_status(monkeypatch, {"terraria-tmod.service"}, None)
    assert checks.failed


def test_verifier_rejects_two_active_profiles_even_if_one_is_owned(monkeypatch):
    checks = _run_status(
        monkeypatch,
        {"terraria-tmod.service", "minecraft-sunlit-cobblemon.service"},
        "terraria-tmod",
    )
    assert checks.failed


def test_verifier_accepts_listener_for_controller_owned_terraria(monkeypatch):
    monkeypatch.setattr(VERIFY, "_parse_ss", lambda: {("tcp", 7777, "0.0.0.0")})
    checks = VERIFY.Checks()
    VERIFY._check_listeners(checks, _listener_profiles(), "terraria-tmod")
    check = next(item for item in checks.items if item["id"] == "listener.active.terraria.7777")
    assert check["ok"] is True
    assert check["reason"] == "active_owner_listening"


def test_verifier_rejects_listener_without_active_owner(monkeypatch):
    monkeypatch.setattr(VERIFY, "_parse_ss", lambda: {("tcp", 7777, "0.0.0.0")})
    checks = VERIFY.Checks()
    VERIFY._check_listeners(checks, _listener_profiles(), None)
    check = next(item for item in checks.items if item["id"] == "listener.stopped.terraria.7777")
    assert check["ok"] is False
    assert check["reason"] == "must_be_stopped"


def test_verifier_rejects_retired_project_zomboid_listener(monkeypatch):
    monkeypatch.setattr(VERIFY, "_parse_ss", lambda: {("udp", 16261, "0.0.0.0")})
    checks = VERIFY.Checks()
    VERIFY._check_listeners(checks, _listener_profiles(), None)
    present = next(item for item in checks.items if item["id"] == "listener.retired.pz.16261")
    assert present["ok"] is False
    assert present["reason"] == "retired_listener_present"


def test_verifier_requires_exactly_one_public_lazymc_listener(monkeypatch):
    monkeypatch.setattr(VERIFY, "_systemd_active", lambda unit: unit == "lazymc-minecraft.service")
    monkeypatch.setattr(
        VERIFY,
        "_parse_ss",
        lambda: {
            ("tcp", 25565, "0.0.0.0"),
            ("tcp", 25565, "[::]"),
        },
    )
    checks = VERIFY.Checks()
    VERIFY._check_listeners(checks, _listener_profiles(), None)
    check = next(item for item in checks.items if item["id"] == "listener.lazymc.public.25565")
    assert check["ok"] is False
    assert check["reason"] == "public_proxy_listener_ownership_invalid"


def test_verifier_rejects_non_public_lazymc_listener(monkeypatch):
    monkeypatch.setattr(VERIFY, "_systemd_active", lambda unit: unit == "lazymc-minecraft.service")
    monkeypatch.setattr(VERIFY, "_parse_ss", lambda: {("tcp", 25565, "127.0.0.1")})
    checks = VERIFY.Checks()
    VERIFY._check_listeners(checks, _listener_profiles(), None)
    check = next(item for item in checks.items if item["id"] == "listener.lazymc.public.25565")
    assert check["ok"] is False
    assert check["reason"] == "public_proxy_listener_ownership_invalid"


class _PerfResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_verifier_accepts_status_perf_p50_under_budget(monkeypatch):
    monkeypatch.setattr(VERIFY, "_load_proxy_credential", lambda: "synthetic-proxy-token")
    monkeypatch.setattr(
        VERIFY.urllib.request,
        "urlopen",
        lambda request, timeout: _PerfResponse({"GET /api/v1/status": {"count": 50, "p50_ms": 149.9}}),
    )
    checks = VERIFY.Checks()

    VERIFY._check_status_perf(checks)

    check = next(item for item in checks.items if item["id"] == "perf.status_p50_ms")
    assert check["ok"] is True
    assert check["actual"] == 149.9


def test_verifier_rejects_status_perf_p50_at_or_over_budget(monkeypatch):
    monkeypatch.setattr(VERIFY, "_load_proxy_credential", lambda: "synthetic-proxy-token")
    monkeypatch.setattr(
        VERIFY.urllib.request,
        "urlopen",
        lambda request, timeout: _PerfResponse({"GET /api/v1/status": {"count": 50, "p50_ms": 150.0}}),
    )
    checks = VERIFY.Checks()

    VERIFY._check_status_perf(checks)

    check = next(item for item in checks.items if item["id"] == "perf.status_p50_ms")
    assert check["ok"] is False
    assert check["reason"] == "budget_exceeded"


def test_verifier_accepts_deployed_systemd_adapter_contract(monkeypatch):
    monkeypatch.setattr(VERIFY, "WEB_SOURCE", ROOT / "src" / "game_control")
    checks = VERIFY.Checks()

    VERIFY._check_systemd_adapter_contract(checks)

    check = next(item for item in checks.items if item["id"] == "adapter.systemd.contract")
    assert check["ok"] is True


def test_verifier_rejects_incomplete_systemd_adapter_contract(monkeypatch, tmp_path):
    source = tmp_path / "game_control/adapters"
    source.mkdir(parents=True)
    (source / "systemd.py").write_text("class SystemdAdapter: pass\n", encoding="utf-8")
    monkeypatch.setattr(VERIFY, "WEB_SOURCE", tmp_path / "game_control")
    checks = VERIFY.Checks()

    VERIFY._check_systemd_adapter_contract(checks)

    check = next(item for item in checks.items if item["id"] == "adapter.systemd.contract")
    assert check["ok"] is False


def test_unit_inventory_ignores_symlink_aliases_and_unrelated_regular_units(tmp_path):
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    for name in VERIFY.EXPECTED_UNIT_FILES:
        (units / name).write_text("[Unit]\n", encoding="utf-8")
    (units / "dbus-org.freedesktop.timesync1.service").symlink_to("game-slotd.service")
    (units / "sshd.service").symlink_to("game-control-web.service")

    assert VERIFY._direct_regular_unit_names(units) == VERIFY.EXPECTED_UNIT_FILES

    unrelated = units / "dbus-org.freedesktop.timesync1.service"
    unrelated.unlink()
    unrelated.write_text("[Unit]\n", encoding="utf-8")
    assert VERIFY._direct_regular_unit_names(units) == VERIFY.EXPECTED_UNIT_FILES | {unrelated.name}
    assert VERIFY._horizon_owned_unit_names(VERIFY._direct_regular_unit_names(units)) == VERIFY.EXPECTED_UNIT_FILES


def test_unit_inventory_rejects_unexpected_horizon_owned_units(tmp_path):
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    for name in VERIFY.EXPECTED_UNIT_FILES:
        (units / name).write_text("[Unit]\n", encoding="utf-8")
    unexpected = units / "horizon-mc-gateway-go-test.service"
    unexpected.write_text("[Unit]\n", encoding="utf-8")

    names = VERIFY._direct_regular_unit_names(units)
    assert names is not None
    assert VERIFY._horizon_owned_unit_names(names) == VERIFY.EXPECTED_UNIT_FILES | {unexpected.name}


def test_unit_inventory_fails_closed_when_directory_or_entry_is_unreadable(monkeypatch, tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    assert VERIFY._direct_regular_unit_names(units) == set()

    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda _path: (_ for _ in ()).throw(OSError("unreadable")))
    assert VERIFY._direct_regular_unit_names(units) is None
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    regular = units / "game-slotd.service"
    regular.write_text("[Unit]\n", encoding="utf-8")
    original_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda _path: (_ for _ in ()).throw(OSError("unreadable")))
    assert VERIFY._direct_regular_unit_names(units) is None
    monkeypatch.setattr(Path, "lstat", original_lstat)


def test_target_package_reports_unavailable_unit_inventory_without_exception(monkeypatch):
    monkeypatch.setattr(VERIFY, "_direct_regular_unit_names", lambda _directory: None)
    checks = VERIFY.Checks()

    VERIFY._check_target_package(checks)

    unit_check = next(item for item in checks.items if item["id"] == "target.units.manifest")
    assert unit_check["ok"] is False
    assert unit_check["reason"] == "unit_manifest_unavailable"



def test_verifier_main_emits_full_security_contract_and_46_checks(monkeypatch, tmp_path, capsys):
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    for profile_id in VERIFY.PROFILE_IDS:
        (profiles_dir / f"{profile_id}.toml").write_text(
            f'id = "{profile_id}"\nadapter = "systemd"\nsystemd_unit = "{VERIFY.PROFILE_UNITS[profile_id]}"\nports = []\n',
            encoding="utf-8",
        )
    runners_dir = tmp_path / "runner.d"
    runners_dir.mkdir()
    sunlit_runner = {
        "user": "svc-sunlit",
        "group": "svc-sunlit",
        "cwd": "/srv/game-servers/minecraft-sunlit-cobblemon-current",
        "argv": [
            "/usr/bin/java",
            "-Duser.home=/srv/game-servers/minecraft-sunlit-cobblemon-state/local",
            "@/srv/game-servers/minecraft-sunlit-cobblemon-current/user_jvm_args.txt",
            "@/opt/game-servers/minecraft-sunlit-cobblemon/libraries/net/minecraftforge/forge/1.20.1-47.4.0/unix_args.txt",
            "nogui",
        ],
        "environment": {
            "HOME": "/srv/game-servers/minecraft-sunlit-cobblemon-state/local",
        },
    }
    for profile_id in VERIFY.PROFILE_IDS:
        runner = sunlit_runner if profile_id == "minecraft-sunlit-cobblemon" else {
            "user": "terraria-vanilla" if profile_id == "terraria-vanilla" else "tmodloader",
            "group": "terraria-vanilla" if profile_id == "terraria-vanilla" else "tmodloader",
            "cwd": f"/srv/game-servers/{profile_id}",
            "argv": ["/usr/bin/true"],
            "environment": {},
        }
        (runners_dir / f"{profile_id}.json").write_text(json.dumps(runner), encoding="utf-8")
    (tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon").mkdir(parents=True)
    (tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon/libraries").symlink_to(
        "/opt/game-servers/minecraft-sunlit-cobblemon/libraries"
    )
    release = tmp_path / "opt/game-servers/minecraft-sunlit-cobblemon/releases/1.1.2-SSV4.1.4"
    release.mkdir(parents=True)
    state = tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon-state"
    state.mkdir()
    state.chmod(0o750)
    (tmp_path / "srv/game-servers/minecraft-sunlit-cobblemon-current").symlink_to(
        "../../opt/game-servers/minecraft-sunlit-cobblemon/releases/1.1.2-SSV4.1.4",
        target_is_directory=True,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_dir.chmod(0o755)

    state_db = tmp_path / "state.db"
    web_db = tmp_path / "web.db"

    def _create_database(path, tables, triggers=()):
        database = VERIFY.sqlite3.connect(path)
        for table in tables:
            database.execute(f'CREATE TABLE "{table}" (id INTEGER)')
        for trigger in triggers:
            table = "audit" if trigger.startswith("audit_") else "events"
            operation = "DELETE" if trigger.endswith("_delete") else "UPDATE"
            database.execute(
                f'''CREATE TRIGGER "{trigger}" BEFORE {operation} ON "{table}"
                    BEGIN SELECT RAISE(ABORT, 'append only'); END'''
            )
        database.commit()
        database.close()

    _create_database(state_db, VERIFY.EXPECTED_STATE_TABLES, VERIFY.EXPECTED_APPEND_ONLY)
    _create_database(web_db, {"web_sessions"})

    monkeypatch.setattr(VERIFY, "PROFILES", profiles_dir)
    monkeypatch.setattr(VERIFY, "RUNNERS", runners_dir)
    monkeypatch.setattr(VERIFY, "TARGET_ROOT", tmp_path)
    monkeypatch.setattr(VERIFY, "RUN", run_dir)
    monkeypatch.setattr(VERIFY, "STATE_DB", state_db)
    monkeypatch.setattr(VERIFY, "WEB_DB", web_db)
    monkeypatch.setattr(VERIFY, "WEB_SOURCE", ROOT / "src" / "game_control")
    monkeypatch.setattr(VERIFY, "DEPLOYED_PYTHON", tmp_path / "missing-venv-python")
    monkeypatch.setattr(
        VERIFY,
        "_systemd_active",
        lambda unit: unit in VERIFY.EXPECTED_SERVICES,
    )
    monkeypatch.setattr(VERIFY, "_controller_owned_profile", lambda: None)
    monkeypatch.setattr(
        VERIFY,
        "_parse_ss",
        lambda: {
            ("tcp", 8444, "192.0.2.10"),
        },
    )
    monkeypatch.setattr(VERIFY, "_gid", lambda _name: 0)
    monkeypatch.setattr(VERIFY, "_owner_mode", lambda *args, **kwargs: True)
    monkeypatch.setattr(VERIFY, "_load_proxy_credential", lambda: "synthetic-proxy-token")
    effective_controls = {
        "games.slice": {
            "CPUAccounting": "yes", "CPUWeight": "200", "IOAccounting": "yes",
            "MemoryAccounting": "yes", "MemoryHigh": "9663676416",
            "MemoryMax": "10737418240", "MemorySwapMax": "0",
        },
        "minecraft-sunlit-cobblemon.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "MemoryHigh": "8589934592", "MemoryMax": "9663676416",
            "MemorySwapMax": "0", "Slice": "games.slice",
        },
        "game-slotd.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "Slice": "horizon.slice", "ControlGroup": "/horizon.slice/game-slotd.service",
        },
        "game-control-web.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "Slice": "horizon.slice", "ControlGroup": "/horizon.slice/game-control-web.service",
        },
        "terraria-vanilla.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "MemorySwapMax": "0", "Slice": "games.slice",
        },
        "terraria-tmod.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "MemorySwapMax": "0", "Slice": "games.slice",
        },
    }
    monkeypatch.setattr(VERIFY, "_systemd_properties", lambda unit, _properties: effective_controls[unit])
    monkeypatch.setattr(VERIFY, "_active_block_schedulers", lambda: ("none",))
    monkeypatch.setattr(
        VERIFY,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="ok\n"),
    )

    class _StatusResponse:
        status = 403

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _urlopen(request, timeout):
        del timeout
        if request.full_url == VERIFY.PERF_API_URL:
            return _PerfResponse({"GET /api/v1/status": {"count": 50, "p50_ms": 149.9}})
        return _StatusResponse()

    monkeypatch.setattr(VERIFY.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(
        VERIFY,
        "_check_peer_rejection",
        lambda checks: checks.add("rpc.peer_rejection", True),
    )

    assert VERIFY.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_count"] == 56
    systemd_check = next(
        item for item in payload["checks"] if item["id"] == "adapter.systemd.contract"
    )
    assert systemd_check["ok"] is True
    retired_dependencies = next(
        item for item in payload["checks"] if item["id"] == "profiles.retired_dependencies_absent"
    )
    assert retired_dependencies["ok"] is True


def _staged_root(tmp_path: Path) -> Path:
    root = tmp_path / "target"
    Installer(root, skip_systemd_verify=True).apply()
    return root


def test_static_verifier_accepts_complete_vm_target_root(tmp_path, capsys):
    root = _staged_root(tmp_path)
    assert VERIFY.main(["--root", str(root), "--static"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["check_count"] == 70
    assert {item["id"] for item in payload["checks"]} >= {
        "target.symlink.srv.game-servers.minecraft-sunlit-cobblemon.libraries",
        "target.unit.minecraft-sunlit-cobblemon",
        "target.platform_controls",
    }


def test_static_verifier_rejects_undeclared_phase2_helper(tmp_path, capsys):
    root = _staged_root(tmp_path)
    path = root / "usr/local/libexec/horizon-phase2-collect"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["target.libexec.manifest"]["ok"] is False


def test_static_verifier_rejects_retired_manifest_artifact(tmp_path, capsys):
    root = _staged_root(tmp_path)
    (root / "etc/game-control/profiles.d/pz-rising.toml").write_text("id = 'pz-rising'\n", encoding="utf-8")
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {item["id"]: item for item in payload["checks"]}
    assert checks["target.legacy_artifacts_absent"]["ok"] is False
    assert checks["target.profiles.manifest"]["ok"] is False


def test_static_verifier_rejects_sunlit_launch_bridge_drift(tmp_path, capsys):
    root = _staged_root(tmp_path)
    link = root / "srv/game-servers/minecraft-sunlit-cobblemon/libraries"
    link.unlink()
    link.symlink_to("/tmp/incorrect-libraries")
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    check = next(item for item in payload["checks"] if item["id"].startswith("target.symlink."))
    assert check["ok"] is False


def test_static_verifier_uses_canonical_manifest_and_ignores_target_manifests(tmp_path, capsys):
    root = _staged_root(tmp_path)
    runtime_manifest = root / "opt/game-control/.horizon-runtime-manifest"
    runtime_manifest.write_text("1\t../escape\t" + "0" * 64 + "\t0600\n", encoding="ascii")
    assert VERIFY.main(["--root", str(root), "--static"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert next(item for item in payload["checks"] if item["id"] == "target.files.manifest")["ok"]


def test_static_verifier_reports_manifest_file_metadata_drift(tmp_path, capsys):
    root = _staged_root(tmp_path)
    target = root / "etc/game-control/game-control.toml"
    target.chmod(0o644)
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    check = next(item for item in payload["checks"] if item["id"] == "target.files.manifest")
    assert check["ok"] is False


@pytest.mark.parametrize("mutation", ("mode", "owner", "group", "nlink", "symlink", "type"))
def test_static_verifier_rejects_directory_metadata_drift(tmp_path, capsys, mutation):
    root = _staged_root(tmp_path)
    target = root / "etc/game-control/arm"
    if mutation == "mode":
        target.chmod(0o755)
    elif mutation == "owner":
        os.chown(target, 65534, target.stat().st_gid)
    elif mutation == "group":
        os.chown(target, target.stat().st_uid, 65534)
    elif mutation == "nlink":
        (target / "unexpected-child").mkdir()
    elif mutation == "symlink":
        target.rmdir()
        target.symlink_to("elsewhere", target_is_directory=True)
    else:
        target.rmdir()
        target.write_text("not a directory", encoding="utf-8")
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    check = next(item for item in payload["checks"] if item["id"] == "target.directory.etc.game-control.arm")
    assert check["ok"] is False


def test_static_verifier_allows_declared_tmpfiles_children_and_rejects_unknown(tmp_path, capsys):
    root = _staged_root(tmp_path)
    for relative in (
        "run/game-control/operation.lock",
        "run/game-control/slot.lock",
        "run/game-control/reservation.json",
        "run/game-slot/slot.json",
    ):
        path = root / relative
        path.write_text("fixture\n", encoding="utf-8")
    assert VERIFY.main(["--root", str(root), "--static"]) == 0
    capsys.readouterr()
    (root / "run/game-control/undeclared-child").write_text("unexpected\n", encoding="utf-8")
    assert VERIFY.main(["--root", str(root), "--static"]) == 1
    payload = json.loads(capsys.readouterr().out)
    check = next(item for item in payload["checks"] if item["id"] == "target.directory.run.game-control")
    assert check["ok"] is False


def test_static_ownership_projection_ignores_host_accounts_for_staged_root(tmp_path, monkeypatch, capsys):
    root = _staged_root(tmp_path)
    monkeypatch.setattr(VERIFY.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=1234))
    monkeypatch.setattr(VERIFY.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=1234))
    assert VERIFY.main(["--root", str(root), "--static"]) == 0
    capsys.readouterr()


def test_verifier_accepts_updated_sunlit_release_pointer(tmp_path, monkeypatch):
    root = tmp_path
    runners = root / "etc/game-control/runner.d"
    runners.mkdir(parents=True)
    sunlit_runner = {
        "user": "svc-sunlit",
        "group": "svc-sunlit",
        "cwd": "/srv/game-servers/minecraft-sunlit-cobblemon-current",
        "argv": [
            "/usr/bin/java",
            "-Duser.home=/srv/game-servers/minecraft-sunlit-cobblemon-state/local",
            "@/srv/game-servers/minecraft-sunlit-cobblemon-current/user_jvm_args.txt",
            "@/opt/game-servers/minecraft-sunlit-cobblemon/libraries/net/minecraftforge/forge/1.20.1-47.4.0/unix_args.txt",
            "nogui",
        ],
        "environment": {"HOME": "/srv/game-servers/minecraft-sunlit-cobblemon-state/local"},
    }
    (runners / "minecraft-sunlit-cobblemon.json").write_text(
        json.dumps(sunlit_runner), encoding="utf-8"
    )
    release = root / "opt/game-servers/minecraft-sunlit-cobblemon/releases/1.1.3-SSV4.1.4"
    release.mkdir(parents=True)
    state = root / "srv/game-servers/minecraft-sunlit-cobblemon-state"
    state.mkdir(parents=True)
    state.chmod(0o750)
    current = root / "srv/game-servers/minecraft-sunlit-cobblemon-current"
    current.symlink_to(
        "../../opt/game-servers/minecraft-sunlit-cobblemon/releases/1.1.3-SSV4.1.4",
        target_is_directory=True,
    )
    monkeypatch.setattr(VERIFY, "TARGET_ROOT", root)
    monkeypatch.setattr(VERIFY, "RUNNERS", runners)
    profiles = {
        profile_id: {"adapter": "systemd", "systemd_unit": VERIFY.PROFILE_UNITS[profile_id]}
        for profile_id in VERIFY.PROFILE_IDS
    }
    checks = VERIFY.Checks()

    VERIFY._check_target_registry(checks, profiles)

    launch = next(item for item in checks.items if item["id"] == "profile.sunlit.launch_layout")
    assert launch["ok"] is True


def test_verifier_reports_effective_approved_controls(monkeypatch):
    values = {
        "games.slice": {
            "CPUAccounting": "yes",
            "CPUWeight": "200",
            "IOAccounting": "yes",
            "MemoryAccounting": "yes",
            "MemoryHigh": "9663676416",
            "MemoryMax": "10737418240",
            "MemorySwapMax": "0",
        },
        "minecraft-sunlit-cobblemon.service": {
            "CPUAccounting": "yes",
            "IOAccounting": "yes",
            "MemoryAccounting": "yes",
            "MemoryHigh": "8589934592",
            "MemoryMax": "9663676416",
            "MemorySwapMax": "0",
            "Slice": "games.slice",
        },
        "game-slotd.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "Slice": "horizon.slice", "ControlGroup": "/horizon.slice/game-slotd.service",
        },
        "game-control-web.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "Slice": "horizon.slice", "ControlGroup": "/horizon.slice/game-control-web.service",
        },
        "terraria-vanilla.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "MemorySwapMax": "0", "Slice": "games.slice",
        },
        "terraria-tmod.service": {
            "CPUAccounting": "yes", "IOAccounting": "yes", "MemoryAccounting": "yes",
            "MemorySwapMax": "0", "Slice": "games.slice",
        },
    }
    monkeypatch.setattr(VERIFY, "_systemd_properties", lambda unit, _properties: values[unit])
    checks = VERIFY.Checks()

    VERIFY._check_effective_controls(checks)

    assert not checks.failed
    assert {item["id"] for item in checks.items} == {
        "effective.games_slice",
        "effective.minecraft-sunlit-cobblemon",
        "effective.game-slotd",
        "effective.game-control-web",
        "effective.terraria-vanilla",
        "effective.terraria-tmod",
    }


def test_verifier_reports_effective_control_gap(monkeypatch):
    monkeypatch.setattr(
        VERIFY,
        "_systemd_properties",
        lambda _unit, _properties: {"CPUAccounting": "no"},
    )
    checks = VERIFY.Checks()

    VERIFY._check_effective_controls(checks)

    assert all(item["reason"] == "effective_control_gap" for item in checks.items)


def test_verifier_reports_none_scheduler_as_best_effort(monkeypatch):
    monkeypatch.setattr(VERIFY, "_active_block_schedulers", lambda: ("none",))
    checks = VERIFY.Checks()

    VERIFY._check_block_schedulers(checks)

    assert checks.items == [{
        "id": "effective.block_schedulers",
        "ok": True,
        "reason": "best_effort_no_ionice",
        "actual": ["none"],
    }]


def test_verifier_marks_unreadable_scheduler_inventory_unavailable(monkeypatch):
    monkeypatch.setattr(VERIFY, "_active_block_schedulers", lambda: None)
    checks = VERIFY.Checks()

    VERIFY._check_block_schedulers(checks)

    assert checks.failed is True
    assert checks.missing_privilege is True
    assert checks.items[0]["reason"] == "missing_privilege"
