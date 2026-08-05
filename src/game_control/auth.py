"""Authentication and browser-session boundary for the unprivileged web app.

Only the reverse-proxy credential and Authentik identity cross this boundary.
Operational data is deliberately absent from this module; sessions and CSRF
tokens are hashes in the fixed web database.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from .web_db import WEB_DB_PATH, WebDatabase

PROXY_CREDENTIAL_PATH = Path("/run/credentials/game-control-web.service/proxy-token")
SESSION_COOKIE = "game_control_session"
SESSION_TTL = timedelta(hours=8)
_ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9@._-]{0,127}$")


def load_proxy_credential() -> str:
    """Read only the dedicated systemd credential, never an environment secret."""

    try:
        value = PROXY_CREDENTIAL_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PermissionError("proxy credential unavailable") from exc
    value = value.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise PermissionError("proxy credential unavailable")
    return value


def normalize_actor(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid identity")
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not _ACTOR_RE.fullmatch(normalized):
        raise ValueError("invalid identity")
    return normalized


def _headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {str(key).casefold(): str(value) for key, value in headers.items()}


def authenticate_proxy(headers: Mapping[str, str], credential: str | None = None) -> str:
    """Validate proxy authentication and return the normalized Authentik actor."""

    presented = _headers(headers).get("x-game-control-proxy", "")
    expected = credential if credential is not None else load_proxy_credential()
    if not isinstance(expected, str) or not hmac.compare_digest(presented, expected):
        raise PermissionError("proxy authentication failed")
    identity = _headers(headers).get("x-authentik-username", "")
    if not identity:
        raise LookupError("identity unavailable")
    try:
        return normalize_actor(identity)
    except ValueError as exc:
        raise LookupError("identity unavailable") from exc


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_origin(origin: str | None, allowed_origins: set[str] | frozenset[str]) -> bool:
    """Require an explicit exact-origin match for browser mutations."""

    return isinstance(origin, str) and bool(origin) and origin in allowed_origins


def verify_csrf(store: "SessionStore", session: str, csrf: str | None, actor: str) -> bool:
    """Public seam for tests and non-FastAPI callers."""

    return store.validate(session, csrf, actor=actor)


@dataclass(frozen=True)
class Session:
    session_hash: str
    csrf_hash: str
    actor: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None


class SessionStore:
    """Small fixed-schema session store.

    ``db`` is an injected connection for tests. Production callers use
    :meth:`open`, which can only open ``/var/lib/game-control-web/web.db``.
    """

    def __init__(self, db: sqlite3.Connection, *, now: Callable[[], datetime] | None = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ensure_schema()

    @classmethod
    def open(cls) -> "SessionStore":
        database = WebDatabase.open(WEB_DB_PATH)
        return cls(database.connection)

    def _ensure_schema(self) -> None:
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS web_sessions (
                session_hash TEXT PRIMARY KEY,
                csrf_hash TEXT NOT NULL,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT
            )
            """
        )
        # The migration keeps old databases readable while auth writes only
        # the new, hashed columns.  The old compatibility columns contain no
        # operational data and are not consulted.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(web_sessions)")}
        for name, declaration in (
            ("session_hash", "TEXT"),
            ("csrf_hash", "TEXT"),
            ("last_seen_at", "TEXT"),
            ("revoked_at", "TEXT"),
        ):
            if name not in columns:
                self.db.execute(f"ALTER TABLE web_sessions ADD COLUMN {name} {declaration}")
        self.db.commit()

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    def create(self, actor: str, *, ttl: timedelta = SESSION_TTL) -> tuple[str, str]:
        actor = normalize_actor(actor)
        now = self._now().astimezone(timezone.utc)
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(web_sessions)")}
        values_by_name = {
            "session_hash": token_hash(session),
            "csrf_hash": token_hash(csrf),
            "actor": actor,
            "created_at": self._iso(now),
            "last_seen_at": self._iso(now),
            "expires_at": self._iso(now + ttl),
            "revoked_at": None,
        }
        names = ["session_hash", "csrf_hash", "actor", "created_at", "last_seen_at", "expires_at", "revoked_at"]
        if "id" in columns:
            names.insert(0, "id")
            values_by_name["id"] = token_hash(session)
        if "csrf_token" in columns:
            names.insert(names.index("csrf_hash"), "csrf_token")
            values_by_name["csrf_token"] = token_hash(csrf)
        values = [values_by_name[name] for name in names]
        placeholders = ",".join("?" for _ in names)
        self.db.execute(f"INSERT INTO web_sessions({','.join(names)}) VALUES({placeholders})", values)
        self.db.commit()
        return session, csrf

    def _row(self, session: str) -> tuple | None:
        if not isinstance(session, str) or not session:
            return None
        return self.db.execute(
            "SELECT session_hash, csrf_hash, actor, created_at, last_seen_at, expires_at, revoked_at FROM web_sessions WHERE session_hash=?",
            (token_hash(session),),
        ).fetchone()

    def validate(self, session: str, csrf: str | None, *, actor: str) -> bool:
        try:
            actor = normalize_actor(actor)
        except ValueError:
            return False
        row = self._row(session)
        if row is None:
            return False
        session_hash, csrf_hash, row_actor, created, last_seen, expires, revoked = row
        now = self._now().astimezone(timezone.utc)
        if (
            not hmac.compare_digest(str(row_actor), actor)
            or not csrf
            or not hmac.compare_digest(str(csrf_hash), token_hash(csrf))
            or revoked is not None
            or self._parse(expires) <= now
        ):
            return False
        self.db.execute("UPDATE web_sessions SET last_seen_at=? WHERE session_hash=?", (self._iso(now), session_hash))
        self.db.commit()
        return True

    def touch(self, session: str, *, ttl: timedelta = SESSION_TTL) -> bool:
        """Extend expiry after the current session has crossed its halfway point."""
        now = self._now().astimezone(timezone.utc)
        record = self.get(session)
        if record is None or record.revoked_at is not None or record.expires_at <= now:
            return False
        if (record.expires_at - now) > ttl / 2:
            return False
        self.db.execute(
            "UPDATE web_sessions SET expires_at=?, last_seen_at=? WHERE session_hash=?",
            (self._iso(now + ttl), self._iso(now), token_hash(session)),
        )
        self.db.commit()
        return True

    def rotate_csrf(self, session: str, *, actor: str) -> str:
        """Replace and return the CSRF token for a valid session owner."""

        try:
            actor = normalize_actor(actor)
        except ValueError as exc:
            raise ValueError("invalid session") from exc
        row = self._row(session)
        if row is None:
            raise ValueError("invalid session")
        session_hash, _csrf_hash, row_actor, _created, _last_seen, expires, revoked = row
        now = self._now().astimezone(timezone.utc)
        if (
            not hmac.compare_digest(str(row_actor), actor)
            or revoked is not None
            or self._parse(expires) <= now
        ):
            raise ValueError("invalid session")

        csrf = secrets.token_urlsafe(32)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(web_sessions)")}
        assignments = ["csrf_hash=?", "last_seen_at=?"]
        values = [token_hash(csrf), self._iso(now)]
        if "csrf_token" in columns:
            assignments.insert(1, "csrf_token=?")
            values.insert(1, token_hash(csrf))
        values.append(session_hash)
        self.db.execute(
            f"UPDATE web_sessions SET {','.join(assignments)} WHERE session_hash=?",
            values,
        )
        self.db.commit()
        return csrf

    def get(self, session: str) -> Session | None:
        row = self._row(session)
        if row is None:
            return None
        return Session(
            session_hash=row[0],
            csrf_hash=row[1],
            actor=row[2],
            created_at=self._parse(row[3]),
            last_seen_at=self._parse(row[4]),
            expires_at=self._parse(row[5]),
            revoked_at=self._parse(row[6]) if row[6] else None,
        )

    def revoke(self, session: str) -> None:
        self.db.execute(
            "UPDATE web_sessions SET revoked_at=? WHERE session_hash=?",
            (self._iso(self._now().astimezone(timezone.utc)), token_hash(session)),
        )
        self.db.commit()


def set_session_cookie(response, session: str, *, max_age: int = int(SESSION_TTL.total_seconds())) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=max_age,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


__all__ = [
    "PROXY_CREDENTIAL_PATH",
    "SESSION_COOKIE",
    "SESSION_TTL",
    "Session",
    "SessionStore",
    "authenticate_proxy",
    "load_proxy_credential",
    "normalize_actor",
    "set_session_cookie",
    "token_hash",
    "validate_origin",
    "verify_csrf",
]
