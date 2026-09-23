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


def test_match_tolerates_missing_duration_and_metadata_formatting():
    track = Track(
        number=1,
        total_tracks=1,
        name="Canci\u00f3n-Test",
        duration=180,
        uri="spotify:track:formatting",
        artists=["Beyonce"],
        album_artist=["Beyonce"],
        release_date="2020-01-01",
        album_name="\u00c1lbum [Deluxe]",
    )
    youtube_result = {
        "title": "Cancion Test",
        "artists": [{"name": "Beyonc\u00e9"}],
        "album": {"name": "Album Deluxe"},
    }

    score = ScoreMatchCalculator()._calculate_match_score(
        youtube_result, track, strict=True
    )

    assert score > 0


def test_fuzzy_match_accepts_short_soundtrack_upload_without_duration():
    track = Track(
        number=1,
        total_tracks=35,
        name="CAPCOM LOGO",
        duration=6,
        uri="spotify:track:soundtrack-cue",
        artists=["Capcom Sound Team"],
        album_artist=["Capcom Sound Team"],
        release_date="2013-01-01",
        album_name="MEGA MAN X SOUND COLLECTION",
    )
    youtube_result = {
        "title": "Mega Man X - Capcom Logo",
        "artists": [{"name": "Capcom Sound Team"}],
    }

    score = ScoreMatchCalculator()._calculate_match_score(
        youtube_result, track, strict=False
    )

    assert score > 0