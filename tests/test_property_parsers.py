from __future__ import annotations

from datetime import datetime

from hypothesis import given, settings, strategies as st

from game_control import redaction
from game_control.adapters import crafty, systemd
from game_control.schedule import ScheduleEntry, parse_schedule


settings.register_profile(
    "property-tests",
    derandomize=True,
    max_examples=80,
    deadline=None,
)
settings.load_profile("property-tests")


_JSONISH_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(allow_nan=True, allow_infinity=True, width=32),
    st.text(max_size=256),
)
_JSONISH = st.recursive(
    _JSONISH_SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(max_size=16), children, max_size=4),
    ),
    max_leaves=12,
)


@given(_JSONISH)
def test_parse_version_text_is_total_and_bounded(value: object) -> None:
    result = crafty.parse_version_text(value)

    assert result is None or result == "unknown" or (
        isinstance(result, str)
        and len(result) <= crafty._MAX_VERSION_LENGTH
        and crafty._VERSION_TOKEN.fullmatch(result) is not None
    )


@given(_JSONISH)
def test_ping_health_is_total_and_tristate(value: object) -> None:
    result = crafty._ping_health(value)

    assert result is None or result is True or result is False


@given(_JSONISH)
def test_parse_schedule_returns_valid_tuple_or_value_error(value: object) -> None:
    try:
        result = parse_schedule(value)
    except ValueError:
        return

    assert isinstance(result, tuple)
    assert all(isinstance(entry, ScheduleEntry) for entry in result)
    assert all(isinstance(entry.enabled, bool) for entry in result)


@given(st.text(max_size=256))
def test_systemd_show_property_helpers_are_total_for_current_contract(value: str) -> None:
    pid = systemd._parse_pid(value)
    assert pid is None or (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0)

    # T23's broader hostile-input contract is not on current main yet.  Keep
    # this branch compatible with the current string-input helper contract.
    started_at = systemd._parse_started_at(value)
    assert started_at is None or (
        isinstance(started_at, datetime)
        and started_at.tzinfo is not None
        and started_at.utcoffset() is not None
    )

    monotonic_started_at = systemd._parse_monotonic_started_at(value)
    assert monotonic_started_at is None or (
        isinstance(monotonic_started_at, datetime)
        and monotonic_started_at.tzinfo is not None
        and monotonic_started_at.utcoffset() is not None
    )


_SECRET = st.text(
    alphabet="bcfghijklmnopqsuvwxyz0123456789",
    min_size=8,
    max_size=32,
).filter(lambda value: value not in "[REDACTED]")


@given(secret=_SECRET, prefix=st.text(max_size=128), suffix=st.text(max_size=128))
def test_redaction_never_returns_a_marked_secret(
    secret: str,
    prefix: str,
    suffix: str,
) -> None:
    payload = f"{prefix}{secret}{suffix}"

    result = redaction.Redactor([secret]).redact(payload)

    assert secret not in result
