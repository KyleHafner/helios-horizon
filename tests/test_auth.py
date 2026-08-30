import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response

import game_control.auth as auth_module
from game_control.auth import (
    SessionStore,
    authenticate_proxy,
    load_proxy_credential,
    normalize_actor,
    set_session_cookie,
    validate_origin,
    verify_csrf,
)


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


def test_proxy_identity_rejection_paths_do_not_authenticate():
    with pytest.raises(LookupError, match="identity unavailable"):
        authenticate_proxy({"x-game-control-proxy": "secret"}, "secret")
    with pytest.raises(LookupError, match="identity unavailable"):
        authenticate_proxy(
            {"x-game-control-proxy": "secret", "x-authentik-username": "alice/../../root"},
            "secret",
        )
    with pytest.raises(LookupError, match="identity unavailable"):
        authenticate_proxy(
            {"x-game-control-proxy": "secret", "x-authentik-username": object()},
            "secret",
        )


def test_proxy_credential_loader_fails_closed_for_unavailable_or_malformed_values(monkeypatch):
    class CredentialPath:
        def __init__(self, value=None, error=None):
            self.value = value
            self.error = error

        def read_text(self, *, encoding):
            assert encoding == "utf-8"
            if self.error:
                raise self.error
            return self.value

    monkeypatch.setattr(auth_module, "PROXY_CREDENTIAL_PATH", CredentialPath(error=OSError("hidden")))
    with pytest.raises(PermissionError, match="proxy credential unavailable"):
        load_proxy_credential()

    for value in ("", " \t", "secret\nvalue", "secret\rvalue", "secret\x00"):
        monkeypatch.setattr(auth_module, "PROXY_CREDENTIAL_PATH", CredentialPath(value=value))
        with pytest.raises(PermissionError, match="proxy credential unavailable"):
            load_proxy_credential()

    monkeypatch.setattr(auth_module, "PROXY_CREDENTIAL_PATH", CredentialPath(value="  secret  \n"))
    assert load_proxy_credential() == "secret"


def test_sessions_bind_csrf_actor_and_expire():
    store = _store()
    session, csrf = store.create("operator")
    assert store.validate(session, csrf, actor="operator")
    assert not store.validate(session, csrf, actor="other")
    store._now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=9)
    assert not store.validate(session, csrf, actor="operator")
    store.db.close()


def test_session_validation_rejects_tampering_invalid_inputs_and_revocation():
    store = _store()
    session, csrf = store.create("operator")
    assert verify_csrf(store, session, csrf, "operator")
    assert not verify_csrf(store, session, None, "operator")
    assert not verify_csrf(store, None, csrf, "operator")
    assert not store.validate("unknown-session", csrf, actor="operator")
    assert not store.validate(session, csrf + "tampered", actor="operator")
    assert not store.validate(session, csrf, actor="operator/invalid")

    store.revoke(session)
    assert store.get(session).revoked_at is not None
    assert not verify_csrf(store, session, csrf, "operator")
    store.db.close()


def test_revoke_all_invalidates_only_current_sessions():
    store = _store()
    first, first_csrf = store.create("alice")
    second, second_csrf = store.create("bob")
    store.revoke(first)

    assert store.revoke_all() == 1
    assert store.validate(first, first_csrf, actor="alice") is False
    assert store.validate(second, second_csrf, actor="bob") is False
    assert store.revoke_all() == 0
    store.db.close()


def test_touch_rejects_unknown_expired_and_revoked_sessions():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = SessionStore(sqlite3.connect(":memory:"), now=lambda: now)
    session, _csrf = store.create("operator", ttl=timedelta(hours=2))
    assert not store.touch("unknown-session", ttl=timedelta(hours=2))

    now = now + timedelta(hours=3)
    assert not store.touch(session, ttl=timedelta(hours=2))
    store.db.close()

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store = SessionStore(sqlite3.connect(":memory:"), now=lambda: now)
    session, _csrf = store.create("operator", ttl=timedelta(hours=2))
    store.revoke(session)
    assert not store.touch(session, ttl=timedelta(hours=2))
    store.db.close()


def test_rotate_csrf_rejects_invalid_owners_and_replaces_token():
    store = _store()
    session, csrf = store.create("operator")
    rotated = store.rotate_csrf(session, actor="operator")
    assert rotated != csrf
    assert not store.validate(session, csrf, actor="operator")
    assert store.validate(session, rotated, actor="operator")

    with pytest.raises(ValueError, match="invalid session"):
        store.rotate_csrf("unknown-session", actor="operator")
    with pytest.raises(ValueError, match="invalid session"):
        store.rotate_csrf(session, actor="operator/invalid")

    store.revoke(session)
    with pytest.raises(ValueError, match="invalid session"):
        store.rotate_csrf(session, actor="operator")

    expired_store = _store()
    expired_session, _expired_csrf = expired_store.create("operator")
    expired_store._now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=9)
    with pytest.raises(ValueError, match="invalid session"):
        expired_store.rotate_csrf(expired_session, actor="operator")
    store.db.close()
    expired_store.db.close()


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
    store.db.close()


def test_actor_normalization_rejects_unsafe_values():
    assert normalize_actor("  Alice@Example.COM ") == "alice@example.com"
    with pytest.raises(ValueError):
        normalize_actor("alice/../../root")
    with pytest.raises(ValueError, match="invalid identity"):
        normalize_actor(None)


def test_cookie_is_secure_and_origin_requires_allowlist():
    response = Response()
    set_session_cookie(response, "opaque")
    cookie = response.headers["set-cookie"].casefold()
    assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert validate_origin("https://console.example", {"https://console.example"})
    assert not validate_origin("https://evil.example", {"https://console.example"})
    assert not validate_origin(None, {"https://console.example"})
    assert not validate_origin("", {"https://console.example"})
