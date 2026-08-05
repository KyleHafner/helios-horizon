from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control.metrics import MetricSampler, enumerate_cgroup_pids


def test_cgroup_pid_reader_is_bounded_and_ignores_invalid(tmp_path):
    (tmp_path / "cgroup.procs").write_text("11\nbad\n12\n" + "99\n" * 10000)
    assert enumerate_cgroup_pids(tmp_path) == (11, 12, 99)


def test_first_cpu_sample_is_none():
    sampler = MetricSampler(process_provider=lambda pid: None, monotonic=lambda: 10.0)
    profile = SimpleNamespace(id="minecraft", adapter="crafty", process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=()))
    first = sampler.sample(profile, pid=123)
    assert first.cpu_percent is None


def test_systemd_cgroup_resolution_is_cached_per_unit(tmp_path):
    (tmp_path / "cgroup.procs").write_text("")
    calls: list[str] = []

    def resolve(unit):
        calls.append(unit)
        return tmp_path

    sampler = MetricSampler(
        process_provider=lambda pid: None,
        cgroup_resolver=resolve,
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        systemd_unit="pz-rising.service",
        process=SimpleNamespace(executable=Path("/usr/bin/pz"), argv_contains=()),
    )

    sampler.sample(profile)
    sampler.sample(profile)

    assert calls == ["pz-rising.service"]


def test_disk_usage_is_cached_within_ttl():
    calls: list[str] = []

    def disk_usage(path):
        calls.append(str(path))
        return SimpleNamespace(free=100)

    sampler = MetricSampler(
        process_provider=lambda pid: None,
        monotonic=lambda: 10.0,
        disk_usage=disk_usage,
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        process=SimpleNamespace(executable=Path("/usr/bin/pz"), argv_contains=()),
        paths=SimpleNamespace(data_roots=("/",), backup_root="/"),
    )

    sampler.sample(profile, cgroup_path=Path("/does-not-exist"))
    sampler.sample(profile, cgroup_path=Path("/does-not-exist"))

    assert calls == ["/"]


def test_minecraft_controller_python_is_excluded_from_jvm_metrics():
    class Proc:
        def __init__(self, pid, exe):
            self.pid = pid
            self._exe = exe

        def create_time(self):
            return 1.0

        def exe(self):
            return self._exe

        def cmdline(self):
            return ("java", "forge")

        def cpu_times(self):
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def children(self, recursive=True):
            return ()

    java = Proc(123, "/usr/bin/java")
    python = Proc(124, "/usr/bin/python3")
    sampler = MetricSampler(
        process_provider=lambda pid: {123: java, 124: python}[pid],
        disk_usage=lambda path: SimpleNamespace(free=100),
    )
    profile = SimpleNamespace(
        id="minecraft",
        adapter="crafty",
        process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=("forge",)),
    )
    assert sampler.sample(profile, pid=124).pids == ()
    assert sampler.sample(profile, pid=123).pids == (123,)


def test_invalid_adapter_pid_falls_back_to_valid_jvm_candidate():
    class Proc:
        def __init__(self, pid, exe):
            self.pid, self._exe = pid, exe

        def create_time(self): return 1.0
        def exe(self): return self._exe
        def cmdline(self): return ("java", "forge")
        def cpu_times(self): return SimpleNamespace(user=1.0, system=0.0)
        def memory_info(self): return SimpleNamespace(rss=1)
        def children(self, recursive=True): return ()

    python = Proc(10, "/usr/bin/python3")
    java = Proc(11, "/usr/bin/java")
    sampler = MetricSampler(
        process_provider=lambda pid: {10: python, 11: java}[pid],
        connection_provider=lambda kind: (SimpleNamespace(laddr=("127.0.0.1", 25565), pid=11),),
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="minecraft", adapter="crafty",
        ports=(SimpleNamespace(protocol="tcp", port=25565),),
        process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=("forge",)),
    )
    assert sampler.sample(profile, pid=10).pids == (11,)


def test_jvm_descendant_is_included_without_matching_argv():
    class Proc:
        def __init__(self, pid, exe):
            self.pid, self._exe = pid, exe
        def create_time(self): return 1.0
        def exe(self): return self._exe
        def cmdline(self): return ("java", "forge") if self.pid == 11 else ("helper",)
        def cpu_times(self): return SimpleNamespace(user=1.0, system=0.0)
        def memory_info(self): return SimpleNamespace(rss=1)
        def children(self, recursive=True): return (Proc(12, "/usr/bin/helper"),)

    root = Proc(11, "/usr/bin/java")
    sampler = MetricSampler(
        process_provider=lambda pid: root,
        connection_provider=lambda kind: (SimpleNamespace(laddr=("127.0.0.1", 25565), pid=11),),
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(id="minecraft", adapter="crafty", ports=(SimpleNamespace(protocol="tcp", port=25565),), process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=("forge",)))
    assert sampler.sample(profile, pid=11).pids == (11, 12)


def test_systemd_metrics_are_limited_to_cgroup_pids(tmp_path):
    (tmp_path / "cgroup.procs").write_text("11\n12\n")

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 1.0

        def exe(self):
            return "/usr/bin/pz"

        def cmdline(self):
            return ("pz",)

        def cpu_times(self):
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_info(self):
            return SimpleNamespace(rss=100)

        def children(self, recursive=True):
            return (Proc(13),)

    sampler = MetricSampler(
        process_provider=lambda pid: Proc(pid),
        disk_usage=lambda path: SimpleNamespace(free=100),
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        process=SimpleNamespace(executable=Path("/usr/bin/pz"), argv_contains=()),
    )
    assert sampler.sample(profile, cgroup_path=tmp_path).pids == (11, 12)


def test_systemd_aggregates_cgroup_after_validating_game_process(tmp_path):
    (tmp_path / "cgroup.procs").write_text("11\n12\n")

    class Proc:
        def __init__(self, pid, exe):
            self.pid, self._exe = pid, exe
        def create_time(self): return 1.0
        def exe(self): return self._exe
        def cmdline(self): return ("game",) if self.pid == 11 else ("helper",)
        def cpu_times(self): return SimpleNamespace(user=1.0, system=0.0)
        def memory_info(self): return SimpleNamespace(rss=10)
        def children(self, recursive=True): return (Proc(99, "/usr/bin/outside"),)

    processes = {11: Proc(11, "/usr/bin/game"), 12: Proc(12, "/usr/bin/helper")}
    sampler = MetricSampler(process_provider=lambda pid: processes[pid], disk_usage=lambda p: SimpleNamespace(free=1))
    profile = SimpleNamespace(id="pz-rising", adapter="systemd", process=SimpleNamespace(executable=Path("/usr/bin/game"), argv_contains=()))
    result = sampler.sample(profile, cgroup_path=tmp_path)
    assert result.pids == (11, 12)
    assert result.rss_bytes == 20


def test_release_selector_symlink_is_accepted_for_process_identity(tmp_path):
    release = tmp_path / "releases" / "1.4.5.3"
    release.mkdir(parents=True)
    executable = release / "TerrariaServer.bin.x86_64"
    executable.write_text("")
    selector = tmp_path / "current"
    selector.symlink_to(release, target_is_directory=True)

    class Process:
        def exe(self):
            return str(executable)

        def cmdline(self):
            return [str(selector / executable.name), "-config", "/srv/game/serverconfig.txt"]

    profile = SimpleNamespace(
        id="terraria-vanilla",
        adapter="systemd",
        process=SimpleNamespace(
            executable=Path(selector / executable.name),
            argv_contains=("-config", "/srv/game/serverconfig.txt"),
        ),
    )

    assert MetricSampler._is_valid_process(Process(), profile)


def test_empty_pid_set_never_reports_cpu_zero(tmp_path):
    (tmp_path / "cgroup.procs").write_text("")
    sampler = MetricSampler(process_provider=lambda pid: None, monotonic=iter((1.0, 2.0)).__next__)
    profile = SimpleNamespace(id="pz-rising", adapter="systemd", process=SimpleNamespace(executable=Path("/usr/bin/game"), argv_contains=()))
    assert sampler.sample(profile, cgroup_path=tmp_path).cpu_percent is None
    assert sampler.sample(profile, cgroup_path=tmp_path).cpu_percent is None


def test_cpu_percent_has_no_cpu_count_multiplier(monkeypatch):
    class Proc:
        pid = 1

        def create_time(self): return 1.0
        def exe(self): return "/usr/bin/java"
        def cmdline(self): return ("java",)
        def cpu_times(self): return SimpleNamespace(user=self.value, system=0.0)
        def memory_info(self): return SimpleNamespace(rss=1)
        def children(self, recursive=True): return ()

    proc = Proc()
    sampler = MetricSampler(
        process_provider=lambda pid: proc,
        monotonic=iter((1.0, 2.0)).__next__,
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(id="minecraft", adapter="crafty", process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=()))
    proc.value = 1.0
    sampler.sample(profile, pid=1)
    proc.value = 2.0
    assert sampler.sample(profile, pid=1).cpu_percent == 100.0


def test_io_rates_are_aggregated_when_proc_io_is_readable():
    class Proc:
        pid = 1
        read = 100
        write = 200

        def create_time(self): return 1.0
        def exe(self): return "/usr/bin/java"
        def cmdline(self): return ("java",)
        def cpu_times(self): return SimpleNamespace(user=1.0, system=0.0)
        def memory_info(self): return SimpleNamespace(rss=1)
        def io_counters(self): return SimpleNamespace(read_bytes=self.read, write_bytes=self.write)
        def children(self, recursive=True): return ()

    proc = Proc()
    sampler = MetricSampler(
        process_provider=lambda pid: proc,
        monotonic=iter((1.0, 3.0)).__next__,
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(id="minecraft", adapter="crafty", process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=()))
    sampler.sample(profile, pid=1)
    proc.read, proc.write = 300, 600
    result = sampler.sample(profile, pid=1)
    assert result.disk_read_bps == 100.0
    assert result.disk_write_bps == 200.0


def test_systemd_parent_child_io_survives_health_probe_sample(tmp_path):
    """A health identity probe must not consume the status I/O delta."""

    (tmp_path / "cgroup.procs").write_text("11\n12\n")

    class Proc:
        def __init__(self, pid, exe, write):
            self.pid = pid
            self._exe = exe
            self.write = write

        def create_time(self):
            return 1.0

        def exe(self):
            return self._exe

        def cmdline(self):
            return ("game-slot-run",) if self.pid == 11 else ("dotnet", "tModLoader.dll")

        def cpu_times(self):
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_info(self):
            return SimpleNamespace(rss=1)

        def io_counters(self):
            return SimpleNamespace(read_bytes=0, write_bytes=self.write)

        def children(self, recursive=True):
            return ()

    parent = Proc(11, "/usr/local/libexec/game-slot-run", 0)
    child = Proc(12, "/usr/bin/dotnet", 0)
    processes = {11: parent, 12: child}
    sampler = MetricSampler(
        process_provider=lambda pid: processes[pid],
        monotonic=iter((1.0, 2.0, 3.0)).__next__,
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="terraria-tmod",
        adapter="systemd",
        systemd_unit="terraria-tmod.service",
        process=SimpleNamespace(executable=Path("/usr/bin/dotnet"), argv_contains=("tModLoader.dll",)),
    )

    baseline = sampler.sample(profile, cgroup_path=tmp_path)
    assert baseline.pids == (11, 12)
    child.write = 100
    sampler.sample(profile, cgroup_path=tmp_path, track_rates=False)
    status_sample = sampler.sample(profile, cgroup_path=tmp_path)
    assert status_sample.disk_write_bps == 50.0
