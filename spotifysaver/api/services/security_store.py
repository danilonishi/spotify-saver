"""Persistent authentication and Spotify credential storage."""

import hashlib
import hmac
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

from ..config import APIConfig

PASSWORD_ITERATIONS = 600_000
SESSION_LIFETIME = timedelta(days=30)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_MAX_FAILURES = 5
SETUP_TOKEN_BYTES = 32


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _password_digest(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )


class SecurityStore:
    """Stores the single admin password, sessions, login limits, and Spotify setup."""

    def __init__(self, database_path: Optional[str] = None):
        self.database_path = Path(
            database_path or APIConfig.get_security_db_path()
        ).expanduser()
        self.setup_token: Optional[str] = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> Optional[str]:
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database_path.parent.chmod(0o700)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_credentials (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_login_attempts (
                    client_key TEXT PRIMARY KEY,
                    failed_count INTEGER NOT NULL,
                    window_started_at TEXT NOT NULL,
                    blocked_until TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spotify_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    client_id TEXT NOT NULL,
                    client_secret TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spotify_oauth_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
        self.database_path.chmod(0o600)

        if self.is_configured():
            self.setup_token = None
            return None
        self.setup_token = secrets.token_urlsafe(SETUP_TOKEN_BYTES)
        return self.setup_token

    def is_configured(self) -> bool:
        with self._connect() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM admin_credentials WHERE id = 1"
                ).fetchone()
                is not None
            )

    def complete_setup(self, token: str, password: str) -> bool:
        expected = self.setup_token
        if expected is None or not hmac.compare_digest(token, expected):
            return False
        salt = secrets.token_bytes(16)
        password_hash = _password_digest(password, salt)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO admin_credentials (id, salt, password_hash)
                VALUES (1, ?, ?)
                """,
                (salt, password_hash),
            )
            created = cursor.rowcount == 1
        if created:
            self.setup_token = None
        return created

    def verify_password(self, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT salt, password_hash FROM admin_credentials WHERE id = 1"
            ).fetchone()
        if row is None:
            return False
        actual = _password_digest(password, row[0])
        return hmac.compare_digest(actual, row[1])

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create_session(self) -> tuple[str, datetime]:
        token = secrets.token_urlsafe(32)
        expires_at = _utc_now() + SESSION_LIFETIME
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO auth_sessions (token_hash, expires_at) VALUES (?, ?)",
                (self._token_hash(token), expires_at.isoformat()),
            )
            connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?",
                (_utc_now().isoformat(),),
            )
        return token, expires_at

    def is_valid_session(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self._connect() as connection:
            row = connection.execute(
                "SELECT expires_at FROM auth_sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            ).fetchone()
        return row is not None and datetime.fromisoformat(row[0]) > _utc_now()

    def revoke_session(self, token: Optional[str]) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            )

    def login_is_blocked(self, client_key: str) -> bool:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT blocked_until FROM auth_login_attempts WHERE client_key = ?",
                (client_key,),
            ).fetchone()
        return bool(row and row[0] and datetime.fromisoformat(row[0]) > now)

    def record_login_failure(self, client_key: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT failed_count, window_started_at
                FROM auth_login_attempts WHERE client_key = ?
                """,
                (client_key,),
            ).fetchone()
            if row is None or datetime.fromisoformat(row[1]) + LOGIN_WINDOW <= now:
                count = 1
                window_started = now
            else:
                count = row[0] + 1
                window_started = datetime.fromisoformat(row[1])
            blocked_until = (
                (now + LOGIN_WINDOW).isoformat()
                if count >= LOGIN_MAX_FAILURES
                else None
            )
            connection.execute(
                """
                INSERT INTO auth_login_attempts
                    (client_key, failed_count, window_started_at, blocked_until)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(client_key) DO UPDATE SET
                    failed_count = excluded.failed_count,
                    window_started_at = excluded.window_started_at,
                    blocked_until = excluded.blocked_until
                """,
                (client_key, count, window_started.isoformat(), blocked_until),
            )

    def clear_login_failures(self, client_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_login_attempts WHERE client_key = ?",
                (client_key,),
            )

    def get_spotify_settings(self) -> Optional[dict[str, str]]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT client_id, client_secret, redirect_uri
                FROM spotify_settings WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "client_id": row[0],
            "client_secret": row[1],
            "redirect_uri": row[2],
        }

    def save_spotify_settings(
        self, client_id: str, client_secret: str, redirect_uri: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO spotify_settings (id, client_id, client_secret, redirect_uri)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    client_id = excluded.client_id,
                    client_secret = excluded.client_secret,
                    redirect_uri = excluded.redirect_uri
                """,
                (client_id, client_secret, redirect_uri),
            )
        self.database_path.chmod(0o600)

    def save_oauth_state(self, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO spotify_oauth_state (id, state, created_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state = excluded.state,
                    created_at = excluded.created_at
                """,
                (state, _utc_now().isoformat()),
            )

    def consume_oauth_state(self, state: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state, created_at FROM spotify_oauth_state WHERE id = 1"
            ).fetchone()
            if row is None:
                return False
            created_at = datetime.fromisoformat(row[1])
            valid = (
                _utc_now() - created_at <= timedelta(minutes=10)
                and hmac.compare_digest(row[0], state)
            )
            if valid or _utc_now() - created_at > timedelta(minutes=10):
                connection.execute(
                    "DELETE FROM spotify_oauth_state WHERE id = 1"
                )
        return valid

    def clear_spotify_settings(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM spotify_settings WHERE id = 1")
            connection.execute("DELETE FROM spotify_oauth_state WHERE id = 1")
