import logging

from spotifysaver.services.spotify_api import SpotifyAPI
import spotifysaver.services.spotify_api as spotify_api_module


class FakeSpotify:
    def __init__(self):
        self.calls = []

    def _get(self, endpoint):
        self.calls.append(("_get", endpoint))
        return {"items": [{"item": {"name": "Track A"}}], "next": None}

    def playlist_tracks(self, playlist_id):
        self.calls.append(("playlist_tracks", playlist_id))
        return {"items": [{"track": {"name": "Track B"}}], "next": None}

    def next(self, page):
        self.calls.append(("next", page))
        return {"items": [{"name": "Track C"}], "next": None}


def test_get_playlist_tracks_uses_playlist_items_when_available():
    api = object.__new__(SpotifyAPI)
    api.sp = FakeSpotify()

    tracks = api._get_playlist_tracks("playlist_123")

    assert tracks == [{"item": {"name": "Track A"}}]
    assert api.sp.calls == [("_get", "playlists/playlist_123/items")]


def test_fetch_album_data_includes_all_paginated_tracks():
    api = object.__new__(SpotifyAPI)
    api.sp = FakeSpotify()
    api.logger = logging.getLogger(__name__)
    api.sp.album = lambda album_id: {
        "tracks": {"items": [{"name": "Track A"}], "next": "next-page"}
    }

    album = api._fetch_album_data("https://open.spotify.com/album/album_123")

    assert [track["name"] for track in album["tracks"]["items"]] == [
        "Track A",
        "Track C",
    ]
    assert api.sp.calls[0][0] == "next"


def test_get_playlist_uses_album_track_number_and_preserves_playlist_position():
    api = object.__new__(SpotifyAPI)
    api._fetch_playlist_data = lambda _: {
        "name": "My Playlist",
        "description": "",
        "owner": {"display_name": "me"},
        "uri": "spotify:playlist:1",
        "images": [],
        "items": {"total": 1},
        "tracks": [],
        "next": None,
    }
    api._playlist_items = lambda _: [
        {
            "track": {
                "track_number": 6,
                "name": "Track A",
                "duration_ms": 180000,
                "uri": "spotify:track:1",
                "artists": [{"name": "Artist One"}],
                "album": {
                    "name": "Album One",
                    "artists": [{"name": "Artist One"}],
                    "release_date": "2024-01-01",
                    "images": [],
                },
            }
        }
    ]

    playlist = api.get_playlist("spotify:playlist:1")

    assert playlist.tracks[0].number == 6
    assert playlist.tracks[0].playlist_position == 1


def test_get_album_preserves_unique_collection_positions_for_multiple_discs():
    api = object.__new__(SpotifyAPI)
    api._fetch_album_data = lambda _: {
        "name": "Multi-disc album",
        "artists": [{"name": "Artist"}],
        "release_date": "2024-01-01",
        "genres": [],
        "images": [],
        "total_tracks": 2,
        "uri": "spotify:album:multi-disc",
        "tracks": {
            "items": [
                {
                    "track_number": 1,
                    "disc_number": disc_number,
                    "name": f"Disc {disc_number} track",
                    "duration_ms": 180000,
                    "uri": f"spotify:track:{disc_number}",
                    "artists": [{"name": "Artist"}],
                }
                for disc_number in (1, 2)
            ]
        },
    }

    album = api.get_album("spotify:album:multi-disc")

    assert [track.number for track in album.tracks] == [1, 1]
    assert [track.playlist_position for track in album.tracks] == [1, 2]


def test_resolve_youtube_track_title_uses_spotify_album_and_duration(
    monkeypatch,
):
    monkeypatch.setattr(spotify_api_module.Config, "SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setattr(
        spotify_api_module.Config, "SPOTIFY_CLIENT_SECRET", "client-secret"
    )

    class FakeSpotify:
        def __init__(self, client_credentials_manager):
            self.client_credentials_manager = client_credentials_manager

        def search(self, q, type, limit):
            assert type == "track"
            assert limit == 10
            return {
                "tracks": {
                    "items": [
                        {
                            "id": "spotify-track",
                            "name": "The Archadian Empire",
                            "duration_ms": 469000,
                            "album": {"name": "FINAL FANTASY XII Original Soundtrack"},
                        }
                    ]
                }
            }

    monkeypatch.setattr(
        spotify_api_module,
        "SpotifyClientCredentials",
        lambda **kwargs: kwargs,
    )
    monkeypatch.setattr(spotify_api_module.spotipy, "Spotify", FakeSpotify)

    title = SpotifyAPI.resolve_youtube_track_title(
        {
            "title": "帝国のテーマ",
            "track": "帝国のテーマ",
            "artist": "Hitoshi Sakimoto, Hitoshi Sakimoto",
            "album": "FINAL FANTASY XII Original Soundtrack",
            "duration": 469,
        }
    )

    assert title == "The Archadian Empire"
