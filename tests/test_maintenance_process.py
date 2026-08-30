import importlib.util
from pathlib import Path
import subprocess

import pytest

from game_control import maintenance_process as control


def test_old_interim_module_has_no_compatibility_alias() -> None:
    assert importlib.util.find_spec("game_control.interim_maintenance_control") is None


def test_scheduler_probe_reports_active_bracketed_name(tmp_path: Path):
    scheduler = tmp_path / "scheduler"
    scheduler.write_text("none [mq-deadline] kyber\n", encoding="ascii")

    assert control.active_block_schedulers((scheduler,)) == ("mq-deadline",)


def test_scheduler_probe_marks_partial_inventory_unavailable(tmp_path: Path):
    readable = tmp_path / "readable"
    readable.write_text("none [mq-deadline]\n", encoding="ascii")

    assert control.active_block_schedulers((readable, tmp_path / "missing")) is None


def test_partial_scheduler_inventory_degrades_to_nice_only(monkeypatch):
    monkeypatch.setattr(control, "active_block_schedulers", lambda: None)
    monkeypatch.setattr(control.shutil, "which", lambda _path: "/usr/bin/ionice")

    argv = control.maintenance_argv(["/opt/steamcmd/steamcmd.sh", "+quit"])

    assert argv == ["/usr/bin/nice", "-n", "10", "/opt/steamcmd/steamcmd.sh", "+quit"]


def test_none_scheduler_degrades_to_nice_only_and_bounds_rclone(monkeypatch):
    monkeypatch.setattr(control.shutil, "which", lambda _path: "/usr/bin/ionice")

    argv = control.maintenance_argv(
        ["/usr/bin/rclone", "--config", "/root/secret", "copyto", "a", "b"],
        schedulers=("none",),
    )

    assert argv == [
        "/usr/bin/nice", "-n", "10", "/usr/bin/rclone", "--bwlimit", "8M",
        "--config", "/root/secret", "copyto", "a", "b",
    ]
    assert "--shell" not in argv


def test_supported_scheduler_adds_best_effort_ionice(monkeypatch):
    monkeypatch.setattr(control.shutil, "which", lambda _path: "/usr/bin/ionice")

    argv = control.maintenance_argv(
        ["/opt/steamcmd/steamcmd.sh", "+quit"],
        schedulers=("mq-deadline",),
    )

    assert argv[:8] == [
        "/usr/bin/nice", "-n", "10", "/usr/bin/ionice", "-c", "2", "-n", "7",
    ]
    assert argv[8:] == ["/opt/steamcmd/steamcmd.sh", "+quit"]


def test_invalid_maintenance_argv_fails_closed():
    with pytest.raises(ValueError):
        control.maintenance_argv([])
    with pytest.raises(ValueError):
        control.maintenance_argv(["/usr/bin/rclone", ""])


def test_transient_units_are_unique_for_concurrent_attempts(monkeypatch):
    monkeypatch.setattr(control, "active_block_schedulers", lambda: ("none",))
    first = control.maintenance_argv(["/usr/bin/tar", "--create"], slice_name="maintenance.slice")
    second = control.maintenance_argv(["/usr/bin/tar", "--create"], slice_name="maintenance.slice")

    first_unit = next(item for item in first if item.startswith("--unit="))
    second_unit = next(item for item in second if item.startswith("--unit="))
    assert first_unit != second_unit
    assert first_unit.split("=", 1)[1].startswith("horizon-maint-")


class _Client:
    args = ["systemd-run"]
    stdout = None
    stderr = None

    def __init__(self, *, wait_error=None):
        self.wait_error = wait_error
        self.calls = []
        self.returncode = 0

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.wait_error:
            raise self.wait_error
        return self.returncode

    def communicate(self, *args, **kwargs):
        self.calls.append(("communicate", kwargs))
        return "", ""


def test_managed_process_stops_and_resets_orphan_on_terminal_failure():
    controller_calls = []

    def controller(argv, **kwargs):
        controller_calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    client = _Client()
    process = control.ManagedMaintenanceProcess(
        client, "horizon-maint-opaque", controller=controller,
        verifier=lambda: (_ for _ in ()).throw(RuntimeError("orphan")),
    )
    with pytest.raises(RuntimeError, match="orphan"):
        process.wait(timeout=3)
    assert controller_calls == [
        ["/usr/bin/systemctl", "stop", "horizon-maint-opaque"],
        ["/usr/bin/systemctl", "reset-failed", "horizon-maint-opaque"],
    ]


def test_managed_process_timeout_and_cancel_cleanup_remote_unit():
    controller_calls = []

    def controller(argv, **kwargs):
        controller_calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    client = _Client(wait_error=subprocess.TimeoutExpired(["systemd-run"], 1))
    process = control.ManagedMaintenanceProcess(
        client, "horizon-maint-timeout", controller=controller, verifier=lambda: None,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    process.terminate()
    process.kill()
    assert client.calls == [("wait", 1), "terminate", "kill"]
    assert [call[1] for call in controller_calls].count("stop") == 1
    assert [call[1] for call in controller_calls].count("reset-failed") == 1


def test_managed_success_verifies_status_before_removing_unit():
    controller_calls = []

    def controller(argv, **kwargs):
        controller_calls.append(argv)
        if argv[1] == "show":
            return type("Result", (), {
                "returncode": 0, "stdout": "ExecMainStatus=0\nResult=success\n", "stderr": "",
            })()
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    client = _Client()
    process = control.ManagedMaintenanceProcess(client, "horizon-maint-success", controller=controller)
    assert process.wait() == 0
    assert controller_calls[0][1] == "show"
    assert [call[1] for call in controller_calls[1:]] == ["stop", "reset-failed"]


def test_context_exit_cleans_running_unit():
    controller_calls = []

    def controller(argv, **kwargs):
        controller_calls.append(argv)
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    client = _Client()
    client.poll = lambda: None
    with control.ManagedMaintenanceProcess(client, "horizon-maint-context", controller=controller):
        pass
    assert client.calls == ["terminate"]
    assert [call[1] for call in controller_calls] == ["stop", "reset-failed"]
