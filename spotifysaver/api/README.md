# SpotifySaver API

A REST API built with FastAPI for downloading music from Spotify via YouTube Music.

## ⚙️ Configuration

### 1. Spotify API Credentials

1. Go to the [Spotify for Developers dashboard](https://developer.spotify.com/dashboard/applications)
2. Create a new application or use an existing one
3. Copy the `Client ID` and `Client Secret`
4. Create a `.env` file in the project root:

```bash
# Copy the example file
cp .env.example .env
```

5. Edit the `.env` file with your credentials:

```env
SPOTIFY_CLIENT_ID=your_client_id_here
SPOTIFY_CLIENT_SECRET=your_client_secret_here
SPOTIFYSAVER_OUTPUT_DIR=Music  # Optional
SPOTIFYSAVER_CHANNEL_WATCHER_DB=~/.spotify-saver/channel-watcher.sqlite3  # Optional
```

### 2. Install Dependencies

```bash
# Install all dependencies
pip install -r requirements.txt

# Or install only the API dependencies
pip install fastapi uvicorn
```

## 🚀 Quick Start

### Run the Server

```bash
# Using Poetry
poetry run uvicorn spotifysaver.api.main:app --reload

# Or directly
python -m spotifysaver.api.main

# Or using the script
spotifysaver-api
```

The server will be available at: `http://localhost:8000`

## 📚 Documentation

- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

## 🔌 Endpoints

### GET `/`
Basic API information.

### GET `/health`
Service health check.

### GET `/api/v1/inspect`
Inspect a Spotify URL and return its metadata without downloading anything.

**Parameters:**
- `spotify_url` (string): URL de Spotify

**Ejemplo:**
```bash
curl "http://localhost:8000/api/v1/inspect?spotify_url=https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"
```

### POST `/api/v1/download`
Start a download task.

Accepts individual YouTube/YouTube Music video links, as well as Spotify links and YouTube collections. `output_format` defaults to `m4a` and can be `OPUS`, `mp3` or `m4a`.
YouTube video downloads write the video description to the `<plot>` field in a
Jellyfin-compatible `.nfo` file alongside each downloaded video when a
description is available, and store the source video URL in its `<url>` field.
When `generate_nfo` is enabled for Spotify audio downloads, album NFOs contain
the source download URL and each track entry contains its matched YouTube Music
URL; playlist downloads also include a `playlist.nfo` with the playlist URL
and each matched track URL.

**Request body:**
```json
{
  "spotify_url": "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
  "download_lyrics": false,
  "download_cover": true,
  "generate_nfo": false,
  "output_format": "m4a",
  "output_dir": "Music"
}
```

**Response:**
```json
{
  "task_id": "uuid-task-id",
  "status": "pending",
  "spotify_url": "https://open.spotify.com/album/...",
  "content_type": "album",
  "message": "Download task started for album"
}
```

### GET `/api/v1/download/{task_id}/status`
Get the status of a download task.

**Response:**
```json
{
  "task_id": "uuid-task-id",
  "status": "processing",
  "progress": 45,
  "current_track": "Track Name",
  "total_tracks": 12,
  "completed_tracks": 5,
  "failed_tracks": 0,
  "output_directory": "/path/to/music",
  "started_at": "2024-01-01T12:00:00"
}
```

### POST `/api/v1/download/{task_id}/cancel`
Request cancellation of an active task. The status changes to `cancelling` while the current download is stopping, then changes to `cancelled` once the worker has stopped. GET is retained for compatibility.

### POST `/api/v1/download/{task_id}/confirm-youtube`
Approve the current YouTube match for a download task that is waiting for confirmation. A task exposes one awaiting match at a time; confirming it resumes the server-owned queue.

### GET/PUT `/api/v1/preferences/youtube-confirmation`
Read or update persistent server-wide YouTube confirmation preferences. The PUT body accepts `confirm_matched_downloads` and `confirm_channel_watcher_downloads` booleans. Spotify matches, SoundCloud-to-YouTube fallbacks, and items from YouTube collections are controlled independently from Channel Watcher downloads. User-submitted single-video YouTube URLs do not require approval. Waiting tasks report `status: "awaiting_confirmation"` and include their current title and URL.

### GET `/api/v1/downloads`
List all download tasks.

### Channel watcher

Watched channels are persisted in SQLite at `~/.spotify-saver/channel-watcher.sqlite3`
by default. Set `SPOTIFYSAVER_CHANNEL_WATCHER_DB` to use another database path.
The same database stores one selected folder each for audio, video, and Channel
Watcher downloads. These paths are server-wide and shared by every browser
connecting to that server; they are independent of browser cookies and site data.
Existing browser-profile folder selections are migrated to the shared settings
when the server starts, keeping the first saved value for each media type. If a
saved folder has been deleted, the server walks up to the nearest existing
parent directory (including the filesystem root) and saves that as the new
preference.
The server starts its UTC scheduler with the API and checks due channels after
startup. Each channel's schedule is saved in the timezone reported by the browser;
the server converts local schedule times to UTC for execution. A newly added
channel is checked once immediately to establish a baseline without enumerating
its full upload history; recent upload IDs are saved as the starting cursor and
are not downloaded. Subsequent checks scan the channel's YouTube uploads
playlist newest-first, stop at the saved video-ID cursor, and re-scan a 20-video
overlap to catch uploads that become visible late. The server persists discovered
video IDs and their states (`discovered`, `queued`, `downloaded`, `skipped`, or
`failed`) before advancing the cursor. Failed or interrupted candidates are
retried on a later check. A 20-new-upload safety limit applies per check; if
there are more, the cursor stays in place and remaining uploads are handled on
later checks. This uses yt-dlp and does not require a YouTube API key.

`last_checked_at` records the last successful check for UI/status only; it is
not used to decide whether an upload is new. Existing watched channels are
migrated by treating up to the latest 20 previously untracked uploads as
recovery candidates and establishing a cursor, while older entries in the
overlap are marked as baseline. The next scheduled check is calculated from
completion time. If fetching a list fails, its last successful-check timestamp
is retained.
Updating a watched channel recalculates its next check from the new schedule
without forcing an immediate check. The server terminal logs when a check starts
and when it completes, including the number of videos found and downloads queued.
YouTube channel-list requests time out after 90 seconds and report the failure
in the watched-channel status. On server shutdown, in-flight check tasks are
cancelled so a slow request does not block the scheduler's shutdown.
Each check discovers at most 20 new uploads. If more are waiting, discovery
resumes from the persisted video-ID cursor on a later check instead of skipping
the remaining uploads.
Detailed video metadata requests are spaced to reduce YouTube rate limits. If
YouTube explicitly reports that the session is rate-limited, all channel watcher
checks pause for one hour, even when channels are scheduled more often. The
cooldown is persisted across server restarts and displayed in the Channel Watcher
section of the UI. This pause applies only to watcher checks; separate manual
downloads are unaffected.
An existing YouTube rate limit must still expire on YouTube's side. Cookies are
disabled by default; a fresh browser cookies file may help with access
restrictions, but stale or incompatible cookies can cause HTTP 403 errors.

Hourly schedules use only the selected minute and run once each hour; daily
and weekly schedules use the full selected local time. Weekly schedules include
a weekday in the selected timezone. Daily and weekly scheduled times follow
that timezone across daylight-saving transitions. Manual schedules never run
automatically. The Check now action starts an immediate one-off check; requests
are rejected while that channel is already checking or during a YouTube cooldown.
Existing minutely schedules are migrated to hourly to avoid frequent requests.

- `GET /api/v1/channel-watcher` lists watched channels and check status.
- `POST /api/v1/channel-watcher` adds a channel.
- `PUT /api/v1/channel-watcher/{id}` updates its settings.
- `POST /api/v1/channel-watcher/{id}/check` starts an immediate check.
- `DELETE /api/v1/channel-watcher/{id}` stops watching it.

## 💡 Usage Examples

### Python with requests

```python
import requests

# Inspect an album
response = requests.get(
    "http://localhost:8000/api/v1/inspect",
    params={"spotify_url": "https://open.spotify.com/album/..."}
)
metadata = response.json()

# Start a download
download_request = {
    "spotify_url": "https://open.spotify.com/album/...",
    "download_lyrics": True,
    "download_cover": True,
    "generate_nfo": True
}
response = requests.post(
    "http://localhost:8000/api/v1/download",
    json=download_request
)
task = response.json()

# Check status
status_response = requests.get(
    f"http://localhost:8000/api/v1/download/{task['task_id']}/status"
)
status = status_response.json()
```

### JavaScript/Node.js

```javascript
// Inspect URL
const inspectResponse = await fetch(
  `http://localhost:8000/api/v1/inspect?spotify_url=${encodeURIComponent(spotifyUrl)}`
);
const metadata = await inspectResponse.json();

// Start a download
const downloadResponse = await fetch('http://localhost:8000/api/v1/download', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    spotify_url: spotifyUrl,
    download_lyrics: true,
    download_cover: true,
    generate_nfo: false
  })
});
const task = await downloadResponse.json();

// Check status
const statusResponse = await fetch(
  `http://localhost:8000/api/v1/download/${task.task_id}/status`
);
const status = await statusResponse.json();
```

### cURL

```bash
# Inspect
curl "http://localhost:8000/api/v1/inspect?spotify_url=https://open.spotify.com/track/..."

# Start a download
curl -X POST "http://localhost:8000/api/v1/download" \
  -H "Content-Type: application/json" \
  -d '{
    "spotify_url": "https://open.spotify.com/album/...",
    "download_lyrics": true,
    "download_cover": true
  }'

# Check status
curl "http://localhost:8000/api/v1/download/{task_id}/status"
```

## ⚙️ Configuration

### Environment Variables

```bash
# .env file
SPOTIFY_CLIENT_ID=your_client_id
SPOTIFY_CLIENT_SECRET=your_client_secret
SPOTIFYSAVER_OUTPUT_DIR="Music"   # Default output directory
# Optional: only set when authentication is required; otherwise leave unset.
# YTDLP_COOKIES_PATH="cookies.txt"
```

### CORS Configuration

By default, the API allows connections from:
- `http://localhost:*`
- `http://127.0.0.1:*`

To change this, edit `spotifysaver/api/config.py`.

## 🔄 Download States

- **`pending`**: Task created and waiting to be processed
- **`processing`**: Download in progress
- **`completed`**: Download completed successfully
- **`failed`**: Download error
- **`cancelled`**: Task cancelled by the user

## 📁 Output Structure

```
Music/
├── Artist/
│   ├── Album (Year)/
│   │   ├── 01 - Track.mp3
│   │   ├── 01 - Track.lrc    # If lyrics are requested
│   │   ├── album.nfo         # If NFO is requested
│   │   └── cover.jpg         # If cover art is requested
│   └── ...
└── Playlist Name/
    ├── Track 01.mp3
    ├── Track 02.mp3
    └── cover.jpg
```

## 🚨 Limitations

- Downloads are processed sequentially to avoid overloading the system
- Task storage is in memory (it resets when the server restarts)
- Redis or a database is recommended for production
- YouTube Music cookies may be required for restricted content

## 🛡️ Security Considerations

- The API requires an administrator password, established with a one-time
  setup token printed to the server logs on first startup.
- Keep the setup token private; it permits the first administrator account to
  be created and becomes invalid after use.
- Spotify credentials and authentication data are stored in the server's
  security database and protected by filesystem permissions, not encrypted.
- Use HTTPS for remote web access, and protect server logs, configuration files,
  and Docker administrator access.
- Consider using a reverse proxy (such as Caddy) in production.
- Validate and sanitize all input URLs

## 📝 Logging

Logs are generated using the SpotifySaver logging system. To enable detailed logs:

```python
from spotifysaver.spotlog import LoggerConfig
LoggerConfig.setup(level="DEBUG")
```
