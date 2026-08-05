"""Pure, idempotent Project Zomboid JVM argument tuning helpers."""

from __future__ import annotations

import re
from typing import Any

_XMX = re.compile(r"^-Xmx(?P<size>[0-9]+[mMgG])$")
_REMOVE_PREFIXES = (
    "-Xms",
    "-XX:+UseZGC",
    "-XX:+UseG1GC",
    "-XX:MaxGCPauseMillis=",
    "-XX:+ParallelRefProcEnabled",
    "-XX:+PerfDisableSharedMem",
)


def tune_config(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with only the approved PZ JVM/GC flags changed."""

    if not isinstance(payload, dict) or not isinstance(payload.get("vmArgs"), list):
        raise ValueError("PZ config must contain a vmArgs list")
    args = payload["vmArgs"]
    if any(not isinstance(arg, str) for arg in args):
        raise ValueError("PZ vmArgs must contain text values")
    max_heap = next(
        (match for arg in args if (match := _XMX.fullmatch(arg)) is not None),
        None,
    )
    if max_heap is None:
        raise ValueError("PZ config must declare -Xmx")
    xms = f"-Xms{max_heap.group('size')}"
    tuned = [
        xms,
        f"-Xmx{max_heap.group('size')}",
        "-XX:+UseG1GC",
        "-XX:MaxGCPauseMillis=50",
        "-XX:+ParallelRefProcEnabled",
        "-XX:+PerfDisableSharedMem",
    ]
    result: list[str] = []
    inserted = False
    for arg in args:
        if _XMX.fullmatch(arg):
            if not inserted:
                result.extend(tuned)
                inserted = True
            continue
        if any(arg.startswith(prefix) for prefix in _REMOVE_PREFIXES):
            continue
        result.append(arg)
    if not inserted:
        raise ValueError("PZ config lost -Xmx while tuning")
    output = dict(payload)
    output["vmArgs"] = result
    return output
