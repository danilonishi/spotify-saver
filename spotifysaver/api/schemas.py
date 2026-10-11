"""Pydantic schemas for API requests and responses"""

from typing import Dict, List, Optional
from pydantic import BaseModel, HttpUrl, Field
from .config import APIConfig


class DownloadRequest(BaseModel):
    """Schema for download request."""

    spotify_url: HttpUrl = Field(
        ...,
        description="Spotify track, album, or playlist URL, or YouTube video, playlist, or album URL",
        example="https://open.spotify.com/track/2kd0T6zgABT8P0s2h9QU5O",
    )
    download_lyrics: bool = Field(
        default=False, description="Whether to download synchronized lyrics"
    )
    download_cover: bool = Field(default=True, description="Whether to download cover art")
    generate_nfo: bool = Field(default=False, description="Whether to generate NFO metadata files")
    overwrite_existing: bool = Field(
        default=False,
        description="Whether to overwrite local files that already exist. Missing files are always downloaded.",
    )
    download_files: Optional[bool] = Field(
        default=None,
        description="Deprecated alias kept for backwards compatibility.",
    )
    output_format: str = Field(
        default=APIConfig.DEFAULT_FORMAT,
        description="Audio format for downloaded files",
        pattern="^(m4a|mp3)$",
    )
    bit_rate: int = Field(
        default=256,
        description="Bit rate for audio files in kbps",
        ge=64,
        le=256,  # Valid range for MP3 bit rates
    )
    output_dir: Optional[str] = Field(
        default="Music", description="Custom output directory (optional)"
    )
    media_type: str = Field(
        default="audio",
        description="Kind of download: 'audio' or 'video' (video requires a YouTube URL)",
        pattern="^(audio|video)$",
    )
    video_format: str = Field(
        default="mp4",
        description="Container for video downloads",
        pattern="^(mp4|mkv|webm)$",
    )
    video_resolution: int = Field(
        default=0,
        description="Maximum video height in pixels; 0 means best available",
        ge=0,
        le=4320,
    )
    include_subtitles: bool = Field(
        default=False, description="Whether to download subtitles for videos"
    )


class WatchedChannelRequest(BaseModel):
    """Settings required to watch a channel for new content."""

    platform: str = Field(default="youtube", pattern="^youtube$")
    url: str
    output_dir: str
    interval: str = Field(pattern="^(manual|hourly|daily|weekly)$")
    timezone: str = Field(default="UTC", description="IANA timezone for the selected schedule")
    check_hour: int = Field(ge=0, le=23)
    check_minute: int = Field(ge=0, le=59)
    check_weekday: Optional[int] = Field(default=None, ge=0, le=6)
    video_format: str = Field(default="mp4", pattern="^(mp4|mkv|webm)$")
    video_resolution: int = Field(default=0, ge=0, le=4320)
    include_subtitles: bool = False


class WatchedChannel(BaseModel):
    """Persisted watched channel settings and last successful check."""

    id: str
    platform: str
    url: str
    title: Optional[str] = None
    output_dir: str
    interval: str
    timezone: str
    check_hour: int
    check_minute: int
    check_weekday: Optional[int] = None
    video_format: str
    video_resolution: int
    include_subtitles: bool
    created_at: str
    last_checked_at: Optional[str] = None
    check_started_at: Optional[str] = None
    next_check_at: str
    last_error: Optional[str] = None


class ChannelWatcherStatus(BaseModel):
    """Global channel watcher state that applies to all watched channels."""

    youtube_cooldown_until: Optional[str] = None


class TrackInfo(BaseModel):
    """Schema for track information."""

    name: str
    artists: List[str]
    album_name: Optional[str] = None
    duration: int  # in seconds
    number: int
    uri: str


class AlbumInfo(BaseModel):
    """Schema for album information."""

    name: str
    artists: List[str]
    release_date: str
    total_tracks: int
    cover_url: Optional[str] = None
    tracks: List[TrackInfo]


class PlaylistInfo(BaseModel):
    """Schema for playlist information."""

    name: str
    owner: str
    description: Optional[str] = None
    total_tracks: int
    cover_url: Optional[str] = None
    tracks: List[TrackInfo]


class DownloadResponse(BaseModel):
    """Schema for download response."""

    task_id: str = Field(..., description="Unique task identifier")
    status: str = Field(..., description="Current status of the download")
    spotify_url: str = Field(..., description="Original Spotify or YouTube source URL")
    content_type: str = Field(..., description="Type of content (track, album, playlist)")
    message: str = Field(..., description="Status message")


class DownloadStatus(BaseModel):
    """Schema for download status."""

    task_id: str
    status: str  # pending, processing, cancelling, cancelled, completed, failed
    progress: int  # 0-100
    current_track: Optional[str] = None
    current_track_number: Optional[int] = None
    current_track_status: Optional[str] = None
    track_updates: List[Dict[str, object]] = Field(default_factory=list)
    total_tracks: int = 0
    completed_tracks: int = 0
    failed_tracks: int = 0
    failed_track_names: List[str] = Field(default_factory=list)
    output_directory: Optional[str] = None
    output_format: str = APIConfig.DEFAULT_FORMAT
    bit_rate: Optional[int] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    spotify_url: Optional[str] = None
    output_dir: Optional[str] = None
    content_type: Optional[str] = None
    media_type: str = "audio"
    title: Optional[str] = Field(
        default=None,
        description="Display name (album/playlist/track title) once resolved",
    )
    playlist_verification: Optional[str] = Field(
        default=None,
        description="Playlist metadata phase: checking or rechecking",
    )
    tracks: List[TrackInfo] = Field(
        default_factory=list,
        description="Full track listing, populated once metadata is resolved",
    )
    queue_position: Optional[int] = Field(
        default=None,
        description="1-based position in the pending queue; 0 means it is currently downloading",
    )


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Error message")
    details: Optional[str] = Field(None, description="Additional error details")
