from __future__ import annotations

import pytest

from game_control.gc_telemetry import GcTelemetryParser


NORMAL = b"[0.123s][info][gc] GC(7) Pause Young (Normal) 10M->5M 5.678ms age=3\n"
CONCURRENT = b"[0.124s][info][gc] GC(7) Concurrent Mark Cycle\n"


def test_java17_normal_pause_is_bounded_and_typed():
    event = GcTelemetryParser().feed(NORMAL)[0]
    assert event.kind == "pause"
    assert event.duration_ms == 5.678
    assert event.counter == 7
    assert event.age == 3
    assert event.gc_type == "young"


def test_concurrent_and_no_pause_lines_are_ignored():
    assert GcTelemetryParser().feed(CONCURRENT) == ()


def test_partial_lines_and_rotation_reset_are_restart_safe():
    parser = GcTelemetryParser()
    assert parser.feed(NORMAL[:30]) == ()
    assert parser.feed(NORMAL[30:])[0].counter == 7
    reset = parser.feed(NORMAL, rotated=True)
    assert reset[0].kind == "reset"
    assert reset[1].offset == len(NORMAL)


@pytest.mark.parametrize("line", [
    b"[0s][info][gc] GC(1) Pause Full 1e999ms\n",
    b"[0s][info][gc] GC(10000001) Pause Full 1ms\n",
    b"[0s][info][gc] GC(1) Pause Full 1ms age=1001\n",
    b"x" * (16 * 1024 + 1) + b"\n",
])
def test_malformed_nonfinite_and_oversized_lines_fail_closed(line: bytes):
    parser = GcTelemetryParser()
    if len(line) > 16 * 1024:
        with pytest.raises(ValueError):
            parser.feed(line)
    else:
        assert parser.feed(line) == ()
