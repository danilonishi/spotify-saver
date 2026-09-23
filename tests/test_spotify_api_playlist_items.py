from spotifysaver.services.spotify_api import SpotifyAPI


class FakeSpotify:
    def __init__(self):
        self.calls = []

    def _get(self, endpoint):
        self.calls.append(("_get", endpoint))
        return {"items": [{"item": {"name": "Track A"}}], "next": None}

    def playlist_tracks(self, playlist_id):
        self.calls.append(("playlist_tracks", playlist_id))
        return {"items": [{"track": {"name": "Track B"}}], "next": None}


def test_get_playlist_tracks_uses_playlist_items_when_available():
    api = object.__new__(SpotifyAPI)
    api.sp = FakeSpotify()

    tracks = api._get_playlist_tracks("playlist_123")

    assert tracks == [{"item": {"name": "Track A"}}]
    assert api.sp.calls == [("_get", "playlists/playlist_123/items")]


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
