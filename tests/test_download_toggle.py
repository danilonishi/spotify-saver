from spotifysaver.api.schemas import DownloadRequest


def test_download_request_defaults_to_disabled():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
    )

    assert request.overwrite_existing is False


def test_download_request_allows_enabling_downloads():
    request = DownloadRequest(
        spotify_url="https://open.spotify.com/track/2t9DE7p2wx4McTXQm0y2Fe?si=278359903d454ee1",
        output_format="mp3",
        overwrite_existing=True,
    )

    assert request.overwrite_existing is True
