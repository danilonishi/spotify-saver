"""Authentication and first-run setup protections."""

import asyncio
import sqlite3
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request

from spotifysaver.api.routers.authentication import (
    InitialSetup,
    LoginCredentials,
    complete_initial_setup,
    get_authentication_status,
    login,
    logout,
)
from spotifysaver.api.routers.spotify_settings import (
    SpotifySettingsUpdate,
    save_spotify_settings,
)
from spotifysaver.api.services.security_store import SecurityStore


def _request(app, path="/api/v1/auth/status", method="GET", headers=()):
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": list(headers),
            "server": ("testserver", 8000),
            "client": ("127.0.0.1", 50000),
            "root_path": "",
            "app": app,
        }
    )


def test_setup_token_is_one_time_and_password_is_hashed(tmp_path):
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    setup_token = store.initialize()

    assert setup_token
    assert store.complete_setup(setup_token, "correct horse battery staple")
    assert not store.complete_setup(setup_token, "another secure password")
    assert store.setup_token is None
    assert store.verify_password("correct horse battery staple")
    assert not store.verify_password("wrong password")

    with sqlite3.connect(store.database_path) as connection:
        stored_password = connection.execute(
            "SELECT password_hash FROM admin_credentials"
        ).fetchone()[0]
    assert b"correct horse battery staple" not in stored_password


def test_login_failures_are_limited_and_reset_after_success(tmp_path):
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    token = store.initialize()
    assert token
    assert store.complete_setup(token, "correct horse battery staple")

    for _ in range(5):
        store.record_login_failure("127.0.0.1")

    assert store.login_is_blocked("127.0.0.1")
    store.clear_login_failures("127.0.0.1")
    assert not store.login_is_blocked("127.0.0.1")


def test_authentication_endpoints_issue_and_revoke_sessions(tmp_path):
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    setup_token = store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    request = _request(app, method="POST")
    response = Response()

    result = asyncio.run(
        complete_initial_setup(
            InitialSetup(
                setup_token=setup_token,
                password="correct horse battery staple",
            ),
            request,
            response,
        )
    )
    assert result["authenticated"] is True
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    token = cookie["spotifysaver_session"].value
    assert cookie["spotifysaver_session"]["httponly"]
    assert store.is_valid_session(token)
    with store._connect() as connection:
        stored_session_hash = connection.execute(
            "SELECT token_hash FROM auth_sessions"
        ).fetchone()[0]
    assert token not in stored_session_hash

    status = asyncio.run(
        get_authentication_status(
            _request(app, headers=[(b"cookie", f"spotifysaver_session={token}".encode())])
        )
    )
    assert status == {"configured": True, "authenticated": True}

    logout_response = Response()
    asyncio.run(
        logout(
            _request(
                app,
                method="POST",
                headers=[(b"cookie", f"spotifysaver_session={token}".encode())],
            ),
            logout_response,
        )
    )
    assert not store.is_valid_session(token)

    login_response = Response()
    login_result = asyncio.run(
        login(
            LoginCredentials(password="correct horse battery staple"),
            _request(app, method="POST"),
            login_response,
        )
    )
    assert login_result["authenticated"] is True
    assert "spotifysaver_session=" in login_response.headers["set-cookie"]


def test_session_cookie_is_secure_behind_https_reverse_proxy(tmp_path):
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    setup_token = store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    response = Response()

    asyncio.run(
        complete_initial_setup(
            InitialSetup(
                setup_token=setup_token,
                password="correct horse battery staple",
            ),
            _request(
                app,
                method="POST",
                headers=[(b"x-forwarded-proto", b"https")],
            ),
            response,
        )
    )

    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    assert cookie["spotifysaver_session"]["secure"]


def test_api_auth_middleware_rejects_unauthenticated_and_cross_origin_requests(
    tmp_path,
):
    from spotifysaver.api.app import create_app

    app = create_app()
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    setup_token = store.initialize()
    assert setup_token
    app.state.security_store = store

    middleware = next(
        item
        for item in app.user_middleware
        if "dispatch" in item.kwargs and item.kwargs["dispatch"].__name__ == "require_authentication"
    )
    dispatch = middleware.kwargs["dispatch"]

    async def continue_request(_request):
        return Response("ok")

    unauthorized = asyncio.run(
        dispatch(_request(app, "/api/v1/queue"), continue_request)
    )
    assert unauthorized.status_code == 401

    store.complete_setup(setup_token, "correct horse battery staple")
    token, _ = store.create_session()
    cross_origin = asyncio.run(
        dispatch(
            _request(
                app,
                "/api/v1/queue",
                method="POST",
                headers=[
                    (b"cookie", f"spotifysaver_session={token}".encode()),
                    (b"origin", b"https://attacker.example"),
                ],
            ),
            continue_request,
        )
    )
    assert cross_origin.status_code == 403


def test_spotify_secret_is_persisted_but_never_returned(tmp_path, monkeypatch):
    from spotifysaver.config import Config

    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", None)
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", None)
    monkeypatch.setattr(Config, "SPOTIFY_REDIRECT_URI", "")
    monkeypatch.setattr(
        Config, "SPOTIFY_AUTH_CACHE_PATH", str(tmp_path / ".spotify-cache")
    )
    request = _request(app, "/api/v1/spotify/settings", method="PUT")

    result = asyncio.run(
        save_spotify_settings(
            SpotifySettingsUpdate(
                client_id="spotify-client-id",
                client_secret="top-secret-value",
                redirect_uri="https://music.example.com/api/v1/spotify/callback",
            ),
            request,
        )
    )

    assert result["credentials_configured"] is True
    assert "top-secret-value" not in str(result)
    assert store.get_spotify_settings()["client_secret"] == "top-secret-value"


def test_legacy_spotify_callback_url_is_not_suggested(tmp_path, monkeypatch):
    from spotifysaver.api.routers.spotify_settings import get_spotify_settings
    from spotifysaver.config import Config

    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", None)
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", None)
    monkeypatch.setattr(
        Config, "SPOTIFY_AUTH_CACHE_PATH", str(tmp_path / ".spotify-cache")
    )
    monkeypatch.setenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    result = asyncio.run(
        get_spotify_settings(_request(app, "/api/v1/spotify/settings"))
    )

    assert result["redirect_uri"] == ""


def test_spotify_authorization_rejects_legacy_redirect_url(tmp_path, monkeypatch):
    from spotifysaver.api.routers import spotify_settings
    from spotifysaver.config import Config

    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", "spotify-client-id")
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", "spotify-client-secret")
    monkeypatch.setattr(Config, "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            spotify_settings.start_spotify_authorization(
                _request(app, "/api/v1/spotify/authorization", method="POST")
            )
        )

    assert error.value.status_code == 400


def test_spotify_authorization_start_stores_state_and_returns_url(
    tmp_path, monkeypatch
):
    from spotifysaver.api.routers import spotify_settings
    from spotifysaver.config import Config

    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", "spotify-client-id")
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", "spotify-client-secret")
    monkeypatch.setattr(
        Config,
        "SPOTIFY_REDIRECT_URI",
        "https://music.example.com/api/v1/spotify/callback",
    )
    monkeypatch.setattr(
        Config, "SPOTIFY_AUTH_CACHE_PATH", str(tmp_path / ".spotify-cache")
    )
    issued_states = []

    class FakeOAuth:
        def get_authorize_url(self, state):
            issued_states.append(state)
            return "https://accounts.spotify.com/authorize?state=" + state

    monkeypatch.setattr(spotify_settings, "_oauth", lambda: FakeOAuth())

    result = asyncio.run(
        spotify_settings.start_spotify_authorization(
            _request(app, "/api/v1/spotify/authorization", method="POST")
        )
    )

    assert result["authorization_url"].endswith(issued_states[0])
    assert store.consume_oauth_state(issued_states[0])


def test_spotify_callback_exchanges_code_and_persists_token_cache(
    tmp_path, monkeypatch
):
    from spotifysaver.api.routers import spotify_settings
    from spotifysaver.config import Config

    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    store.save_oauth_state("valid-oauth-state")
    app = SimpleNamespace(state=SimpleNamespace(security_store=store))
    cache_path = tmp_path / ".spotify-cache"
    monkeypatch.setattr(Config, "SPOTIFY_AUTH_CACHE_PATH", str(cache_path))
    exchanged = []

    class FakeOAuth:
        def get_access_token(self, code, check_cache):
            exchanged.append((code, check_cache))
            cache_path.write_text("cached token")

    monkeypatch.setattr(spotify_settings, "_oauth", lambda: FakeOAuth())

    response = asyncio.run(
        spotify_settings.spotify_authorization_callback(
            _request(app, "/api/v1/spotify/callback"),
            code="authorization-code",
            state="valid-oauth-state",
        )
    )

    assert response.status_code == 200
    assert "Spotify is connected" in response.body.decode()
    assert exchanged == [("authorization-code", False)]
    assert cache_path.exists()


def test_invalid_oauth_state_does_not_consume_valid_state(tmp_path):
    store = SecurityStore(str(tmp_path / "security.sqlite3"))
    store.initialize()
    store.save_oauth_state("expected-state")

    assert not store.consume_oauth_state("attacker-state")
    assert store.consume_oauth_state("expected-state")
    assert not store.consume_oauth_state("expected-state")
