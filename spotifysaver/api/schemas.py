"""Pydantic schemas for API requests and responses"""

from typing import Dict, List, Optional
from pydantic import BaseModel, HttpUrl, Field


class DownloadRequest(BaseModel):
    """Schema for download request."""

    spotify_url: HttpUrl = Field(
        ...,
        description="Spotify track, album, or playlist URL, or YouTube playlist/album URL",
        example="https://open.spotify.com/track/2kd0T6zgABT8P0s2h9QU5O",
    )
    download_lyrics: bool = Field(
        default=False, description="Whether to download synchronized lyrics"
    )
    download_cover: bool = Field(
        default=True, description="Whether to download cover art"
    )
    generate_nfo: bool = Field(
        default=False, description="Whether to generate NFO metadata files"
    )
    overwrite_existing: bool = Field(
        default=False,
        description="Whether to overwrite local files that already exist. Missing files are always downloaded.",
    )
    download_files: Optional[bool] = Field(
        default=None,
        description="Deprecated alias kept for backwards compatibility.",
    )
    output_format: str = Field(
        default="m4a",
        description="Audio format for downloaded files",
        pattern="^(m4a|mp3)$",
    )
    bit_rate: int = Field(
        default=256,
        description="Bit rate for audio files in kbps",
        ge=64, le=256,  # Valid range for MP3 bit rates
    )
    output_dir: Optional[str] = Field(
        default="Music", description="Custom output directory (optional)"
    )


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
    content_type: str = Field(
        ..., description="Type of content (track, album, playlist)"
    )
    message: str = Field(..., description="Status message")


class DownloadStatus(BaseModel):
    """Schema for download status."""

    task_id: str
    status: str  # pending, processing, completed, failed
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
    output_format: str = "m4a"
    bit_rate: Optional[int] = None
    error_message: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ErrorResponse(BaseModel):
    """Schema for error responses."""

    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Error message")
    details: Optional[str] = Field(None, description="Additional error details")
