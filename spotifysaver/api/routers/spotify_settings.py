"""Spotify credential management and browser-based account authorization."""

import secrets
import os
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from spotipy.exceptions import SpotifyOauthError
from spotipy.oauth2 import SpotifyOAuth

from ...config import Config
from ..services.security_store import SecurityStore

router = APIRouter()
CALLBACK_PATH = "/api/v1/spotify/callback"


class SpotifySettingsUpdate(BaseModel):
    client_id: str = Field(min_length=1, max_length=128)
    client_secret: str = Field(default="", max_length=256)
    redirect_uri: str = Field(min_length=1, max_length=2048)


def _store(request: Request) -> SecurityStore:
    store: SecurityStore | None = getattr(request.app.state, "security_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Spotify settings are unavailable")
    return store


def _apply_spotify_settings(settings: dict[str, str] | None) -> None:
    if settings is None:
        return
    Config.SPOTIFY_CLIENT_ID = settings["client_id"]
    Config.SPOTIFY_CLIENT_SECRET = settings["client_secret"]
    Config.SPOTIFY_REDIRECT_URI = settings["redirect_uri"]


def _oauth() -> SpotifyOAuth:
    if not Config.SPOTIFY_CLIENT_ID or not Config.SPOTIFY_CLIENT_SECRET:
        raise HTTPException(
            status_code=400, detail="Save Spotify application credentials first"
        )
    cache_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.parent.chmod(0o700)
    return SpotifyOAuth(
        client_id=Config.SPOTIFY_CLIENT_ID,
        client_secret=Config.SPOTIFY_CLIENT_SECRET,
        redirect_uri=Config.SPOTIFY_REDIRECT_URI,
        scope=Config.SPOTIFY_SCOPES,
        cache_path=str(cache_path),
        open_browser=False,
    )


def _validate_redirect_uri(value: str) -> str:
    redirect_uri = value.strip()
    parsed = urlsplit(redirect_uri)
    local_http = parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if (
        not parsed.hostname
        or (parsed.scheme != "https" and not local_http)
        or parsed.path != CALLBACK_PATH
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Use an HTTPS redirect URL ending in "
                f"{CALLBACK_PATH} (HTTP is allowed only for localhost)"
            ),
        )
    return redirect_uri


@router.get("/spotify/settings")
async def get_spotify_settings(request: Request):
    """Return Spotify setup status without returning the application secret."""
    stored = _store(request).get_spotify_settings()
    client_id = stored["client_id"] if stored else Config.SPOTIFY_CLIENT_ID
    redirect_uri = (
        stored["redirect_uri"]
        if stored
        else os.getenv("SPOTIFY_REDIRECT_URI", "")
    )
    if redirect_uri and urlsplit(redirect_uri).path != CALLBACK_PATH:
        redirect_uri = ""
    configured = bool(client_id and Config.SPOTIFY_CLIENT_SECRET)
    token_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
    return {
        "client_id": client_id or "",
        "redirect_uri": redirect_uri,
        "client_secret_configured": bool(Config.SPOTIFY_CLIENT_SECRET),
        "credentials_configured": configured,
        "authorized": bool(
            configured
            and token_path.exists()
            and _oauth().get_cached_token()
        ),
    }


@router.put("/spotify/settings")
async def save_spotify_settings(
    settings: SpotifySettingsUpdate, request: Request
):
    """Persist Spotify app credentials without ever returning the secret."""
    store = _store(request)
    client_id = settings.client_id.strip()
    redirect_uri = _validate_redirect_uri(settings.redirect_uri)
    if not client_id:
        raise HTTPException(status_code=400, detail="Spotify Client ID is required")

    secret = settings.client_secret.strip()
    if not secret:
        secret = Config.SPOTIFY_CLIENT_SECRET or ""
    if not secret:
        raise HTTPException(status_code=400, detail="Spotify Client Secret is required")

    previous = store.get_spotify_settings()
    if previous and (
        previous["client_id"] != client_id
        or previous["client_secret"] != secret
        or previous["redirect_uri"] != redirect_uri
    ):
        cache_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
        if cache_path.exists():
            cache_path.unlink()
    store.save_spotify_settings(client_id, secret, redirect_uri)
    _apply_spotify_settings(
        {
            "client_id": client_id,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
        }
    )
    return await get_spotify_settings(request)


@router.delete("/spotify/settings")
async def clear_spotify_settings(request: Request):
    """Remove stored Spotify app credentials and cached user authorization."""
    store = _store(request)
    store.clear_spotify_settings()
    cache_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
    if cache_path.exists():
        cache_path.unlink()
    Config.SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
    Config.SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
    Config.SPOTIFY_REDIRECT_URI = os.getenv(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"
    )
    return await get_spotify_settings(request)


@router.post("/spotify/authorization")
async def start_spotify_authorization(request: Request):
    """Create a single-use OAuth state and return Spotify's consent URL."""
    store = _store(request)
    if not Config.SPOTIFY_CLIENT_ID or not Config.SPOTIFY_CLIENT_SECRET:
        raise HTTPException(
            status_code=400, detail="Save Spotify application credentials first"
        )
    _validate_redirect_uri(Config.SPOTIFY_REDIRECT_URI)
    state = secrets.token_urlsafe(32)
    store.save_oauth_state(state)
    authorization_url = _oauth().get_authorize_url(state=state)
    return {"authorization_url": authorization_url}


@router.get("/spotify/callback", response_class=HTMLResponse)
async def spotify_authorization_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
):
    """Validate Spotify's OAuth state and persist the returned access token."""
    store = _store(request)
    if not state or not store.consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Spotify authorization state is invalid")
    if error or not code:
        message = "Spotify authorization was cancelled or denied."
        return HTMLResponse(_callback_page(message, success=False), status_code=400)

    try:
        _oauth().get_access_token(code=code, check_cache=False)
        cache_path = Path(Config.SPOTIFY_AUTH_CACHE_PATH).expanduser()
        if cache_path.exists():
            cache_path.chmod(0o600)
    except (OSError, SpotifyOauthError, ValueError):
        return HTMLResponse(
            _callback_page(
                "Spotify authorization failed. Return to SpotifySaver Settings and try again.",
                success=False,
            ),
            status_code=502,
        )
    return HTMLResponse(
        _callback_page(
            "Spotify is connected. You can close this page and return to SpotifySaver.",
            success=True,
        )
    )


def _callback_page(message: str, success: bool) -> str:
    heading = "Spotify connected" if success else "Spotify connection failed"
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading}</title></head>
<body><main><h1>{heading}</h1><p>{escape(message)}</p>
<a href="/">Return to SpotifySaver</a></main></body></html>"""
