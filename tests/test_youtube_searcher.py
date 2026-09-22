from spotifysaver.models.track import Track
from spotifysaver.services.youtube_api import YoutubeMusicSearcher


class FakeYoutubeMusic:
    def __init__(self):
        self.search_calls = []

    def search(self, query, filter, limit, **kwargs):
        self.search_calls.append((query, filter, limit))
        if filter == "songs":
            return [{"videoId": "track-video"}]
        return []


def _make_track():
    return Track(
        number=1,
        total_tracks=12,
        name="The March Of The Varangian Guard",
        duration=257,
        uri="spotify:track:1",
        artists=["Turisas"],
        album_artist=["Turisas"],
        release_date="2011-01-01",
        album_name="Stand Up And Fight (Incl. Bonustrack)",
    )


def test_track_search_does_not_check_youtube_album_metadata():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher._process_results = lambda results, track, strict: (
        "https://music.youtube.com/watch?v=track-video"
        if results and not strict
        else None
    )

    result = searcher._search_with_fallback(_make_track())

    assert result == "https://music.youtube.com/watch?v=track-video"
    assert all(filter_name == "songs" for _, filter_name, _ in searcher.ytmusic.search_calls)
    assert searcher.ytmusic.search_calls[-1] == (
        "turisas the march of the varangian guard",
        "songs",
        10,
    )


def test_fuzzy_search_does_not_include_album_title():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher._process_results = lambda results, track, strict: None

    searcher._search_fuzzy_match(_make_track())

    query, filter_name, limit = searcher.ytmusic.search_calls[-1]
    assert query == "turisas the march of the varangian guard"
    assert filter_name == "songs"
    assert limit == 10
