import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

import spotifysaver.api.routers.download as download_router
from spotifysaver.api.routers.download import inspect_spotify_url, start_download, tasks
from spotifysaver.api.schemas import DownloadRequest
from spotifysaver.config import Config
from spotifysaver.services.errors.errors import SpotifyAuthorizationRequired
from spotifysaver.services.spotify_api import SpotifyAPI


SPOTIFY_TRACK_URL = "https://open.spotify.com/track/6TT9QrhtNpgWdrFGfjfV02"


def test_spotify_api_requires_credentials_with_setup_instructions(monkeypatch):
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", None)
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", None)

    with pytest.raises(SpotifyAuthorizationRequired, match="Settings > Spotify"):
        SpotifyAPI()


def test_spotify_api_requires_cached_user_authorization(monkeypatch, tmp_path):
    class FakeOAuth:
        def __init__(self, **_kwargs):
            pass

        def get_cached_token(self):
            return None

    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setattr(Config, "SPOTIFY_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(Config, "SPOTIFY_AUTH_CACHE_PATH", str(tmp_path / "cache"))
    monkeypatch.setattr("spotifysaver.services.spotify_api.SpotifyOAuth", FakeOAuth)

    with pytest.raises(SpotifyAuthorizationRequired, match="Connect Spotify"):
        SpotifyAPI()


def test_spotify_download_is_rejected_before_it_is_queued(monkeypatch):
    message = (
        "Spotify account authorization is required. Open Settings > Spotify "
        "and select Connect Spotify before downloading."
    )

    def require_authorization():
        raise SpotifyAuthorizationRequired(message)

    monkeypatch.setattr(download_router, "SpotifyAPI", require_authorization)
    initial_task_ids = set(tasks)
    request = DownloadRequest(spotify_url=SPOTIFY_TRACK_URL)

    with pytest.raises(HTTPException) as error:
        asyncio.run(start_download(request, BackgroundTasks()))

    assert error.value.status_code == 400
    assert error.value.detail == message
    assert set(tasks) == initial_task_ids


def test_spotify_inspection_returns_actionable_authorization_error(monkeypatch):
    message = "Spotify account authorization is required. Connect Spotify in Settings."

    def require_authorization():
        raise SpotifyAuthorizationRequired(message)

    monkeypatch.setattr(download_router, "SpotifyAPI", require_authorization)

    with pytest.raises(HTTPException) as error:
        asyncio.run(inspect_spotify_url(SPOTIFY_TRACK_URL))

    assert error.value.status_code == 400
    assert error.value.detail == message
