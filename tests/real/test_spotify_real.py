import pytest
import logging
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from spotifysaver.config.setting_environment import Config
from spotifysaver.services.spotify_api import SpotifyAPI

SPOTIFY_TRACK_URL = (
    "https://open.spotify.com/track/"
    "6HPd9dB8tO5luKDtXpjivN?si=31a49f8db2ac4cdc"
)
SPOTIFY_TRACK_ID = "6HPd9dB8tO5luKDtXpjivN"


@pytest.mark.real
def test_real_spotify_credentials_can_fetch_the_supplied_public_track():
    if not Config.SPOTIFY_CLIENT_ID or not Config.SPOTIFY_CLIENT_SECRET:
        pytest.skip("Configure SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to run this test")

    client = spotipy.Spotify(
        client_credentials_manager=SpotifyClientCredentials(
            client_id=Config.SPOTIFY_CLIENT_ID,
            client_secret=Config.SPOTIFY_CLIENT_SECRET,
        )
    )
    api = object.__new__(SpotifyAPI)
    api.sp = client
    api.logger = logging.getLogger(__name__)
    track = api.get_track(SPOTIFY_TRACK_URL)

    assert api._extract_spotify_id(SPOTIFY_TRACK_URL) == SPOTIFY_TRACK_ID
    assert track.name.strip()
    assert track.artists
    assert track.album_name.strip()
    assert track.duration > 0
    assert track.cover_url
