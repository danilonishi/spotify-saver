# SpotifySaver API - Implementation Summary

The API is implemented with FastAPI in `spotifysaver/api/`. It serves the web UI,
exposes OpenAPI documentation, and runs downloads through a server-owned in-memory
queue.

## Application Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/` | Serves the web UI, when initialized and running |
| GET | `/health` | Returns the service health status |
| GET | `/version` | Returns the running API version |
| GET | `/api/v1/` | Returns API information and documentation links |
| GET | `/docs` | Swagger UI |
| GET | `/redoc` | ReDoc |

The download router is mounted below `/api/v1`.

## Download Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/download` | Validate and enqueue a Spotify or YouTube download |
| GET | `/api/v1/download/{task_id}/status` | Return one task's current status |
| GET, POST | `/api/v1/download/{task_id}/cancel` | Cancel a queued or active task; GET is retained for compatibility |
| DELETE | `/api/v1/download/{task_id}` | Remove a completed, failed, or cancelled task from history |
| GET | `/api/v1/downloads` | List completed, pending, and processing tasks |
| GET | `/api/v1/inspect` | Fetch Spotify track, album, or playlist metadata without downloading |

### Queue Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/queue` | Return all known tasks in queue order and the paused state |
| POST | `/api/v1/queue/pause` | Pause starting queued tasks and interrupt the active task for later resumption |
| POST | `/api/v1/queue/resume` | Resume the queue and start pending tasks automatically |
| DELETE | `/api/v1/queue` | Remove queued tasks while leaving the active task running |
| DELETE | `/api/v1/queue/completed` | Remove completed, failed, and cancelled task history |

The queue currently runs one download at a time (`MAX_CONCURRENT_DOWNLOADS = 1`).
Tasks continue on the server when the submitting browser is refreshed or closed.

## Request Model

`POST /api/v1/download` accepts `DownloadRequest`:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `spotify_url` | URL | required | Spotify track/album/playlist, YouTube video, or YouTube collection |
| `download_lyrics` | boolean | `false` | Download synchronized lyrics |
| `download_cover` | boolean | `true` | Download cover art or thumbnail |
| `generate_nfo` | boolean | `false` | Generate Jellyfin NFO metadata |
| `overwrite_existing` | boolean | `false` | Replace existing files |
| `download_files` | boolean or null | `null` | Deprecated compatibility alias for overwrite behavior |
| `output_format` | string | `mp3` | `mp3` or `m4a` |
| `bit_rate` | integer | `256` | Between 64 and 256 kbps |
| `output_dir` | string or null | `Music` | Optional output directory |

The response contains `task_id`, `status`, `spotify_url`, `content_type`, and a
message. New tasks start with status `queued`.

## Task Status

`DownloadStatus` includes:

- Lifecycle: `queued`, `processing`, `cancelling`, `cancelled`, `completed`, or `failed`
- Progress: `progress`, `current_track`, `current_track_number`, and `current_track_status`
- Counts: `total_tracks`, `completed_tracks`, `failed_tracks`, and `failed_track_names`
- Per-track events: `track_updates`
- Metadata: `title` and the full `tracks` list once source metadata is resolved
- Source/configuration: `spotify_url`, `content_type`, `output_dir`, `output_directory`, `output_format`, and `bit_rate`
- Queue state: `queue_position` (`1`-based for pending tasks, `0` for the active task)
- Timing/errors: `started_at`, `completed_at`, and `error_message`

Task and queue state is stored in process memory. It is lost when the API server
restarts and is not shared between multiple API worker processes.

## Service and Integration

- `app.py` creates the FastAPI application, configures CORS, mounts static UI assets,
  and registers the download router.
- `routers/download.py` validates URLs, owns task state, manages queue lifecycle,
  and exposes the API endpoints above.
- `services/download_service.py` adapts the existing Spotify and YouTube downloaders
  to async API calls using an executor for blocking work.
- Metadata callbacks populate the task title and complete track listing as soon as
  Spotify metadata is available. YouTube collection titles may be filled when the
  download result is returned.
- Spotify inspection and Spotify downloads require configured Spotify credentials.

## Configuration

Relevant `APIConfig` values are:

| Setting | Default |
|---------|---------|
| `DEFAULT_OUTPUT_DIR` | `Music` |
| `MAX_CONCURRENT_DOWNLOADS` | `1` |
| `DEFAULT_FORMAT` | `mp3` |
| `API_HOST` | `0.0.0.0` |
| `API_PORT` | `8000` |
| `ALLOWED_ORIGINS` | `[*]` |

`SPOTIFYSAVER_OUTPUT_DIR` overrides the default output directory. The output
directory endpoints are:

- `GET /api/v1/config/output_dir` - Return the configured default directory.
- `GET /api/v1/config/output_dirs` - List immediate subdirectories under a supplied
  `root`, or under the configured default directory.

## Running the API

From the repository root:

```bash
# Poetry
poetry run uvicorn spotifysaver.api.main:app --reload

# Or the installed script
spotifysaver-api
```

The default server URL is `http://127.0.0.1:8000`. Interactive API documentation
is available at `/docs` and `/redoc`.
