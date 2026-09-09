"""Bounded, read-only effective resource capacity for a trusted process."""

from __future__ import annotations

import os
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROC = Path("/proc")
_CGROUP = Path("/sys/fs/cgroup")
_MAX_READ = 4096
_MAX_PROC_STAT_READ = 1024 * 1024
_MAX_CGROUP_DEPTH = 64

def _read(path: Path, limit: int = _MAX_READ) -> str | None:
    try:
        with path.open("r", encoding="ascii") as stream:
            value = stream.read(limit + 1)
            return value.strip() if len(value) <= limit else None
    except (OSError, UnicodeError):
        return None

def _cpuset(value: str | None) -> int | None:
    if not value or value == "unknown": return None
    total, previous = 0, -1
    try:
        for part in value.split(","):
            if "-" in part:
                first, last = (int(x) for x in part.split("-", 1))
                if first < 0 or last < first or first <= previous: return None
                total += last - first + 1
            else:
                if int(part) < 0 or int(part) <= previous: return None
                total += 1; last = int(part)
            previous = last
    except (TypeError, ValueError): return None
    return total or None

def _quota_cpus(path: Path) -> float | None:
    value = _read(path / "cpu.max")
    if value is None: return None
    fields = value.split()
    if len(fields) != 2: return None
    if fields[0] == "max":
        return 0.0 if fields[1].isdigit() and int(fields[1]) > 0 else None
    try:
        quota, period = int(fields[0]), int(fields[1])
        return quota / period if quota > 0 and period > 0 else None
    except (ValueError, OverflowError): return None

def _memory_limit(path: Path) -> int | float | None:
    value = _read(path / "memory.max")
    if value is None: return None
    if value == "max": return math.inf
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except ValueError: return None

def _host_memory() -> int | None:
    value = _read(_PROC / "meminfo", _MAX_PROC_STAT_READ)
    if value is None: return None
    for line in value.splitlines():
        if line.startswith("MemTotal:"):
            try: return int(line.split()[1]) * 1024
            except (IndexError, ValueError): return None
    return None

def _start_ticks(pid: int) -> int | None:
    value = _read(_PROC / str(pid) / "stat")
    if value is None or ")" not in value: return None
    try: return int(value.rsplit(")", 1)[1].split()[19])
    except (IndexError, ValueError): return None

def process_start_ticks(pid: int) -> int | None: return _start_ticks(pid)

def process_started_at(pid: int) -> datetime | None:
    ticks = _start_ticks(pid); boot = _read(_PROC / "stat", _MAX_PROC_STAT_READ)
    if ticks is None or boot is None: return None
    try:
        btime = next(int(line.split()[1]) for line in boot.splitlines() if line.startswith("btime "))
        return datetime.fromtimestamp(btime + ticks / os.sysconf(os.sysconf_names["SC_CLK_TCK"]), timezone.utc)
    except (StopIteration, IndexError, ValueError, OSError, KeyError): return None

def _cgroup_paths(pid: int) -> tuple[Path, ...] | None:
    value = _read(_PROC / str(pid) / "cgroup")
    if value is None: return None
    paths: list[Path] = []
    for line in value.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3 or fields[1] != "": continue
        relative = fields[2]; parts = Path(relative).parts
        if not relative.startswith("/") or ".." in parts or len(parts) > _MAX_CGROUP_DEPTH: return None
        current = _CGROUP / relative.lstrip("/")
        for _ in range(len(parts) + 1):
            paths.append(current)
            if current == _CGROUP: break
            current = current.parent
    return tuple(dict.fromkeys(paths)) or None

def _unknown() -> dict[str, Any]:
    return {"cpu_capacity_percent": None, "memory_capacity_bytes": None, "cpu_source": "unknown", "memory_source": "unknown"}

def probe(pid: int | None, started_ticks: int | None = None) -> dict[str, Any]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid > 4_000_000: return _unknown()
    before = _start_ticks(pid)
    if before is None or (started_ticks is not None and before != started_ticks): return _unknown()
    try: affinity = len(os.sched_getaffinity(pid))
    except (OSError, AttributeError): affinity = None
    host_cpus = os.cpu_count(); effective = float(min(affinity, host_cpus)) if affinity and host_cpus else None
    cpu_source = "process_affinity" if affinity else ("host_cpu_count" if host_cpus else "unknown")
    memory = _host_memory(); memory_source = "host_meminfo" if memory else "unknown"
    paths = _cgroup_paths(pid)
    if paths is None: return _unknown()
    for path in paths:
        # A controller not enabled by the parent has no interface at this
        # level. Its ancestor limits still apply. Do not confuse a proven
        # absent controller with unreadable or malformed limits.
        controllers = _read(path / "cgroup.controllers")
        available = set(controllers.split()) if controllers is not None else None
        def limit_absent(name: str, controller: str) -> bool:
            try:
                (path / name).stat()
            except FileNotFoundError:
                return path == _CGROUP or (available is not None and controller not in available)
            except OSError:
                pass
            return False
        quota = 0.0 if limit_absent("cpu.max", "cpu") else _quota_cpus(path)
        limit = math.inf if limit_absent("memory.max", "memory") else _memory_limit(path)
        if quota is None or limit is None: return _unknown()
        cpuset_value = _read(path / "cpuset.cpus.effective") or _read(path / "cpuset.cpus")
        cpuset = _cpuset(cpuset_value)
        if cpuset_value not in (None, "", "unknown") and cpuset is None: return _unknown()
        if cpuset is not None and effective is not None: effective = min(effective, float(cpuset)); cpu_source = "cgroup_cpuset"
        if quota and effective is not None: effective = min(effective, quota); cpu_source = "cgroup_cpu_quota"
        if memory is not None and limit < memory: memory = int(limit); memory_source = "cgroup_memory_max"
    if _start_ticks(pid) != before: return _unknown()
    return {"cpu_capacity_percent": effective * 100 if effective is not None else None, "memory_capacity_bytes": memory, "cpu_source": cpu_source, "memory_source": memory_source}

__all__ = ["probe", "process_started_at", "process_start_ticks"]
