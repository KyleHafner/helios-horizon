from pathlib import Path
from types import SimpleNamespace

import pytest

from game_control.metrics import HostTelemetrySource, MetricSampler, enumerate_cgroup_pids, process_cgroup_path
from game_control.profile import ProfileRegistry


def test_cgroup_pid_reader_is_bounded_and_ignores_invalid(tmp_path):
    (tmp_path / "cgroup.procs").write_text("11\nbad\n0\n-1\n11\n12\n" + "99\n" * 10000)
    assert enumerate_cgroup_pids(tmp_path) == (11, 12, 99)


def test_systemd_cgroup_path_rejects_hostile_unit_names():
    from game_control.metrics import systemd_cgroup_path

    assert systemd_cgroup_path("minecraft.service", Path("/tmp/cgroup")) == Path(
        "/tmp/cgroup/system.slice/minecraft.service"
    )
    assert systemd_cgroup_path("../../etc.service", Path("/tmp/cgroup")) is None
    assert systemd_cgroup_path("not-a-unit", Path("/tmp/cgroup")) is None


def test_host_telemetry_reads_bounded_cgroup_psi_disk_and_host_network(tmp_path):
    cgroup = tmp_path / "cgroup" / "games.slice" / "minecraft.service"
    cgroup.mkdir(parents=True)
    (cgroup / "io.stat").write_text("8:0 rbytes=10 wbytes=20 rios=2 wios=3\n")
    proc = tmp_path / "proc" / "pressure"
    proc.mkdir(parents=True)
    (proc / "io").write_text("some avg10=1.25 avg60=0.2 avg300=0.1 total=3\nfull avg10=0.00 avg60=0 avg300=0 total=0\n")
    (tmp_path / "proc" / "diskstats").write_text("8 0 sda 1 0 2 7 3 0 4 11 0 0 0\n")
    net = tmp_path / "net" / "eth0" / "statistics"
    net.mkdir(parents=True)
    (net / "rx_bytes").write_text("100")
    (net / "tx_bytes").write_text("200")
    profile = SimpleNamespace(systemd_unit="minecraft.service")
    values = HostTelemetrySource(proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup",
                                 net_root=tmp_path / "net", block_devices=("sda",)).collect(profile)
    assert values["service_io_read_bytes_total"] == 10
    assert values["service_io_write_ops_total"] == 3
    assert values["host_psi_io_some_avg10"] == 1.25
    assert values["host_disk_read_io_time_ms_total"] == 7
    assert values["host_network_tx_bytes_total"] == 200


def test_host_diskstats_requires_approved_backing_devices_and_does_not_double_count_partitions(tmp_path):
    diskstats = tmp_path / "diskstats"
    diskstats.write_text(
        "8 0 sda 1 0 2 7 3 0 4 11 0 0 0\n"
        "8 1 sda1 1 0 2 70 3 0 4 110 0 0 0\n"
        "7 0 loop0 1 0 2 700 3 0 4 1100 0 0 0\n"
    )
    assert HostTelemetrySource._diskstats(diskstats) == {}
    assert HostTelemetrySource._diskstats(diskstats, ("sda",)) == {
        "host_disk_read_io_time_ms_total": 7,
        "host_disk_write_io_time_ms_total": 11,
    }


def test_host_source_prefers_observed_pid_cgroup_over_guessed_slice(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "cgroup").write_text("0::/custom.slice/minecraft.service\n")
    (proc / "pressure").mkdir()
    (proc / "pressure" / "io").write_text("")
    observed = tmp_path / "cgroup" / "custom.slice" / "minecraft.service"
    guessed = tmp_path / "cgroup" / "games.slice" / "minecraft.service"
    observed.mkdir(parents=True); guessed.mkdir(parents=True)
    (observed / "io.stat").write_text("8:0 rbytes=41 wbytes=0 rios=1 wios=0\n")
    (guessed / "io.stat").write_text("8:0 rbytes=99 wbytes=0 rios=1 wios=0\n")
    values = HostTelemetrySource(proc_root=proc, cgroup_root=tmp_path / "cgroup",
                                 net_root=tmp_path / "missing").collect(
        SimpleNamespace(systemd_unit="minecraft.service"), pid=42
    )
    assert values["service_io_read_bytes_total"] == 41


def test_process_cgroup_path_reads_unified_membership_without_forking(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "42" / "cgroup").write_text(
        "0::/games.slice/pz-rising.service/workers\n"
    )

    assert process_cgroup_path(
        42,
        "pz-rising.service",
        root=cgroup_root,
        proc_root=proc_root,
    ) == cgroup_root / "games.slice" / "pz-rising.service"


def test_process_cgroup_path_rejects_another_units_membership(tmp_path):
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "42" / "cgroup").write_text(
        "0::/system.slice/unrelated.service\n"
    )

    assert process_cgroup_path(
        42,
        "pz-rising.service",
        root=tmp_path / "cgroup",
        proc_root=proc_root,
    ) is None


def test_process_cgroup_path_rejects_malformed_and_multiline_membership(tmp_path):
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)
    (proc_root / "42" / "cgroup").write_text(
        "0::/system.slice/other.service\n"
        "malformed\n"
        "0:name=legacy:/system.slice/pz-rising.service\n"
    )
    assert process_cgroup_path(
        42,
        "pz-rising.service",
        root=tmp_path / "cgroup",
        proc_root=proc_root,
    ) is None


def test_sampler_uses_fixed_unit_fallback_when_pid_membership_unavailable(tmp_path):
    fallback_calls = []
    sampler = MetricSampler(
        process_provider=lambda _pid: None,
        pid_cgroup_resolver=lambda _pid, _unit: None,
        cgroup_resolver=lambda unit: fallback_calls.append(unit) or tmp_path,
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        systemd_unit="pz-rising.service",
        process=SimpleNamespace(executable=Path("/usr/bin/pz"), argv_contains=()),
    )
    sampler.sample(profile, pid=42)
    sampler.sample(profile, pid=42)
    assert fallback_calls == ["pz-rising.service"]


def test_sampler_prefers_pid_cgroup_resolution(tmp_path):
    (tmp_path / "cgroup.procs").write_text("")
    pid_calls: list[tuple[int, str]] = []
    fallback_calls: list[str] = []

    sampler = MetricSampler(
        process_provider=lambda pid: None,
        pid_cgroup_resolver=lambda pid, unit: pid_calls.append((pid, unit)) or tmp_path,
        cgroup_resolver=lambda unit: fallback_calls.append(unit) or None,
        disk_usage=lambda path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        systemd_unit="pz-rising.service",
        process=SimpleNamespace(executable=Path("/usr/bin/pz"), argv_contains=()),
    )

    sampler.sample(profile, pid=42)

    assert pid_calls == [(42, "pz-rising.service")]
    assert fallback_calls == []


def test_sampler_rebinds_cached_cgroup_to_each_observed_main_pid(tmp_path):
    stale = tmp_path / "system.slice" / "minecraft.service"
    active = tmp_path / "games.slice" / "minecraft.service"
    stale.mkdir(parents=True)
    active.mkdir(parents=True)
    (stale / "cgroup.procs").write_text("")
    (active / "cgroup.procs").write_text("42\n")
    pid_paths = iter((None, active))

    class Process:
        def exe(self):
            return "/usr/bin/java"

        def cmdline(self):
            return ("/usr/bin/java", "@server.args")

        def create_time(self):
            return 1.0

        def cpu_times(self):
            return SimpleNamespace(user=1.0, system=1.0)

        def memory_info(self):
            return SimpleNamespace(rss=1024)

        def io_counters(self):
            return SimpleNamespace(read_bytes=0, write_bytes=0)

    sampler = MetricSampler(
        process_provider=lambda _pid: Process(),
        pid_cgroup_resolver=lambda _pid, _unit: next(pid_paths),
        cgroup_resolver=lambda _unit: stale,
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="minecraft",
        adapter="systemd",
        systemd_unit="minecraft.service",
        process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=("@server.args",)),
    )

    assert sampler.sample(profile, pid=42).pid is None
    assert sampler.sample(profile, pid=42).pid == 42


def test_sampler_does_not_cache_a_missing_systemd_fallback(tmp_path):
    active = tmp_path / "games.slice" / "minecraft.service"
    fallback_calls = []

    sampler = MetricSampler(
        process_provider=lambda _pid: None,
        pid_cgroup_resolver=lambda _pid, _unit: None,
        cgroup_resolver=lambda unit: fallback_calls.append(unit) or active,
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="minecraft",
        adapter="systemd",
        systemd_unit="minecraft.service",
        process=SimpleNamespace(executable=Path("/usr/bin/java"), argv_contains=()),
    )

    sampler.sample(profile, pid=42)
    active.mkdir(parents=True)
    (active / "cgroup.procs").write_text("")
    sampler.sample(profile, pid=42)

    assert fallback_calls == ["minecraft.service", "minecraft.service"]


def test_one_bad_process_does_not_poison_other_process_metrics(tmp_path):
    (tmp_path / "cgroup.procs").write_text("11\n12\n")

    class BadProcess:
        def create_time(self):
            return 1.0

        def exe(self):
            return "/usr/bin/game"

        def cmdline(self):
            return ("game",)

        def cpu_times(self):
            raise OSError("/proc/11/stat vanished")

    class GoodProcess:
        def create_time(self):
            return 1.0

        def exe(self):
            return "/usr/bin/game"

        def cmdline(self):
            return ("game",)

        def cpu_times(self):
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_info(self):
            return SimpleNamespace(rss=42)

    sampler = MetricSampler(
        process_provider=lambda pid: {11: BadProcess(), 12: GoodProcess()}[pid],
        disk_usage=lambda _path: SimpleNamespace(free=1),
    )
    profile = SimpleNamespace(
        id="pz-rising",
        adapter="systemd",
        process=SimpleNamespace(executable=Path("/usr/bin/game"), argv_contains=()),
    )

    result = sampler.sample(profile, cgroup_path=tmp_path)

    assert result.pids == (11, 12)
    assert result.rss_bytes == 42


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


def test_minecraft_process_identity_resolves_executable_and_matches_configured_argv(tmp_path):
    registry = ProfileRegistry.load(Path(__file__).parents[1] / "config" / "profiles")
    configured = registry.require("minecraft")

    runtime_java = tmp_path / "jvm" / "bin" / "java"
    runtime_java.parent.mkdir(parents=True)
    runtime_java.write_text("")
    configured_java = tmp_path / "bin" / "java"
    configured_java.parent.mkdir()
    configured_java.symlink_to(runtime_java)

    class Process:
        def exe(self):
            return str(runtime_java)

        def cmdline(self):
            return ["java", "-jar", "server.jar"]

    profile = SimpleNamespace(
        process=SimpleNamespace(
            executable=configured_java,
            argv_contains=configured.process.argv_contains,
        )
    )

    assert MetricSampler._is_valid_process(Process(), profile)


def test_process_identity_rejects_non_matching_executable(tmp_path):
    expected = tmp_path / "java"
    expected.write_text("")
    actual = tmp_path / "other"
    actual.write_text("")

    class Process:
        def exe(self):
            return str(actual)

        def cmdline(self):
            return ["java", "-jar", "server.jar"]

    profile = SimpleNamespace(
        process=SimpleNamespace(executable=expected, argv_contains=("server.jar",))
    )

    assert not MetricSampler._is_valid_process(Process(), profile)


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
