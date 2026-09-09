from pathlib import Path

import game_control.resource_capacity as capacity


def _fake_tree(tmp_path: Path, monkeypatch, *, cgroup="/a/b", cpu=("max", "100000"), memory=("max",), cpuset="0-3"):
    proc = tmp_path / "proc"; root = tmp_path / "cgroup"
    (proc / "7").mkdir(parents=True)
    (proc / "7" / "stat").write_text("7 (java) S " + " ".join(["0"] * 18) + " 123\n")
    (proc / "7" / "cgroup").write_text(f"0::{cgroup}\n")
    (proc / "meminfo").write_text("MemTotal:       33554432 kB\n")
    for parent in [root / "a/b", root / "a", root]:
        parent.mkdir(parents=True, exist_ok=True)
        (parent / "cpu.max").write_text(" ".join(cpu))
        (parent / "memory.max").write_text(" ".join(memory))
        (parent / "cpuset.cpus.effective").write_text(cpuset)
    monkeypatch.setattr(capacity, "_PROC", proc)
    monkeypatch.setattr(capacity, "_CGROUP", root)
    monkeypatch.setattr(capacity.os, "sched_getaffinity", lambda pid: {0, 1, 2, 3, 4, 5})
    monkeypatch.setattr(capacity.os, "cpu_count", lambda: 6)
    return proc, root


def test_cpuset_ranges_are_counted_and_malformed_values_unknown():
    assert capacity._cpuset("0-2,4") == 4
    assert capacity._cpuset("2-1") is None
    assert capacity._cpuset("0,0") is None


def test_quota_one_and_a_half_cpus(monkeypatch, tmp_path: Path):
    (tmp_path / "cpu.max").write_text("150000 100000")
    assert capacity._quota_cpus(tmp_path) == 1.5


def test_invalid_or_reused_pid_is_unknown():
    result = capacity.probe(0)
    assert result["cpu_capacity_percent"] is None
    assert result["memory_capacity_bytes"] is None


def test_cgroup_ancestors_are_bounded_to_root(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"; cgroup = tmp_path / "cgroup"
    (proc / "7").mkdir(parents=True)
    (proc / "7" / "cgroup").write_text("0::/a/b")
    monkeypatch.setattr(capacity, "_PROC", proc)
    monkeypatch.setattr(capacity, "_CGROUP", cgroup)
    paths = capacity._cgroup_paths(7)
    assert paths == (cgroup / "a/b", cgroup / "a", cgroup)


def test_probe_applies_ancestor_cpuset_quota_and_memory_min(tmp_path: Path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch, cpu=("150000", "100000"), memory=(str(9 * 1024**3),))
    result = capacity.probe(7, started_ticks=123)
    assert result["cpu_capacity_percent"] == 150.0
    assert result["memory_capacity_bytes"] == 9 * 1024**3


def test_probe_accepts_explicit_unlimited_and_rejects_bool_pid(tmp_path: Path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    assert capacity.probe(7, started_ticks=123)["cpu_capacity_percent"] == 400.0
    assert capacity.probe(True)["cpu_capacity_percent"] is None


def test_probe_fails_closed_for_missing_or_malformed_cgroup_limits(tmp_path: Path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    (tmp_path / "cgroup" / "a" / "memory.max").unlink()
    assert capacity.probe(7, started_ticks=123)["memory_capacity_bytes"] is None
    (tmp_path / "cgroup" / "a" / "memory.max").write_text("not-a-limit")
    assert capacity.probe(7, started_ticks=123)["cpu_capacity_percent"] is None


def test_cgroup_depth_over_bound_is_rejected(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"; proc.joinpath("7").mkdir(parents=True)
    deep = "/" + "/".join(f"x{i}" for i in range(65))
    proc.joinpath("7", "cgroup").write_text(f"0::{deep}\n")
    monkeypatch.setattr(capacity, "_PROC", proc)
    monkeypatch.setattr(capacity, "_CGROUP", tmp_path / "cgroup")
    assert capacity._cgroup_paths(7) is None


def test_process_started_at_allows_large_proc_stat(tmp_path: Path, monkeypatch):
    proc = tmp_path / "proc"; proc.joinpath("7").mkdir(parents=True)
    stat = "7 (java) S " + " ".join(["0"] * 18) + " 100\n"
    proc.joinpath("7", "stat").write_text(stat)
    proc.joinpath("stat").write_text("btime 1000\n" + ("x" * 5000))
    monkeypatch.setattr(capacity, "_PROC", proc)
    monkeypatch.setattr(capacity.os, "sysconf", lambda _: 100)
    assert capacity.process_started_at(7) is not None


def test_absent_leaf_controller_still_uses_parent_limits(tmp_path: Path, monkeypatch):
    _, root = _fake_tree(tmp_path, monkeypatch, memory=(str(9 * 1024**3),))
    leaf = root / "a/b"
    (leaf / "cpu.max").unlink()
    (leaf / "cgroup.controllers").write_text("memory pids")
    (root / "cpu.max").unlink()
    (root / "memory.max").unlink()
    (root / "a" / "cpu.max").write_text("200000 100000")
    result = capacity.probe(7, 123)
    assert result["cpu_capacity_percent"] == 200
    assert result["memory_capacity_bytes"] == 9 * 1024**3


def test_missing_enabled_limit_remains_unknown(tmp_path: Path, monkeypatch):
    _, root = _fake_tree(tmp_path, monkeypatch)
    (root / "a/b" / "cpu.max").unlink()
    (root / "a/b" / "cgroup.controllers").write_text("cpu memory")
    assert capacity.probe(7, 123)["cpu_capacity_percent"] is None


def test_zero_memory_limit_is_not_treated_as_unlimited(tmp_path: Path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch, memory=("0",))
    assert capacity.probe(7, 123)["memory_capacity_bytes"] == 0


def test_pid_reuse_during_probe_is_unknown(tmp_path: Path, monkeypatch):
    _fake_tree(tmp_path, monkeypatch)
    ticks = iter([123, 124])
    monkeypatch.setattr(capacity, "_start_ticks", lambda _: next(ticks))
    assert capacity.probe(7, 123)["memory_capacity_bytes"] is None
