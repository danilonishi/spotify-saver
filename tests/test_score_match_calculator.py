from spotifysaver.models.track import Track
from spotifysaver.services.score_match_calculator import ScoreMatchCalculator


def test_localized_title_can_match_by_recording_metadata():
    track = Track(
        number=1,
        total_tracks=1,
        name="テイク・ミー",
        duration=259,
        uri="spotify:track:6HMqmktpk99290w0aD82TK",
        artists=["CASIOPEA"],
        album_artist=["CASIOPEA"],
        release_date="1984-01-01",
        album_name="SUPER FLIGHT",
    )
    youtube_result = {
        "title": "Take Me",
        "duration_seconds": 260,
        "artists": [{"name": "CASIOPEA"}],
        "album": {"name": "SUPER FLIGHT"},
    }

    score = ScoreMatchCalculator()._calculate_match_score(
        youtube_result, track, strict=True
    )

    assert score > 0