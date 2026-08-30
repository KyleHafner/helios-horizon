"""Best-effort priority controls for bounded maintenance subprocesses."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Iterable, Sequence


NICE_LEVEL = 10
IONICE_CLASS = 2  # best-effort
IONICE_LEVEL = 7
RCLONE_BWLIMIT = "8M"


def active_block_schedulers(paths: Iterable[Path] | None = None) -> tuple[str, ...] | None:
    """Return active scheduler names, or None for incomplete inventory."""
    candidates = tuple(paths) if paths is not None else tuple(sorted(Path("/sys/block").glob("*/queue/scheduler")))
    if not candidates:
        return None
    names: list[str] = []
    for path in candidates:
        try:
            value = path.read_text(encoding="ascii")
        except OSError:
            return None
        active = next((part.strip("[]") for part in value.split() if part.startswith("[")), None)
        if active is None:
            return None
        names.append(active)
    return tuple(names)


def _ionice_supported(schedulers: Sequence[str] | None, executable: str = "/usr/bin/ionice") -> bool:
    return bool(schedulers) and all(name != "none" for name in schedulers) and bool(shutil.which(executable))


def maintenance_argv(
    argv: Sequence[str],
    *,
    schedulers: Sequence[str] | None = None,
    ionice_executable: str = "/usr/bin/ionice",
    slice_name: str | None = None,
) -> list[str]:
    """Add conservative controls to one fixed maintenance command.

    Missing ionice or an unsupported ``none`` scheduler degrades to nice only.
    The helper never uses a shell and never applies to game-server commands.
    """
    command = list(argv)
    if not command or any(not isinstance(item, str) or not item for item in command):
        raise ValueError("maintenance command must be a non-empty argv")
    executable = Path(command[0]).name
    if executable == "rclone" and "--bwlimit" not in command:
        command[1:1] = ["--bwlimit", RCLONE_BWLIMIT]
    prefix = ["/usr/bin/nice", "-n", str(NICE_LEVEL)]
    active = active_block_schedulers() if schedulers is None else tuple(schedulers)
    if _ionice_supported(active, ionice_executable):
        prefix.extend([ionice_executable, "-c", str(IONICE_CLASS), "-n", str(IONICE_LEVEL)])
    wrapped = prefix + command
    if slice_name is None:
        return wrapped
    if slice_name != "maintenance.slice":
        raise ValueError("unsupported maintenance slice")
    token = uuid.uuid4().hex[:16]
    return [
        "/usr/bin/systemd-run", "--wait", "--pipe", "--quiet",
        "--service-type=exec", "--slice=maintenance.slice", f"--unit=horizon-maint-{token}",
        "--property=KillMode=control-group", "--property=TimeoutStopSec=900s", "--",
        *wrapped,
    ]


class ManagedMaintenanceProcess:
    """Client process plus explicit transient-unit cleanup boundary."""

    def __init__(self, client, unit: str, *, controller=None, verifier=None):
        self.client = client
        self.unit = unit
        self.controller = controller or subprocess.run
        self.verifier = verifier or self._verify_terminal
        self._cleaned = False
        self.stdout = getattr(client, "stdout", None)
        self.stderr = getattr(client, "stderr", None)
        self.args = getattr(client, "args", None)

    def _stop_reset(self) -> None:
        if self._cleaned:
            return
        for action in ("stop", "reset-failed"):
            try:
                self.controller(
                    ["/usr/bin/systemctl", action, self.unit], check=False,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except Exception:
                # Cleanup is best effort when the local systemd client is already
                # unavailable; the managed client is still reaped by the caller.
                continue
        self._cleaned = True

    def _verify_terminal(self) -> None:
        local_code = getattr(self.client, "returncode", None)
        if local_code not in (None, 0):
            return
        result = self.controller(
            [
                "/usr/bin/systemctl", "show", self.unit,
                "--no-legend", "--property=ExecMainStatus", "--property=Result",
            ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("maintenance unit terminal state unavailable")
        values = dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )
        try:
            status = int(values["ExecMainStatus"])
        except (KeyError, ValueError):
            raise RuntimeError("maintenance unit terminal status unavailable")
        if values.get("Result") != "success" or status != 0:
            raise RuntimeError("maintenance unit terminal result unavailable")

    def terminate(self) -> None:
        self._stop_reset()
        terminate = getattr(self.client, "terminate", None)
        if terminate is not None:
            terminate()

    def kill(self) -> None:
        self._stop_reset()
        kill = getattr(self.client, "kill", None)
        if kill is not None:
            kill()

    def poll(self):
        return self.client.poll()

    def wait(self, timeout=None):
        try:
            result = self.client.wait(timeout=timeout)
            if getattr(self.client, "returncode", result) == 0:
                self.verifier()
            self._stop_reset()
            return result
        except Exception:
            self._stop_reset()
            raise

    def communicate(self, *args, **kwargs):
        try:
            result = self.client.communicate(*args, **kwargs)
            self.verifier()
            self._stop_reset()
            return result
        except Exception:
            self._stop_reset()
            raise

    def __enter__(self):
        enter = getattr(self.client, "__enter__", None)
        if enter is not None:
            enter()
        return self

    def __exit__(self, *args):
        if getattr(self.client, "poll", lambda: None)() is None:
            self.terminate()
        else:
            self._stop_reset()
        exit_method = getattr(self.client, "__exit__", None)
        return exit_method(*args) if exit_method is not None else False


def maintenance_popen(argv: Sequence[str], *, popen=None, controller=None, verifier=None, **kwargs):
    popen = popen or subprocess.Popen
    controller = controller or subprocess.run
    wrapped = maintenance_argv(argv, slice_name="maintenance.slice")
    unit = next(value.split("=", 1)[1] for value in wrapped if value.startswith("--unit="))
    return ManagedMaintenanceProcess(popen(wrapped, **kwargs), unit, controller=controller, verifier=verifier)


__all__ = [
    "IONICE_CLASS",
    "IONICE_LEVEL",
    "NICE_LEVEL",
    "RCLONE_BWLIMIT",
    "active_block_schedulers",
    "maintenance_argv",
    "maintenance_popen",
    "ManagedMaintenanceProcess",
]
