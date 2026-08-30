"""Bounded process, cgroup, and disk metric sampling."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import psutil

from .models import AdapterKind
from .slot import proc_start_ticks

_MAX_PIDS = 512
_MAX_CGROUP_PIDS = 4096


@dataclass(frozen=True)
class DiskMetrics:
    lxc_free_bytes: int | None
    data_free_bytes: int | None
    backup_free_bytes: int | None

    @property
    def lxc_filesystem_free_bytes(self) -> int | None:
        return self.lxc_free_bytes

    @property
    def profile_data_free_bytes(self) -> int | None:
        return self.data_free_bytes

    @property
    def profile_backup_free_bytes(self) -> int | None:
        return self.backup_free_bytes


@dataclass(frozen=True)
class ProcessMetrics:
    pid: int | None
    pids: tuple[int, ...]
    cpu_percent: float | None
    rss_bytes: int | None
    process_start_ticks: int | None
    disk: DiskMetrics
    disk_read_bps: float | None = None
    disk_write_bps: float | None = None


class HostTelemetrySource:
    """Bounded procfs/cgroup-v2 source with no subprocess or process labels."""

    def __init__(self, *, proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"),
                 net_root: Path = Path("/sys/class/net"), block_devices: tuple[str, ...] = ()):
        self.proc_root, self.cgroup_root, self.net_root = proc_root, cgroup_root, net_root
        self.block_devices = tuple(device for device in block_devices if device and "/" not in device and ".." not in device)

    def collect(self, profile: Any, *, pid: int | None = None) -> dict[str, float | int | None]:
        result: dict[str, float | int | None] = {}
        unit = getattr(profile, "systemd_unit", None)
        if isinstance(unit, str):
            candidates = []
            observed = self._observed_cgroup(unit, pid)
            if observed is not None:
                candidates.append(observed)
            # systemd may place game units under either games.slice or
            # system.slice; inspect only those exact unit paths.
            candidates.extend(self.cgroup_root / slice_name / unit for slice_name in ("games.slice", "system.slice"))
            for path in candidates:
                values = self._io_stat(path / "io.stat")
                if values:
                    result.update(values)
                    break
        result.update(self._psi(self.proc_root / "pressure" / "io"))
        result.update(self._diskstats(self.proc_root / "diskstats", self.block_devices))
        result.update(self._network(self.net_root))
        return result

    def _observed_cgroup(self, unit: str, pid: int | None) -> Path | None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return None
        try:
            lines = (self.proc_root / str(pid) / "cgroup").read_text(encoding="ascii", errors="ignore").splitlines()[:32]
        except OSError:
            return None
        for line in lines:
            parts = line.split(":", 2)
            if len(parts) != 3 or parts[0] != "0" or parts[1] != "":
                continue
            relative = Path(parts[2].lstrip("/"))
            if relative.is_absolute() or ".." in relative.parts or relative.name != unit:
                continue
            return self.cgroup_root / relative
        return None

    @staticmethod
    def _io_stat(path: Path) -> dict[str, int]:
        totals = {"service_io_read_bytes_total": 0, "service_io_write_bytes_total": 0,
                  "service_io_read_ops_total": 0, "service_io_write_ops_total": 0}
        try:
            lines = path.read_text(encoding="ascii", errors="ignore").splitlines()[:256]
        except OSError:
            return {}
        for line in lines:
            for token in line.split()[1:33]:
                if "=" not in token:
                    continue
                key, raw = token.split("=", 1)
                try:
                    value = max(0, int(raw))
                except ValueError:
                    continue
                mapping = {"rbytes": "service_io_read_bytes_total", "wbytes": "service_io_write_bytes_total",
                           "rios": "service_io_read_ops_total", "wios": "service_io_write_ops_total"}
                if key in mapping:
                    totals[mapping[key]] += value
        return totals

    @staticmethod
    def _psi(path: Path) -> dict[str, float]:
        try:
            lines = path.read_text(encoding="ascii", errors="ignore").splitlines()[:8]
        except OSError:
            return {}
        values: dict[str, float] = {}
        for line in lines:
            if not line.startswith(("some ", "full ")):
                continue
            for token in line.split()[1:]:
                if token.startswith("avg10="):
                    try:
                        values[f"host_psi_io_{line.split()[0]}_avg10"] = max(0.0, float(token[6:]))
                    except ValueError:
                        pass
        return values

    @staticmethod
    def _diskstats(path: Path, approved_devices: tuple[str, ...] = ()) -> dict[str, int]:
        if not approved_devices:
            return {}
        approved = frozenset(approved_devices)
        read_ms = write_ms = 0
        try:
            lines = path.read_text(encoding="ascii", errors="ignore").splitlines()[:512]
        except OSError:
            return {}
        for line in lines:
            fields = line.split()
            if len(fields) < 14:
                continue
            if fields[2] not in approved:
                continue
            try:
                read_ms += max(0, int(fields[6]))
                write_ms += max(0, int(fields[10]))
            except ValueError:
                continue
        return {"host_disk_read_io_time_ms_total": read_ms, "host_disk_write_io_time_ms_total": write_ms}

    @staticmethod
    def _network(root: Path) -> dict[str, int]:
        rx = tx = 0
        try:
            interfaces = list(root.iterdir())[:64]
        except OSError:
            return {}
        for interface in interfaces:
            try:
                rx += max(0, int((interface / "statistics/rx_bytes").read_text().strip()))
                tx += max(0, int((interface / "statistics/tx_bytes").read_text().strip()))
            except (OSError, ValueError):
                continue
        return {"host_network_rx_bytes_total": rx, "host_network_tx_bytes_total": tx}


def enumerate_cgroup_pids(cgroup: str | os.PathLike[str]) -> tuple[int, ...]:
    """Read a fixed cgroup.procs file, bounded and de-duplicated."""

    path = Path(cgroup)
    if path.name != "cgroup.procs":
        path = path / "cgroup.procs"
    try:
        lines = path.read_text(encoding="ascii", errors="ignore").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return ()
    found: list[int] = []
    seen: set[int] = set()
    for line in lines[:_MAX_CGROUP_PIDS]:
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid > 0 and pid not in seen:
            seen.add(pid)
            found.append(pid)
    return tuple(found)


def systemd_cgroup_path(unit: str, root: Path = Path("/sys/fs/cgroup")) -> Path | None:
    if not isinstance(unit, str) or not unit.endswith(".service") or "/" in unit or ".." in unit:
        return None
    return root / "system.slice" / unit


class MetricSampler:
    def __init__(
        self,
        *,
        process_provider: Callable[[int], Any] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        disk_usage: Callable[[str | os.PathLike[str]], Any] = psutil.disk_usage,
        process_iter: Callable[..., Any] = psutil.process_iter,
        cgroup_resolver: Callable[[str], str | os.PathLike[str] | None] | None = None,
        pid_cgroup_resolver: Callable[[int, str], str | os.PathLike[str] | None] | None = None,
        connection_provider: Callable[..., Any] = psutil.net_connections,
        disk_cache_ttl: float = 45.0,
    ):
        self._process_provider = process_provider or psutil.Process
        self._monotonic = monotonic
        self._disk_usage = disk_usage
        self._process_iter = process_iter
        self._cgroup_resolver = cgroup_resolver or _resolve_control_group
        self._pid_cgroup_resolver = pid_cgroup_resolver or process_cgroup_path
        self._connection_provider = connection_provider
        self._cgroup_cache: dict[str, Path | None] = {}
        self._disk_cache_ttl = max(0.0, float(disk_cache_ttl))
        self._disk_cache: dict[str, tuple[float, int | None]] = {}
        self._previous: dict[str, tuple[float, float, int | None, int | None, frozenset[tuple[int, float | int]]]] = {}

    def sample(
        self,
        profile: Any,
        *,
        pid: int | None = None,
        cgroup_path: str | os.PathLike[str] | None = None,
        track_rates: bool = True,
        connections: Mapping[str, list[Any]] | None = None,
    ) -> ProcessMetrics:
        """Sample process metrics, optionally without advancing rate state.

        Health identity probes share this sampler with status publishing but
        must not consume the previous/current I/O pair used for rates.
        """
        profile_key = getattr(getattr(profile, "id", None), "value", getattr(profile, "id", "unknown"))
        pids = self._target_pids(
            profile,
            pid=pid,
            cgroup_path=cgroup_path,
            connections=connections,
        )
        processes: list[Any] = []
        valid_pids: list[int] = []
        starts: list[int] = []
        identities: list[tuple[int, float | int]] = []
        adapter = getattr(getattr(profile, "adapter", None), "value", getattr(profile, "adapter", None))
        systemd_game_found = False
        for candidate in pids[:_MAX_PIDS]:
            try:
                process = self._process_provider(candidate)
                if process is None:
                    continue
                expected = self._is_valid_process(process, profile)
                if adapter != AdapterKind.SYSTEMD.value and not expected:
                    continue
                systemd_game_found = systemd_game_found or expected
                # Accessing create_time guards against PID reuse.  Linux's
                # start ticks are retained separately for cross-component use.
                create_time = float(process.create_time())
                if create_time <= 0:
                    continue
                processes.append(process)
                valid_pids.append(candidate)
                ticks = proc_start_ticks(candidate)
                if ticks is not None:
                    starts.append(ticks)
                    identities.append((candidate, ticks))
                else:
                    identities.append((candidate, create_time))
            except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                continue
        # A supplied JVM pid may have child workers.  Systemd's cgroup is
        # already the complete identity boundary; never escape it by walking
        # descendants.
        for process in tuple(processes) if adapter != AdapterKind.SYSTEMD.value else ():
            try:
                children = process.children(recursive=True)[: _MAX_PIDS - len(processes)]
            except (AttributeError, OSError, psutil.Error):
                children = ()
            for child in children:
                if child.pid in valid_pids:
                    continue
                try:
                    if float(child.create_time()) <= 0:
                        continue
                except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                    continue
                processes.append(child)
                valid_pids.append(child.pid)
                try:
                    child_ticks = proc_start_ticks(child.pid)
                    identities.append((child.pid, child_ticks if child_ticks is not None else float(child.create_time())))
                except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                    continue

        if adapter == AdapterKind.SYSTEMD.value and not systemd_game_found:
            processes.clear()
            valid_pids.clear()
            starts.clear()
            identities.clear()

        now = self._monotonic()
        cpu_total = 0.0
        rss_total = 0
        read_total = 0
        write_total = 0
        io_complete = bool(processes)
        for process in processes:
            try:
                times = process.cpu_times()
                cpu_total += float(times.user) + float(times.system)
                rss_total += int(process.memory_info().rss)
            except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                continue
            try:
                counters = process.io_counters()
                read_total += int(counters.read_bytes)
                write_total += int(counters.write_bytes)
            except (AttributeError, OSError, PermissionError, psutil.Error, TypeError, ValueError):
                io_complete = False
        current_identities = frozenset(identities)
        previous = self._previous.get(profile_key)
        if track_rates:
            self._previous[profile_key] = (
                now,
                cpu_total,
                read_total if io_complete else None,
                write_total if io_complete else None,
                current_identities,
            )
        else:
            previous = None
        cpu_percent: float | None = None
        if (
            processes
            and previous is not None
            and previous[4] == current_identities
            and now > previous[0]
            and cpu_total >= previous[1]
        ):
            cpu_percent = max(
                0.0,
                min(
                    100.0 * (cpu_total - previous[1]) / (now - previous[0]),
                    10000.0,
                ),
            )
        disk_read_bps = None
        disk_write_bps = None
        if (
            io_complete
            and previous is not None
            and previous[4] == current_identities
            and previous[2] is not None
            and previous[3] is not None
            and now > previous[0]
            and read_total >= previous[2]
            and write_total >= previous[3]
        ):
            elapsed = now - previous[0]
            disk_read_bps = (read_total - previous[2]) / elapsed
            disk_write_bps = (write_total - previous[3]) / elapsed
        return ProcessMetrics(
            pid=valid_pids[0] if valid_pids else None,
            pids=tuple(valid_pids),
            cpu_percent=cpu_percent,
            rss_bytes=rss_total if processes else None,
            process_start_ticks=starts[0] if starts else None,
            disk=self._disk_metrics(profile, now=now),
            disk_read_bps=disk_read_bps,
            disk_write_bps=disk_write_bps,
        )

    def _target_pids(
        self,
        profile: Any,
        *,
        pid: int | None,
        cgroup_path: str | os.PathLike[str] | None,
        connections: Mapping[str, list[Any]] | None = None,
    ) -> tuple[int, ...]:
        adapter = getattr(getattr(profile, "adapter", None), "value", getattr(profile, "adapter", None))
        if adapter == AdapterKind.SYSTEMD.value:
            if cgroup_path is None:
                unit = getattr(profile, "systemd_unit", None)
                path = self._cgroup_cache.get(unit) if unit else None
                # MainPID is the strongest available cgroup identity and can
                # change on every activation.  Resolve it on each observed
                # running sample instead of allowing a pre-exec miss to cache
                # a guessed slice for the lifetime of slotd.
                resolved = (
                    self._pid_cgroup_resolver(pid, unit)
                    if unit and isinstance(pid, int) and pid > 0
                    else None
                )
                if resolved is not None:
                    path = Path(resolved)
                    self._cgroup_cache[unit] = path
                elif path is None or not path.is_dir():
                    resolved = self._cgroup_resolver(unit) if unit else None
                    candidate = Path(resolved) if resolved is not None else None
                    if candidate is not None and candidate.is_dir():
                        path = candidate
                        self._cgroup_cache[unit] = candidate
                    else:
                        path = None
                        if unit:
                            self._cgroup_cache.pop(unit, None)
            else:
                path = Path(cgroup_path)
            return enumerate_cgroup_pids(path) if path else ()
        preferred = (pid,) if pid is not None and pid > 0 else ()
        port_owners = self._port_owner_pids(profile, connections=connections)
        candidates = preferred + tuple(port_owners)
        return tuple(dict.fromkeys(candidates))[:_MAX_PIDS]

    def _port_owner_pids(
        self,
        profile: Any,
        *,
        connections: Mapping[str, list[Any]] | None = None,
    ) -> tuple[int, ...]:
        owners: list[int] = []
        for spec in tuple(getattr(profile, "ports", ()))[:16]:
            protocol = getattr(spec, "protocol", None)
            port = getattr(spec, "port", None)
            if protocol not in {"tcp", "udp"} or not isinstance(port, int):
                continue
            try:
                rows = (
                    connections.get(protocol)
                    if connections is not None and protocol in connections
                    else self._connection_provider(kind=protocol)
                )
            except (OSError, psutil.Error, TypeError):
                continue
            for row in rows:
                try:
                    address = row.laddr
                    row_port = getattr(address, "port", address[1] if address else None)
                    candidate = getattr(row, "pid", None)
                    if row_port == port and isinstance(candidate, int) and candidate > 0:
                        owners.append(candidate)
                except (AttributeError, IndexError, TypeError):
                    continue
        return tuple(dict.fromkeys(owners))[:_MAX_PIDS]

    @staticmethod
    def _is_valid_process(process: Any, profile: Any) -> bool:
        spec = getattr(profile, "process", None)
        expected = str(getattr(spec, "executable", ""))
        if not expected:
            return False
        try:
            actual = process.exe()
        except (AttributeError, OSError, psutil.Error):
            actual = getattr(process, "exe_path", None)
        # Explicit pid identity never permits Crafty's Python controller to
        # masquerade as Minecraft; executable validation is the key boundary.
        # Game releases are selected through a root-owned ``current`` symlink.
        # psutil reports the kernel-resolved executable path, so compare the
        # canonical paths rather than rejecting a valid release solely because
        # its selector symlink was traversed at exec time.
        if not actual:
            return False
        try:
            if os.path.realpath(actual) != os.path.realpath(expected):
                return False
        except (OSError, RuntimeError, ValueError):
            return False
        try:
            cmdline = process.cmdline()
        except (AttributeError, OSError, psutil.Error):
            cmdline = ()
        literals = tuple(getattr(spec, "argv_contains", ()))
        return isinstance(cmdline, (tuple, list)) and (not literals or all(value in cmdline for value in literals))

    def _disk_metrics(self, profile: Any, *, now: float | None = None) -> DiskMetrics:
        paths = getattr(profile, "paths", None)
        current_time = self._monotonic() if now is None else now

        def free(path: Any) -> int | None:
            if path is None:
                return None
            key = str(path)
            cached = self._disk_cache.get(key)
            if cached is not None and current_time - cached[0] < self._disk_cache_ttl:
                return cached[1]
            try:
                value = max(0, int(self._disk_usage(key).free))
            except (AttributeError, OSError, psutil.Error, TypeError, ValueError):
                value = None
            self._disk_cache[key] = (current_time, value)
            return value

        roots = tuple(getattr(paths, "data_roots", ())) if paths else ()
        lxc = free("/")  # filesystem containing the control plane
        data = free(roots[0]) if roots else None
        backup = free(getattr(paths, "backup_root", None)) if paths else None
        return DiskMetrics(lxc, data, backup)

    def cached_disk_metrics(self, profile: Any) -> DiskMetrics:
        """Return process-independent disk metrics through the shared TTL cache."""

        return self._disk_metrics(profile)

    def invalidate_cgroup(self, unit: str | None = None) -> None:
        """Forget cached cgroup paths after a systemd transition."""

        if unit is None:
            self._cgroup_cache.clear()
        else:
            self._cgroup_cache.pop(unit, None)


def _resolve_control_group(unit: str) -> Path | None:
    """Return an existing fixed service cgroup without forking."""

    if not isinstance(unit, str) or not unit.endswith(".service") or "/" in unit or ".." in unit:
        return None
    root = Path("/sys/fs/cgroup")
    # Game units are explicitly assigned to games.slice.  Keep system.slice as
    # the second fixed location for controller-owned services and legacy
    # deployments; never walk arbitrary cgroup paths.
    for slice_name in ("games.slice", "system.slice"):
        candidate = root / slice_name / unit
        if candidate.is_dir():
            return candidate
    return None


def process_cgroup_path(
    pid: int,
    unit: str,
    *,
    root: Path = Path("/sys/fs/cgroup"),
    proc_root: Path = Path("/proc"),
) -> Path | None:
    """Resolve a service cgroup from a process' cgroup-v2 membership.

    The unit component is required in the kernel-provided path and the result
    is truncated at that component so child cgroups cannot narrow accounting.
    """

    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(unit, str)
        or not unit.endswith(".service")
        or "/" in unit
        or ".." in unit
    ):
        return None
    try:
        rows = (proc_root / str(pid) / "cgroup").read_text(
            encoding="ascii", errors="ignore"
        )[:8192].splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    for row in rows[:64]:
        hierarchy, first_separator, remainder = row.partition(":")
        controllers, second_separator, raw = remainder.partition(":")
        if (
            hierarchy != "0"
            or not first_separator
            or not second_separator
            or controllers
            or not raw.startswith("/")
        ):
            continue
        relative = Path(raw.lstrip("/"))
        if ".." in relative.parts or unit not in relative.parts:
            continue
        unit_index = relative.parts.index(unit)
        return root.joinpath(*relative.parts[: unit_index + 1])
    return None


__all__ = [
    "DiskMetrics",
    "ProcessMetrics",
    "MetricSampler",
    "enumerate_cgroup_pids",
    "process_cgroup_path",
    "systemd_cgroup_path",
    "HostTelemetrySource",
]
