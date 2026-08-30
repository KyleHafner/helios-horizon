from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools.acceptance.memory_pressure_drill import (
    ALLOCATOR_SCRIPT,
    FIXTURE_SLICE,
    MemoryDrillError,
    _interpret,
    _limits_match,
    _poll_loaded,
    build_argv,
    run_drill,
    validate_target,
)


def test_refuses_production_targets_and_non_fixture_limits():
    for kwargs in (
        {"profile_id": "minecraft-sunlit-cobblemon"},
        {"unit": "minecraft-sunlit-cobblemon.service"},
        {"cgroup": "/games.slice/minecraft"},
        {"unit": "horizon-memory-drill-test.service", "cgroup": "/system.slice/game-control-web.service"},
    ):
        with pytest.raises(MemoryDrillError):
            validate_target(**kwargs)
    with pytest.raises(MemoryDrillError):
        build_argv(16, 48, "horizon-memory-drill-test.service")
    with pytest.raises(MemoryDrillError):
        build_argv(3, 81, "horizon-memory-drill-test.service")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_duration(value):
    with pytest.raises(MemoryDrillError):
        build_argv(value, 48, "horizon-memory-drill-test.service")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_rejects_non_finite_memory(value):
    with pytest.raises(MemoryDrillError):
        build_argv(3, value, "horizon-memory-drill-test.service")


@pytest.mark.parametrize("value", [True, False, 1.0, 48.0])
def test_rejects_non_strict_integer_memory(value):
    with pytest.raises(MemoryDrillError):
        build_argv(3, value, "horizon-memory-drill-test.service")


@pytest.mark.parametrize(
    "unit",
    [
        "horizon-memory-drill-../../foo.service",
        "horizon-memory-drill-test.service.extra",
        "horizon-memory-drill-test..service",
        "horizon-memory-drill-.service",
        "horizon-memory-drill-" + "a" * 129 + ".service",
    ],
)
def test_rejects_traversal_and_unit_grammar_abuse(unit):
    with pytest.raises(MemoryDrillError):
        validate_target(unit=unit)


def test_argv_is_allowlisted_and_never_starts_game():
    argv = build_argv(3, 48, "horizon-memory-drill-test.service")
    assert argv[0:6] == ["/usr/bin/systemd-run", "--no-block", "--quiet", "--service-type=exec", "--remain-after-exit", "--unit=horizon-memory-drill-test.service"]
    assert f"--slice={FIXTURE_SLICE}" in argv
    assert "--property=MemoryHigh=64M" in argv
    assert "--property=MemoryMax=96M" in argv
    assert "--property=MemorySwapMax=0" in argv
    assert "--property=OOMPolicy=kill" in argv
    assert "--property=RuntimeMaxSec=20s" in argv
    assert "minecraft" not in " ".join(argv)
    assert "systemctl start" not in " ".join(argv)
    assert argv[-1] == "allocate-and-hold"

    oom = build_argv(10, 80, "horizon-memory-drill-test.service", "oom")
    assert "--property=MemoryHigh=96M" in oom
    assert "--property=MemoryMax=96M" in oom
    assert "--property=RuntimeMaxSec=20s" in oom
    assert oom[-3:] == ["24", "0.25", "allocate-and-hold"]

    timeout = build_argv(3, 48, "horizon-memory-drill-test.service", "timeout")
    assert timeout[-1] == "hold-no-pressure"
    assert timeout[-5] == "29"
    assert "--property=MemoryHigh=96M" in timeout
    assert "--property=RuntimeMaxSec=19s" in timeout
    assert "--property=RuntimeMaxSec=20s" not in timeout


def test_interpretation_distinguishes_success_oom_and_timeout():
    assert _interpret({"Result": "success"}, 0, False) == "success"
    assert _interpret({"Result": "oom-kill", "OOMKilled": "yes"}, 1, False) == "oom"
    assert _interpret({}, 0, True) == "timeout"
    assert _interpret({"Result": "timeout"}, 0, False) == "timeout"
    assert _interpret({"Result": "timeout", "OOMKilled": "yes"}, 137, False) == "oom"
    assert _interpret({"Result": "timeout", "OOMKilled": "yes"}, 137, True) == "oom"
    assert _interpret({"Result": "watchdog", "OOMKilled": "yes"}, 247, True) == "oom"


def test_limits_require_exact_disposable_policy():
    for cgroup in (
        "/horizon-memory-drill.slice/horizon-memory-drill-test.service",
        "/horizon.slice/horizon-memory.slice/horizon-memory-drill.slice/horizon-memory-drill-test.service",
    ):
        good = {"Slice": FIXTURE_SLICE, "ControlGroup": cgroup, "MemoryHigh": "67108864", "MemoryMax": "100663296", "MemorySwapMax": "0", "OOMPolicy": "kill"}
        assert _limits_match(good)
    bad = dict(good, Slice="games.slice")
    assert not _limits_match(bad)
    assert not _limits_match(dict(good, MemoryHigh="67108864"), "oom")


def test_accepts_real_nested_fixed_cgroup_and_rejects_other_nesting():
    validate_target(cgroup="/horizon.slice/horizon-memory.slice/horizon-memory-drill.slice/horizon-memory-drill-test.service")
    with pytest.raises(MemoryDrillError):
        validate_target(cgroup="/horizon.slice/other.slice/horizon-memory-drill.slice/horizon-memory-drill-test.service")


class _FakeSystemd:
    def __init__(self, *, result="oom-kill", active_once=True, active_forever=False, terminal_empty=False, stop_code=0, reset_code=0, reset_stderr="", post_code=0, post_load_state="not-found", post_cgroup="", memory_high="67108864", memory_max="100663296"):
        self.result = result
        self.active_once = active_once
        self.active_forever = active_forever
        self.terminal_empty = terminal_empty
        self.stop_code = stop_code
        self.reset_code = reset_code
        self.reset_stderr = reset_stderr
        self.post_code = post_code
        self.post_load_state = post_load_state
        self.post_cgroup = post_cgroup
        self.memory_high = memory_high
        self.memory_max = memory_max
        self.calls = []
        self.show_count = 0
        self.stopped = False

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0].endswith("systemd-run"):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[0].endswith("systemctl") and argv[1] == "show":
            self.show_count += 1
            active = not self.stopped and (self.active_forever or (self.active_once and self.show_count == 1))
            cgroup = self.post_cgroup if self.stopped else ("" if self.terminal_empty and not active else "/horizon-memory-drill.slice/horizon-memory-drill-test.service")
            load_state = self.post_load_state if self.stopped else ("loaded" if not self.stopped else "not-found")
            text = "Slice=horizon-memory-drill.slice\nControlGroup=%s\nMemoryHigh=%s\nMemoryMax=%s\nMemorySwapMax=0\nOOMPolicy=kill\nActiveState=%s\nSubState=%s\nLoadState=%s\nResult=%s\nOOMKilled=%s\nExecMainStatus=%s\n" % (cgroup, self.memory_high, self.memory_max, "active" if active else "inactive", "running" if active else "exited", load_state, self.result, "yes" if self.result == "oom-kill" else "no", "137" if self.result == "oom-kill" else "0")
            return SimpleNamespace(returncode=self.post_code if self.stopped else 0, stdout=text, stderr="")
        if argv[0].endswith("systemctl") and argv[1] == "stop":
            self.stopped = True
            return SimpleNamespace(returncode=self.stop_code, stdout="", stderr="")
        if argv[0].endswith("systemctl") and argv[1] == "reset-failed":
            stderr = self.reset_stderr
            if stderr == "__exact_not_loaded__":
                stderr = f"Failed to reset failed state of unit {argv[2]}: Unit {argv[2]} not loaded.\n"
            return SimpleNamespace(returncode=self.reset_code, stdout="", stderr=stderr)
        if argv[0].endswith("systemctl") and argv[1] == "daemon-reload":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(argv)


def test_run_drill_oom_success_and_cleanup_without_game_start():
    fake = _FakeSystemd(result="oom-kill", memory_high="100663296", memory_max="100663296")
    evidence = run_drill(memory_mib=80, runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["ok"] is True
    assert evidence["result"] == "oom"
    assert evidence["cleanup_verified"] is True
    assert evidence["game_start_attempted"] is False
    assert not any("game-slotd" in " ".join(call) or "minecraft" in " ".join(call) for call in fake.calls)


def test_live_limits_are_retained_when_terminal_observation_loses_cgroup():
    fake = _FakeSystemd(result="success", terminal_empty=True)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["result"] == "success"
    assert evidence["observed_limits_match"] is True
    assert evidence["ok"] is True


def test_cleanup_accepts_already_unloaded_unit_when_reset_failed_is_not_found():
    fake = _FakeSystemd(result="success", reset_code=5)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["cleanup_verified"] is True
    assert evidence["ok"] is True


def test_cleanup_accepts_show_not_found_with_exit_code_four():
    fake = _FakeSystemd(result="success", post_code=4)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["cleanup_verified"] is True
    assert evidence["ok"] is True


def test_cleanup_accepts_exact_reset_not_loaded_rc1_after_canonical_gone():
    fake = _FakeSystemd(result="success", reset_code=1, reset_stderr="__exact_not_loaded__")
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["cleanup_verified"] is True
    assert evidence["ok"] is True


@pytest.mark.parametrize(
    "reset_stderr,post_load_state,post_cgroup",
    [
        ("Unit other.service not loaded.\n", "not-found", ""),
        ("Unit horizon-memory-drill-test.service failed.\n", "not-found", ""),
        ("Unit horizon-memory-drill-test.service not loaded.\n", "loaded", ""),
        ("Unit horizon-memory-drill-test.service not loaded.\n", "not-found", "/horizon-memory-drill.slice/horizon-memory-drill-test.service"),
    ],
)
def test_cleanup_rejects_nonexact_reset_not_loaded_contract(reset_stderr, post_load_state, post_cgroup):
    fake = _FakeSystemd(result="success", reset_code=1, reset_stderr=reset_stderr, post_load_state=post_load_state, post_cgroup=post_cgroup)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["cleanup_verified"] is False
    assert evidence["ok"] is False


@pytest.mark.parametrize(
    "post_code,post_load_state,post_cgroup",
    [(4, "loaded", ""), (4, "not-found", "/horizon-memory-drill.slice/horizon-memory-drill-test.service"), (3, "not-found", "")],
)
def test_cleanup_rejects_noncanonical_or_unexpected_post_show(post_code, post_load_state, post_cgroup):
    fake = _FakeSystemd(result="success", post_code=post_code, post_load_state=post_load_state, post_cgroup=post_cgroup)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["cleanup_verified"] is False
    assert evidence["ok"] is False


def test_allocator_touches_exact_target_without_artificial_per_mib_throttling():
    assert "while len(chunks) < memory_mib + headroom_mib:" in ALLOCATOR_SCRIPT
    assert "time.sleep(0.05)" not in ALLOCATOR_SCRIPT
    assert "time.sleep(startup_seconds)" in ALLOCATOR_SCRIPT
    assert "time.sleep(duration)" in ALLOCATOR_SCRIPT
    completed = subprocess.run(
        [sys.executable, "-c", ALLOCATOR_SCRIPT, "0.001", "80", "24", "0.001", "allocate-and-hold"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr


def test_run_drill_success_expected():
    fake = _FakeSystemd(result="success")
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["ok"] is True
    assert evidence["result"] == "success"


@pytest.mark.parametrize("memory_mib,expect", [(48, "oom"), (80, "success")])
def test_rejects_mismatched_pressure_preset(memory_mib, expect):
    with pytest.raises(MemoryDrillError):
        run_drill(memory_mib=memory_mib, expect=expect)


def test_run_drill_timeout_cleans_up():
    fake = _FakeSystemd(result="timeout", active_forever=True, memory_high="100663296", memory_max="100663296")
    ticks = iter([0.0, 0.0, 25.0, 25.0, 25.0, 25.0, 25.0, 25.0])
    evidence = run_drill(expect="timeout", runner=fake, clock=lambda: next(ticks, 25.0), sleeper=lambda _seconds: None)
    assert evidence["result"] == "timeout"
    assert evidence["cleanup_verified"] is True
    assert evidence["ok"] is True
    assert evidence["duration_seconds"] <= 25.0
    assert any(call[1] == "stop" for call in fake.calls if len(call) > 1)


def test_launcher_transport_timeout_never_accepts_timeout_preset():
    fake = _FakeSystemd(
        result="timeout",
        active_forever=True,
        memory_high="100663296",
        memory_max="100663296",
    )

    def launcher_timeout(argv, **kwargs):
        if argv[0].endswith("systemd-run"):
            raise subprocess.TimeoutExpired(argv, 5)
        return fake(argv, **kwargs)

    evidence = run_drill(
        expect="timeout",
        runner=launcher_timeout,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )
    assert evidence["transport_timeout"] is True
    assert evidence["observed_limits_match"] is False
    assert evidence["ok"] is False
    assert evidence["cleanup_verified"] is True
    assert evidence["limits"] == {
        "MemoryHigh": "96M",
        "MemoryMax": "96M",
        "MemorySwapMax": "0",
        "OOMPolicy": "kill",
    }
    assert evidence["mode"] == "hold-no-pressure"


def test_timeout_fixture_is_not_memory_pressure():
    argv = build_argv(3, 48, "horizon-memory-drill-test.service", "timeout")
    assert "hold-no-pressure" in argv
    assert argv[-3] == "0"
    assert "--property=MemorySwapMax=0" in argv
    fake = _FakeSystemd(result="timeout", active_forever=True, memory_high="100663296", memory_max="100663296")
    ticks = iter((0.0, 0.0, 25.0, 25.0, 25.0, 25.0))
    evidence = run_drill(expect="timeout", runner=fake, clock=lambda: next(ticks, 25.0), sleeper=lambda _: None)
    assert evidence["mode"] == "hold-no-pressure"
    assert evidence["effective_allocation_mib"] == 0


def test_timeout_allocator_outlives_fixed_service_ceiling():
    argv = build_argv(3, 48, "horizon-memory-drill-test.service", "timeout")
    runtime_limit = int(next(item.removeprefix("--property=RuntimeMaxSec=").removesuffix("s") for item in argv if item.startswith("--property=RuntimeMaxSec=")))
    allocator_duration = int(argv[-5])
    assert runtime_limit == 19
    assert allocator_duration == 29
    assert allocator_duration > runtime_limit


def test_timeout_report_keeps_cleanup_headroom_under_deterministic_clock():
    fake = _FakeSystemd(result="timeout", memory_high="100663296", memory_max="100663296")
    ticks = iter((0.0, 0.0, 19.5, 19.75))
    evidence = run_drill(
        expect="timeout",
        runner=fake,
        clock=lambda: next(ticks, 19.75),
        sleeper=lambda _seconds: None,
    )
    assert evidence["result"] == "timeout"
    assert evidence["cleanup_verified"] is True
    assert evidence["duration_seconds"] == 19.75
    assert evidence["duration_seconds"] <= 20.0


@pytest.mark.parametrize(
    "memory_mib,expect",
    [(48, "oom"), (80, "success"), (80, "timeout"), (48, "invalid")],
)
def test_build_argv_rejects_mismatched_preset(memory_mib, expect):
    with pytest.raises(MemoryDrillError):
        build_argv(3, memory_mib, "horizon-memory-drill-test.service", expect)


def test_run_drill_deadline_equality_terminates_and_cleans_up():
    """A poll observed exactly at its deadline must not spin indefinitely."""
    fake = _FakeSystemd(result="timeout", active_forever=True, memory_high="100663296", memory_max="100663296")
    ticks = iter((0.0, 0.0, 25.0, 25.0, 25.0))
    evidence = run_drill(
        expect="timeout",
        runner=fake,
        clock=lambda: next(ticks, 25.0),
        sleeper=lambda _seconds: None,
    )
    assert evidence["result"] == "timeout"
    assert evidence["cleanup_verified"] is True
    assert evidence["ok"] is True
    assert any(call[1] == "stop" for call in fake.calls if len(call) > 1)


def test_fast_terminal_before_live_identity_fails_closed():
    fake = _FakeSystemd(result="success", active_once=False)
    evidence = run_drill(expect="success", runner=fake, clock=lambda: 0.0, sleeper=lambda _seconds: None)
    assert evidence["result"] == "success"
    assert evidence["observed_limits_match"] is False
    assert evidence["ok"] is False


def test_active_running_is_not_treated_as_terminal():
    states = iter((
        "LoadState=loaded\nActiveState=active\nSubState=running\n",
        "LoadState=loaded\nActiveState=inactive\nSubState=exited\n",
    ))
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=next(states), stderr="")

    ticks = iter((0.0, 0.0, 0.0))
    values = _poll_loaded(runner, "horizon-memory-drill-test.service", lambda: next(ticks), lambda _: None, 1.0)
    assert values["SubState"] == "exited"
    assert len(calls) == 2


def test_creation_race_not_found_is_bounded_and_not_success():
    fake = _FakeSystemd(result="success", active_once=False)
    original = fake

    def runner(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[0].endswith("systemctl") and argv[1] == "show" and original.show_count == 1:
            result.stdout = "LoadState=not-found\n"
        return result

    ticks = iter((0.0, 31.0, 31.0, 31.0, 31.0))
    evidence = run_drill(expect="success", runner=runner, clock=lambda: next(ticks, 31.0), sleeper=lambda _: None)
    assert evidence["result"] == "failed"
    assert evidence["ok"] is False
