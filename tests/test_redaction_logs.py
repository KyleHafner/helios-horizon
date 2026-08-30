from pathlib import Path

import pytest

import game_control.redaction as redaction_module
from game_control.logs import LogService, clamp_since
from game_control.protocol import LogLine
from game_control.redaction import Redactor, SecretRegistry


def test_recognizable_secrets_are_redacted():
    telegram_token = "123456789:AAECAwQFBgcICQoLDA0ODx"
    text = (
        "Bearer abcdefghijklmnop "
        "https://discord.com/api/webhooks/123/token "
        f"telegram {telegram_token} "
        "password=hunter2 cookie=session-value "
        "-----BEGIN PRIVATE KEY-----secret"
    )
    output = Redactor(["configured-secret"]).redact(text + " configured-secret")
    assert "[REDACTED]" in output
    assert all(
        secret not in output
        for secret in ("abcdefghijklmnop", telegram_token, "hunter2", "session-value", "configured-secret")
    )


def test_secret_registry_accepts_mapping_and_filters_empty_or_non_text_values():
    registry = SecretRegistry({"proxy": "configured-secret", "empty": "", "number": 42})
    registry.add("added-secret")
    registry.add("")
    registry.add(None)

    assert set(registry.values()) == {"configured-secret", "added-secret"}
    output = Redactor(registry).redact("configured-secret added-secret")
    assert output == "[REDACTED] [REDACTED]"


@pytest.mark.parametrize("field", ["passwd", "pass", "secret", "token", "api_key", "api-key", "authorization"])
def test_all_generic_secret_fields_are_redacted(field):
    value = f"{field}-value"
    output = Redactor().redact(f"{field}={value}")
    assert value not in output
    assert output == "[REDACTED]"


@pytest.mark.parametrize("field", ["session", "connect.sid", "auth_token", "jwt-cookie"])
def test_all_session_cookie_shapes_are_redacted(field):
    value = f"{field}-value"
    output = Redactor().redact(f"{field}={value}")
    assert value not in output
    assert "[REDACTED]" in output


def test_redaction_fails_closed_for_malformed_input_and_regex_failure(monkeypatch):
    redactor = Redactor()
    assert redactor.redact(None) == "[REDACTED]"
    assert redactor.redact(42) == "[REDACTED]"
    assert redactor.redact("x" * (512 * 1024 + 1)) == "[REDACTED]"

    class BrokenPattern:
        def sub(self, _replacement, _text):
            raise redaction_module.re.error("malformed pattern")

    monkeypatch.setattr(redaction_module, "_PATTERNS", (BrokenPattern(),))
    assert redactor.redact("otherwise safe text") == "[REDACTED]"


def test_truncated_private_key_is_fail_closed():
    output = Redactor().redact("prefix -----BEGIN RSA PRIVATE KEY-----\nTOP-SECRET\n")
    assert "TOP-SECRET" not in output
    assert "BEGIN RSA PRIVATE KEY" not in output


def test_generic_private_key_profiles_are_redacted_across_lines():
    output = Redactor().redact("-----BEGIN PRIVATE KEY-----\nsecret-line\n-----END PRIVATE KEY-----")
    assert "secret-line" not in output
    assert "PRIVATE KEY" not in output


def test_log_tail_redacts_pem_before_splitting(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nprivate-value\n-----END OPENSSH PRIVATE KEY-----\n")
    page = LogService().tail_from_paths((path,), limit=10)
    assert all("private-value" not in line.message for line in page)


def test_log_limits_and_diagnostics_are_bounded_and_allowlisted(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("password=secret\nhealthy line\n")
    service = LogService(redactor=Redactor(["secret"]))
    page = service.tail_from_paths((path,), limit=999999)
    assert len(page) == 2
    assert all("secret" not in line.message for line in page)
    diagnostics = service.diagnostics({"state": "running", "env": {"TOKEN": "secret"}, "cmdline": ["--password=secret"]})
    assert diagnostics == {"state": "running"}


def test_tail_reads_newest_bounded_bytes_and_discards_partial_line(tmp_path):
    path = tmp_path / "server.log"
    path.write_bytes(b"old-line\n" + b"x" * (300 * 1024) + b"\nnewest-line\n")
    page = LogService().tail_from_paths((path,), limit=5)
    messages = [line.message for line in page]
    assert "newest-line" in messages
    assert "old-line" not in messages


def test_since_excludes_lines_without_trustworthy_timestamps(tmp_path):
    path = tmp_path / "server.log"
    path.write_text("unparsed READY\n")
    from datetime import datetime, timedelta, timezone

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert LogService().tail_from_paths((path,), limit=5, since=since) == ()


def test_adversarial_regex_is_treated_as_literal():
    service = LogService()
    result = service.search_lines(["a.*b", "axxb"], "a.*b", regex=True)
    assert [line.message for line in result] == ["a.*b"]


def test_adapter_pem_redaction_preserves_record_metadata():
    from datetime import datetime, timezone

    class Adapter:
        async def recent_logs(self, profile, limit):
            now = datetime.now(timezone.utc)
            return [
                LogLine(timestamp=now, severity="error", message="-----BEGIN PRIVATE KEY-----"),
                LogLine(timestamp=now, severity="error", message="secret"),
                LogLine(timestamp=now, severity="info", message="-----END PRIVATE KEY-----"),
                LogLine(timestamp=now, severity="info", message="after"),
            ]

    import asyncio

    output = asyncio.run(LogService(Adapter()).tail(object(), limit=10))
    assert len(output) == 4
    assert output[-1].message == "after"
    assert all(item.message != "secret" for item in output)


def test_adapter_tail_applies_trustworthy_since_cutoff():
    from datetime import datetime, timezone, timedelta

    class Adapter:
        async def recent_logs(self, profile, limit):
            return [
                LogLine(
                    timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
                    severity="info",
                    message="old",
                ),
                LogLine(
                    timestamp=datetime.now(timezone.utc),
                    severity="info",
                    message="new",
                ),
            ]

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)
    import asyncio

    output = asyncio.run(LogService(Adapter()).tail(object(), limit=10, since=cutoff))
    assert [line.message for line in output] == ["new"]


def test_naive_adapter_timestamp_is_rejected_before_merge():
    from datetime import datetime
    import asyncio

    class Adapter:
        async def recent_logs(self, profile, limit):
            return [LogLine(timestamp=datetime.now(), severity="info", message="naive")]

    assert asyncio.run(LogService(Adapter()).tail(object(), limit=10)) == ()


def test_unmatched_end_redacts_all_prior_page_records():
    redactor = Redactor()
    output = redactor.redact_records(["before-secret", "-----END PRIVATE KEY-----", "after"])
    assert output == ["[REDACTED]", "[REDACTED]", "after"]


def test_inline_pem_redaction_preserves_prior_records():
    output = Redactor().redact_records(
        ["ordinary record", "-----BEGIN PRIVATE KEY-----inline-----END PRIVATE KEY-----", "after"]
    )
    assert output == ["ordinary record", "[REDACTED]", "after"]


def test_end_then_begin_markers_open_a_new_redaction_span():
    output = Redactor().redact_records(
        [
            "-----BEGIN PRIVATE KEY-----",
            "first-secret",
            "-----END PRIVATE KEY----- -----BEGIN PRIVATE KEY-----",
            "second-secret",
            "-----END PRIVATE KEY-----",
            "after",
        ]
    )
    assert output == ["[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]", "[REDACTED]", "after"]


def test_adapter_errors_preserve_file_fallback_for_tail_and_search(tmp_path):
    from game_control.adapters.base import AdapterError
    import asyncio

    path = tmp_path / "server.log"
    path.write_text("fallback line\n")

    class Adapter:
        async def recent_logs(self, _profile, _limit):
            raise AdapterError("journal unavailable")

    profile = type("Profile", (), {"paths": type("Paths", (), {"log_files": (path,)})()})()
    service = LogService(Adapter())
    tail = asyncio.run(service.tail(profile, limit=10))
    search = asyncio.run(service.search(profile, "fallback", limit=10))
    assert [line.message for line in tail] == ["fallback line"]
    assert [line.message for line in search] == ["fallback line"]


def test_trustworthy_adapter_records_replace_untimestamped_file_duplicates(tmp_path):
    from datetime import datetime, timezone
    import asyncio

    path = tmp_path / "server.log"
    path.write_text("same event without source timestamp\n")
    occurred_at = datetime(2026, 7, 12, 21, 30, tzinfo=timezone.utc)

    class Adapter:
        async def recent_logs(self, _profile, _limit):
            return [LogLine(timestamp=occurred_at, severity="info", message="same event")]

    profile = type("Profile", (), {"paths": type("Paths", (), {"log_files": (path,)})()})()
    output = asyncio.run(LogService(Adapter()).tail(profile, limit=10))

    assert [(line.timestamp, line.message) for line in output] == [(occurred_at, "same event")]


def test_since_clamps_to_24_hours_and_preserves_none():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
    assert clamp_since(None, now=now) is None
    assert clamp_since(now - timedelta(days=3), now=now) == now - timedelta(hours=24)
    recent = now - timedelta(hours=2)
    assert clamp_since(recent, now=now) == recent
