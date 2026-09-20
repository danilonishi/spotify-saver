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
