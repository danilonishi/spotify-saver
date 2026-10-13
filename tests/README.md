# Automated tests

Tests under `mock/` are the deterministic, offline suite. They use fakes and
monkeypatching for platform APIs, the filesystem, and download tools where
appropriate. Run them quickly with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\mock -o addopts= -q
```

Tests under `real/` make requests to external services and are marked `real`.
The Spotify test uses the configured client-credentials pair and fetches the
supplied public track. It does not open a browser or use OAuth. The YouTube tests
use the supplied public video to independently resolve the production audio
and MP4 video format selectors. They extract metadata only and do not download
media. Spotify is skipped if credentials are not configured; YouTube tests
require network access and a currently available video. Run those checks
explicitly:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\real -o addopts= -m real -q
```

Tests under `real_download/` actually download your channel-owned YouTube video
to a unique run directory, then inspect the audio/video files with `ffprobe`,
check video NFO and channel-cover output, and verify `.lrc` sidecar writing
using synthetic test lyrics. They are intentionally excluded from the default
suite. FFmpeg/ffprobe and network access are required. By default pytest keeps
these files under its temporary directory; set
`SPOTIFYSAVER_REAL_DOWNLOAD_DIR` to a folder if you want to inspect them after
the run. The test creates a new `run-...` subdirectory and does not overwrite
or delete existing files. MP3 and Opus bitrate assertions inspect the encoded
output; M4A may preserve the downloaded AAC stream, so its measured bitrate can
be lower than the selected target:

```powershell
$env:SPOTIFYSAVER_REAL_DOWNLOAD_DIR = "C:\temp\spotify-saver-real"
.\.venv\Scripts\python.exe -m pytest tests\real_download -o addopts= -m real_download -q
```

The default pytest discovery is limited to the deterministic mock suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\mock -o addopts= -q
```

`-o addopts=` disables the project-wide coverage flags for environments that
do not have the optional `pytest-cov` development dependency installed. The
legacy scripts under `../testing/` are exploratory/manual tools, not pytest
tests. The root-level `../test_api.py` is also a manual client for a running
SpotifySaver server and is not collected by pytest.
