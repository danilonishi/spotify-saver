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

Accepts individual YouTube/YouTube Music video links, as well as Spotify links and YouTube collections. `output_format` can be `OPUS`, `mp3` or `m4a`.

**Request body:**
```json
{
  "spotify_url": "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
  "download_lyrics": false,
  "download_cover": true,
  "generate_nfo": false,
  "output_format": "mp3",
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

### GET `/api/v1/downloads`
List all download tasks.

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
YTDLP_COOKIES_PATH="cookies.txt"  # Optional
SPOTIFYSAVER_OUTPUT_DIR="Music"   # Default output directory
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

- The API does not include authentication by default
- Do not expose the API directly to the internet without authentication
- Consider using a reverse proxy (nginx) in production
- Validate and sanitize all input URLs

## 📝 Logging

Logs are generated using the SpotifySaver logging system. To enable detailed logs:

```python
from spotifysaver.spotlog import LoggerConfig
LoggerConfig.setup(level="DEBUG")
```
