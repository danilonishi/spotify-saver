# SpotifySaver 🎵✨

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![PyPI Version](https://img.shields.io/pypi/v/spotifysaver?color=blue&logo=pypi&logoColor=white)](https://pypi.org/project/spotifysaver/)
[![Docker](https://img.shields.io/badge/Docker-GHCR-blue?logo=docker&logoColor=white)](https://github.com/danilonishi/spotify-saver/pkgs/container/spotify-saver)
[![GitHub Container Registry](https://img.shields.io/badge/ghcr.io-Auto--Build-green?logo=github)](https://github.com/danilonishi/spotify-saver/pkgs/container/spotify-saver)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-Required-orange?logo=ffmpeg&logoColor=white)](https://ffmpeg.org/)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-2026.8.19%2B-red)](https://github.com/yt-dlp/yt-dlp)
[![YouTube Music](https://img.shields.io/badge/YouTube_Music-API-yellow)](https://ytmusicapi.readthedocs.io/)
[![Spotify](https://img.shields.io/badge/Spotify-API-1ED760?logo=spotify)](https://developer.spotify.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/gabrielbaute/spotify-saver)

> ⚠️This repository is a fork from gabrielbaute's SpotifySaver. It's being developed with quality of life features I missed when using his tool. You can easily see the changes made by clicking [here](https://github.com/gabrielbaute/spotify-saver/compare/main...danilonishi:spotify-saver:main).

All-in-one tool for downloading and organizing music with Spotify metadata for Jellyfin.

The app connects to the Spotify and YouTube Music APIs, retrieves information from Spotify, locate the media on Youtube, downloads and extract the audio data, generates an .nfo XML file to complete the metadata required by Jellyfin when building music libraries.

## Changes in This Fork

<p align="center">
    <img src="screenshot.jpg" alt="SpotifySaver web interface" />
</p>

Compared with the upstream baseline, this branch adds:

- **A server-managed download queue:** Server controlled downloading means you can queue downloads, pause or resume them, clear queued or completed items, and retry failed or partially failed downloads. Downloads continue if the browser is refreshed or closed; queue state is held in memory and does not survive an API server restart. The server currently processes one download at a time.
- **Queue and track visibility:** Download names and track listings are reported by the server and shown to other connected browsers, regardless if they're the one that submitted the download.
- **Simple download folders in the web UI:** Choose from folders under a configurable media root, shown as a path with the selected folder in bold. Use `[Parent]` to go up a level and double-click a folder to make it the new root. Audio, video, and Channel Watcher each keep their own selected folder in the server database. The selected paths are shared by all browsers connecting to that server and persist across browser data clearing and server restarts. If a selected folder is deleted, its preference falls back to the nearest existing parent directory, including the filesystem root.
- **Audio and Video tabs:** The Video tab downloads YouTube videos or playlists as MP4/MKV/WebM with a maximum resolution and optional subtitles, into a separate video folder (default `Videos`, override with `SPOTIFYSAVER_VIDEO_DIR`). Video descriptions are saved in a Jellyfin-compatible NFO `<plot>` alongside each video. The download button follows the active tab. Video downloads require ffmpeg for merging streams.
- **Channel Watcher:** Watch YouTube channels for new uploads manually or on an hourly, daily, or weekly schedule. Pause or resume automatic checks for each channel, or use **Check now** to trigger an immediate check even while paused. It tracks uploads by video ID, retries unqueued discoveries, and resumes safely when a check reaches its per-run limit. Each watched channel has its own download folder, video format, resolution, subtitle settings, and optional content filters for title keywords, duration, upload dates, Shorts, and livestreams; checks continue on the server and are visible in the web UI.
- **Jellyfin library scans:** Open **Settings** in the web UI to configure a Jellyfin server URL and API key. SpotifySaver requests a library refresh after the download queue becomes idle, coalesces later completed downloads until the configured minimum interval has elapsed, and persists pending scans and scan settings across server restarts. It authenticates with Jellyfin's `Authorization: MediaBrowser Token="..."` header. Use **Scan library now** to request a manual scan that bypasses the interval cooldown but still waits for active downloads to finish.
- **Web interface improvements:** Dark mode, browser-saved visibility and draggable two-column layout controls for the Download Status, Download, Download Queue, Channel Watcher, and Activity Log panels, collapsible album details and activity log, a clear-URL button for quick mobile pasting, and controls for clearing completed downloads.
- **Direct youtube downloads.** You can now paste any youtube video or playlist link, and it will download and extract the audio content.
- **M4A at 256 kbps as the default for API and web downloads.**
- **Download over 50 tracks per album or playlist.** Downloading large playlists were capped at 50 tracks due to Spotify list being paginated. This is fixed here.
- **Option to skip files.** Before, files were always redownloaded. Now a check is done before downloading.
- **Album-numbered playlist tracks.** Tracks were saved with playlist entry number as track number, now they use the album number. This helps avoid duplicate files.
- **Improved Spotify-youtube accuracy.** Often it matched wrong content from youtube. This will never be 100% but it's much better.


## 🌟 Features
- ✅ Download audio from YouTube Music with Spotify metadata
- ✅ Download YouTube videos, playlists, and album collections with YouTube metadata through the API and web UI
- ✅ Schedule automatic checks for watched YouTube channels from the web interface
- ✅ Download YouTube playlists and album collections from the CLI
- ✅ Synchronized lyrics (.lrc) from LRC Lib
- ✅ Generate Jellyfin-compatible `.nfo` metadata files
- ✅ Automatic folder structure (Artist/Album)
- ✅ Command-line interface (CLI)
- ✅ Web interface (UI) with real-time progress
- ✅ Stop an active download from the web interface or API
- ✅ RESTful API for integrations
- ✅ Docker support with auto-builds
- ✅ Playlist support
- ✅ M4A and MP3 audio output
- ✅ Support for 96, 128, 192, and 256 kbps bitrates

### Requirements
- Python 3.8+
- FFmpeg
- [Spotify Developer Account](https://developer.spotify.com/dashboard/)

```bash
# Installation with Poetry (recommended)
git clone https://github.com/danilonishi/spotify-saver.git
cd spotify-saver
poetry install

# Or with pip
pip install git+https://github.com/danilonishi/spotify-saver.git
```

Spotify URLs require a Spotify developer app and a Spotify account connection.
Configure these in **Settings → Spotify Account** in the web interface. Direct
YouTube downloads do not require Spotify credentials.

## ⚙️ Configuration

Once in your project directory, run:

```bash
spotifysaver init
```
This will create a local `.env` file with the environment variables that will be requested:

| Variable                  | Description                                | Default Value                     |
|---------------------------|--------------------------------------------|-----------------------------------|
| `SPOTIFY_CLIENT_ID`       | ID of the Spotify app you created          | -                                 |
| `SPOTIFY_CLIENT_SECRET`   | Secret key generated for your Spotify app  | -                                 |
| `SPOTIFY_REDIRECT_URI`    | Spotify API Validation URI                 | `http://127.0.0.1:8888/callback`  |
| `SPOTIFYSAVER_OUTPUT_DIR` | Custom directory path (optional)           | `./Music`                         |
| `YTDLP_COOKIES_PATH`      | Cookie file path (optional)                | -                                 |
| `API_PORT`                | API server port (optional)                 | `8000`                            |
| `API_HOST`                | Host for the API (optional)                | `0.0.0.0`                         |
| `UI_ENABLED`              | Enable/disable web interface (optional)    | `true`                            |

The variable `YTDLP_COOKIES_PATH` will indicate the location of the file with the Youtube Music cookies, in case we have problems with restrictions to yt-dlp, specifically it is for cases in which youtube blocks the app for "behaving like a bot" (~~which is not entirely false lol~~)

You can also check the .example.env file

## 📚 Documentation

The original maintainer of the repository owns a [documentation with Deepwiki](https://deepwiki.com/gabrielbaute/spotify-saver). You can consult it at all times.

⚠️ The original documentation on Deepwiki does not reflect changes in this repository.

The **documentation for using the API**, on the other hand, can be found in this same repository here: [API Documentation](API_IMPLEMENTATION_SUMMARY.md)

## 💻 Using the CLI

### Available Commands

| Command              | Description                                | Example                                    |
|----------------------|--------------------------------------------|--------------------------------------------|
| `init`               | Configure environment variables            | `spotifysaver init"`                       |
| `download [URL]`     | Download Spotify tracks/albums/playlists or YouTube albums/playlists | `spotifysaver download "URL"`      |
| `inspect`            | Shows Spotify metadata (album, playlist)   | `spotifysaver inspect "URL_SPOTIFY"`       |
| `show-log`           | Shows the application log                  | `spotifysaver show-log`                    |
| `version`            | Shows the installed version                | `spotifysaver version`                     |

### Download Options

| Option            | Description                                           | Accepted Values         ​​|
|-------------------|-------------------------------------------------------|-------------------------|
| `--lyrics`        | Download synchronized lyrics (.lrc)                   | Flag (no value)         |
| `--output DIR`    | Output directory                                      | Valid path              |
| `--format FORMAT` | Audio format                                          | `m4a` (default), `mp3`, `opus` |
| `--bitrate KBPS`  | Audio bitrate                                         | `96`, `128` , `192`, `256`(default) |
| `--cover/--no-cover` | Save Spotify cover or embed YouTube thumbnail | Flag (no value)      |
| `--nfo`           | Generates a .nfo metadata file in the JellyFin format | Flag (no value)         |
| `--explain`       | Show score breakdown for each track without downloading (for error analysis) | Flag (no value)         |
| `--dry-run`       | Simulate download without saving files                | Flag (no value)         |

### show-log Options

| Option      | Description                             | Accepted Values ​​              |
|-------------|-----------------------------------------|-------------------------------|
| `--lines`   | Number of log lines to display          | `--lines 25` --> `int`        |
| `--level`   | Filter by log level                     | INFO, WARNING, DEBUG, ERROR   |
| `--path`    | Displays the location of the log file   | Flag (no value)               |

## 💡 Usage Examples
```bash
# Set spotifysaver configuration
spotifysaver init

# Download album with synchronized lyrics
spotifysaver download "https://open.spotify.com/album/..." --lyrics

# Download album with metadata file; the cover is saved as cover.jpg by default
spotifysaver download "https://open.spotify.com/album/..." --nfo

# Download song in MP3 format
spotifysaver download "https://open.spotify.com/track/..." --format mp3

# Download a YouTube Music album or playlist with YouTube-provided tags
spotifysaver download "https://music.youtube.com/playlist?list=..."
```

The CLI accepts Spotify tracks, albums, and playlists, and YouTube playlists or album collections. Direct YouTube video URLs are supported by the web interface and API, but not by the CLI. Spotify playlist tracks are saved under their canonical artist/album folders, with playlist files kept separately in a `Playlists/` directory beside the configured output directory. Existing files are skipped by default; set the web interface's overwrite option or the API's `overwrite_existing` field to replace them.

## Usage with API

To use the API, you need to have the API server running. You can start it with the following command:

```bash
# Start the API server
spotifysaver-api
```

The server will run at `http://127.0.0.1:8000` by default. The API accepts Spotify track, album, and playlist URLs, as well as direct YouTube video and collection URLs. Active downloads can be cancelled with `POST /api/v1/download/{task_id}/cancel`. You can find the [API documentation here](API_IMPLEMENTATION_SUMMARY.md), which describes the technical aspects and usage in detail.

## 🖥️ Web Interface (UI)

SpotifySaver now includes a modern web interface that makes it easy to download music without using the command line:

```bash
# Start the API server with web interface
spotifysaver-api
```

This will start the API server with an integrated web interface that you can access at `http://127.0.0.1:8000`. The web interface provides:

- **Easy URL input**: Paste a Spotify track, album, or playlist link, a YouTube video, or a YouTube album/playlist link
- **Full configuration**: All download options available through an intuitive interface
- **Real-time progress**: Monitor download progress and see detailed logs
- **Download cancellation**: Stop an active download
- **Overwrite control**: Keep existing files by default or replace them on request
- **Channel Watcher**: Add YouTube channels, configure each channel's schedule and video download settings, and view last-check or rate-limit cooldown status
- **Jellyfin library scans**: Configure the Jellyfin server URL, API key, and minimum interval between refresh requests; manually request a scan to bypass the cooldown. The API key is stored server-side and is not returned to the UI.
- **Configuration backup and restore**: Export watched channels, download-folder preferences, dark mode, notification preferences, dashboard layout and panel states, Jellyfin URL/scan interval, and Spotify Client ID/redirect URL as JSON. Restore replaces those settings while retaining destination passwords, API keys, Spotify secrets, and authorization tokens. Browser notification permission remains browser-controlled and must be granted on the destination. Backups do not include active downloads or activity logs; folder paths may need adapting after migration.
- **Responsive design**: Works on desktop and mobile devices
- **Automatic browser opening**: Opens your default browser automatically

### Web Interface Features:
- ✅ URL validation for Spotify and supported YouTube links
- ✅ Configurable audio format (M4A by default, or MP3) and bitrate (256 kbps by default)
- ✅ Toggle lyrics and NFO file generation
- ✅ Stop active downloads and choose whether to overwrite existing files
- ✅ Custom output directory
- ✅ Real-time download progress
- ✅ Scheduled YouTube channel checks with per-channel download settings
- ✅ Channel check status and YouTube rate-limit cooldown visibility
- ✅ Activity log with timestamps
- ✅ Error handling and user feedback

**Default Access:**
- Web Interface & API: `http://127.0.0.1:8000`
- API Documentation: `http://127.0.0.1:8000/docs`

The web interface and API require an administrator password. On first startup,
the server logs a one-time setup token; enter it in the browser to create the
password. Store and protect the server's configuration directory because it
contains the password hash, Spotify credentials, and authorization tokens.
Use HTTPS for remote access.

In **Settings → Backup & Restore**, download a JSON configuration backup before
migrating or reinstalling. The file includes watched channel URLs and server
paths, so keep it private. Restoring replaces the included settings on the
current instance. Passwords, Jellyfin API keys, Spotify client secrets, and
Spotify authorization tokens, active downloads, per-video watcher history, and
activity logs are not transferred; re-enter secrets and reconnect Spotify on
the destination if needed. Review media folder paths after restoring because
host and container paths can differ.

## 🐳 Docker Support

Docker images are published to
[GitHub Container Registry](https://github.com/danilonishi/spotify-saver/pkgs/container/spotify-saver)
from pushes to `main` and version tags (`v*`). `latest` tracks `main`; version
tags are also available for pinned deployments. The first successful workflow
run creates the package. Set its visibility to **Public** in GitHub Packages
if servers should be able to pull images without authenticating. Until the
first publish workflow has succeeded, use the local-build override below.

### Install with Docker Compose

Requirements: Docker Engine/Desktop with the Compose plugin. Clone this
repository, then create the host folders and environment file:

In **Windows PowerShell**, run:

```powershell
New-Item -ItemType Directory -Force data\music, data\videos, data\config
Copy-Item .env.docker.example .env
```

On **Linux/macOS**, run the equivalent commands in a terminal using Bash:

```sh
mkdir -p data/music data/videos data/config
cp .env.docker.example .env
```

The container runs as user ID `1000` and needs write access to its config
folder. If you created the repository or its data folders with `sudo`, fix
ownership before starting the container:

```sh
sudo chown -R 1000:1000 data/config
```

If you set `CONFIG_DIR` in `.env` to a different host folder, use that folder
instead of `data/config`.

Before starting SpotifySaver, edit `.env` and set `MUSIC_DIR` and `VIDEO_DIR`
to the existing media folders on your server. For example, if your media
folders are `/media/Music` and `/media/Videos`, set:

```dotenv
MUSIC_DIR=/media/Music
VIDEO_DIR=/media/Videos
```

These are host paths, not the `/music` and `/videos` paths used inside the
container. Leave `CONFIG_DIR=./data/config` unless you want to store the app's
configuration elsewhere. On Linux, you can edit the file with `nano .env` from
the repository folder.

Direct YouTube downloads and the Channel Watcher do not need Spotify
credentials. Spotify URL-based downloads can be configured from **Settings →
Spotify Account** after signing in. The app asks Spotify to authorize in your
browser and receives the callback on the server; this works with a headless
Docker deployment.

Start the published image:

```sh
docker compose pull
docker compose up -d
```

### First-time sign-in

On first launch, SpotifySaver writes a one-time administrator setup token to
the server logs. From the directory containing the Compose files, run:

```sh
docker compose logs --tail=100 spotifysaver
```

Find the line beginning `First-run administrator setup token` and enter its
token in the setup page. If the container is restarting, use this same command
to inspect the startup error in the logs.

Open SpotifySaver in your browser, enter that token, and create an administrator
password of at least 12 characters. The token is generated again after a server
restart until the initial password has been set, and becomes invalid once it is
used. Do not share the logs while they contain the token. After setup, sign in
with the password you created. Login attempts are rate-limited.

The admin password, sessions, and Spotify app credentials are persisted in
`CONFIG_DIR` (the `data/config` folder by default); the database is readable
only by the container account on Linux. The Spotify OAuth token cache is stored
in the same config directory with restricted file permissions. Back up this
directory and protect access to it.

### Configure Spotify

1. Create an app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard/).
2. In SpotifySaver, open **Settings → Spotify Account**. Copy the displayed
   OAuth redirect URL into the app's allowed Redirect URIs in the Spotify
   Developer Dashboard. It must match exactly; for a Caddy deployment it uses
   the public HTTPS hostname.
3. Enter the app's Client ID and Client Secret and save them.
4. Select **Connect to Spotify** and approve access in Spotify.

The Client Secret is stored on the server and is never returned to the browser
after saving. The server-side security database is protected by filesystem
permissions, not encrypted; anyone with access to the server's config files or
Docker administrator access can read it. Keep those host files private.

To build and run the checked-out source instead of pulling GHCR, add the build
override:

```sh
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

To update a server to the latest published `main` image, run `git pull` for
the Compose files, then:

```sh
docker compose pull
docker compose up -d
```

To publish a versioned image, push a version tag such as `v0.7.5`; the workflow
publishes it as `0.7.5` as well as updating the major/minor tags. Set
`IMAGE_TAG=0.7.5` in `.env` to pin the deployment to that image instead of
following `latest`.

Open `http://127.0.0.1:8000`. The API is also available at
`http://127.0.0.1:8000/docs`. The port is bound to loopback only; for access
from other devices on your home network, use the existing Caddy reverse proxy
setup below rather than exposing the application port directly.

The UI's folder selectors operate on container paths. Audio downloads are
stored under `/music`, video and watched-channel downloads under `/videos`,
and both paths map to `data/music` and `data/videos` on the host by default.
You can set `MUSIC_DIR`, `VIDEO_DIR`, and `CONFIG_DIR` in `.env` to change the
host-side folders. Keep the config directory: it stores the watched-channel
database, channel download archives, cookies, and other home-directory app
state. Back up these folders before migrating or reinstalling:

```text
data/music/
data/videos/
data/config/
```

On Linux, make sure the mounted directories are writable by the container's
non-root user (UID 1000). For example, if these are dedicated directories:

```sh
sudo chown -R 1000:1000 data/music data/videos data/config
```

Useful maintenance commands:

```sh
docker compose logs -f spotifysaver
git pull
docker compose pull
docker compose up -d
docker compose down
```

`docker compose down` stops and removes the container but leaves the bind
mounted data folders and named log volume intact. Do not use
`docker compose down -v` unless you intend to remove the named log volume as
well.

## 📂 Output Structure
```
.
├── Music/
│   ├── Artist/
│   │   └── Album (Year)/
│   │       ├── 01 - Song.mp3
│   │       ├── 01 - Song.lrc
│   │       ├── album.nfo
│   │       └── cover.jpg
│   └── YouTube/
│       └── Collection Name/
│           └── 01 - Video Title.mp3
└── Playlists/
    └── Playlist Name/
        └── Playlist Name.m3u
```
Direct YouTube videos are saved under an artist folder when artist metadata is available.

## 🤝 Contributions
1. Fork the project
2. Create your branch (`git checkout -b feature/new-feature`)
3. Commit your changes (`git commit -m 'Add awesome feature'`)
4. Push to the branch (`git push origin feature/new-feature`)
5. Open a Pull Request

## 📄 License

MIT © [TGabriel Baute](https://github.com/gabrielbaute)
