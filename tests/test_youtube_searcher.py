import logging

from spotifysaver.models.track import Track
from spotifysaver.services.score_match_calculator import ScoreMatchCalculator
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
    searcher.logger = logging.getLogger(__name__)
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


def test_title_search_is_available_when_artist_metadata_differs():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher._process_results = (
        lambda results, track, strict, allow_title_only=False: None
    )

    searcher._search_title_match(_make_track())

    query, filter_name, limit = searcher.ytmusic.search_calls[-1]
    assert query == "the march of the varangian guard stand up and fight incl. bonustrack"
    assert filter_name == "songs"
    assert limit == 10


def test_title_search_allows_exact_title_without_artist_metadata():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher.logger = logging.getLogger(__name__)
    searcher._process_results = lambda results, track, strict, allow_title_only=False: (
        "https://music.youtube.com/watch?v=track-video"
        if allow_title_only
        else None
    )

    result = searcher._search_title_match(_make_track())

    assert result == "https://music.youtube.com/watch?v=track-video"


def test_hyphenated_title_keeps_search_word_boundary():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher.ytmusic.search = lambda query, filter, limit, **kwargs: (
        searcher.ytmusic.search_calls.append((query, filter, limit))
        or [
            {
                "title": "Gutter-churl",
                "videoId": "V6bjz63ec3A",
                "duration_seconds": 182,
                "artists": [{"name": "\u5d0e\u5143\u4ec1"}],
                "album": {"name": "FINAL FANTASY XII Original Soundtrack"},
            }
        ]
    )
    searcher.scorer = ScoreMatchCalculator()
    searcher.logger = logging.getLogger(__name__)
    track = _make_track()
    track.name = "Gutter-churl"
    track.artists = ["\u5d0e\u5143\u4ec1"]
    track.album_name = "FINAL FANTASY XII Original Soundtrack"
    track.duration = 182

    result = searcher._search_exact_match(track)

    query, filter_name, limit = searcher.ytmusic.search_calls[-1]
    assert query == "\u5d0e\u5143\u4ec1 gutter churl final fantasy xii original soundtrack"
    assert filter_name == "songs"
    assert limit == 5
    assert result == "https://music.youtube.com/watch?v=V6bjz63ec3A"


def test_fuzzy_search_does_not_include_album_title():
    searcher = object.__new__(YoutubeMusicSearcher)
    searcher.ytmusic = FakeYoutubeMusic()
    searcher._process_results = lambda results, track, strict: None

    searcher._search_fuzzy_match(_make_track())

    query, filter_name, limit = searcher.ytmusic.search_calls[-1]
    assert query == "turisas the march of the varangian guard"
    assert filter_name == "songs"
    assert limit == 10
