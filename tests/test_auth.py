import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response

from game_control.auth import SessionStore, authenticate_proxy, normalize_actor, set_session_cookie, validate_origin


def _store():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE web_sessions (session_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL, revoked_at TEXT)"
    )
    return SessionStore(db, now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_proxy_and_identity_are_required_and_constant_time():
    assert authenticate_proxy({"x-game-control-proxy": "secret", "x-authentik-username": " Alice "}, "secret") == "alice"
    with pytest.raises(PermissionError):
        authenticate_proxy({"x-authentik-username": "alice"}, "secret")
    with pytest.raises(PermissionError):
        authenticate_proxy({"x-game-control-proxy": "wrong", "x-authentik-username": "alice"}, "secret")


def test_sessions_bind_csrf_actor_and_expire():
    store = _store()
    session, csrf = store.create("operator")
    assert store.validate(session, csrf, actor="operator")
    assert not store.validate(session, csrf, actor="other")
    store._now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=9)
    assert not store.validate(session, csrf, actor="operator")


def test_touch_noop_before_halfway_and_extends_after_halfway():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = SessionStore(sqlite3.connect(":memory:"), now=lambda: now)
    session, _csrf = store.create("operator", ttl=timedelta(hours=2))
    original = store.get(session).expires_at
    assert not store.touch(session, ttl=timedelta(hours=2))
    assert store.get(session).expires_at == original

    now = now + timedelta(hours=1, minutes=1)
    assert store.touch(session, ttl=timedelta(hours=2))
    assert store.get(session).expires_at == now + timedelta(hours=2)


def test_actor_normalization_rejects_unsafe_values():
    assert normalize_actor("  Alice@Example.COM ") == "alice@example.com"
    with pytest.raises(ValueError):
        normalize_actor("alice/../../root")


def test_cookie_is_secure_and_origin_requires_allowlist():
    response = Response()
    set_session_cookie(response, "opaque")
    cookie = response.headers["set-cookie"].casefold()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert validate_origin("https://console.example", {"https://console.example"})
    assert not validate_origin("https://evil.example", {"https://console.example"})
