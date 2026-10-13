"""FastAPI Application Factory"""

from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from .routers import channel_watcher, download, jellyfin, notifications, user_preferences
from .config import APIConfig
from .services import (
    ChannelWatcherService,
    DownloadDirectoryStore,
    JellyfinLibraryScanService,
    JellyfinLibraryScanStore,
    NotificationHub,
)
from .. import __version__

# Get the absolute path to the UI directory
UI_DIR = Path(__file__).parent.parent / "ui"
STATIC_DIR = UI_DIR / "static"
INDEX_HTML = UI_DIR / "index.html"

def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        FastAPI: Configured FastAPI application instance
    """
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        notification_hub = NotificationHub()
        app.state.notification_hub = notification_hub
        download.set_notification_hub(notification_hub)
        download_directory_store = DownloadDirectoryStore()
        download_directory_store.initialize()
        app.state.download_directory_store = download_directory_store
        jellyfin_scan = JellyfinLibraryScanService(
            JellyfinLibraryScanStore(),
            is_queue_idle=download.is_queue_idle,
        )
        app.state.jellyfin_library_scan = jellyfin_scan
        download.set_jellyfin_library_scan_service(jellyfin_scan)
        await jellyfin_scan.start()
        watcher = ChannelWatcherService(
            enqueue_video=download.enqueue_watched_video,
            publish_notification=notification_hub.publish,
        )
        app.state.channel_watcher = watcher
        try:
            watcher.start()
            yield
        finally:
            try:
                await watcher.stop()
            finally:
                await jellyfin_scan.stop()
                download.set_jellyfin_library_scan_service(None)
                download.set_notification_hub(None)

    app = FastAPI(
        title="SpotifySaver API",
        description="Download music from Spotify via YouTube Music with metadata preservation",
        version=__version__,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Mount static files (only if directory exists)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Configure CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=APIConfig.ALLOWED_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(download.router, prefix="/api/v1", tags=["download"])
    app.include_router(
        channel_watcher.router, prefix="/api/v1", tags=["channel-watcher"]
    )
    app.include_router(
        user_preferences.router, prefix="/api/v1", tags=["download-preferences"]
    )
    app.include_router(
        jellyfin.router, prefix="/api/v1", tags=["jellyfin-library-scan"]
    )
    app.include_router(
        notifications.router, prefix="/api/v1", tags=["notifications"]
    )

    # Serve the main HTML file
    @app.get("/", tags=["UI"])
    async def read_index():
        """Serve the main index.html file for the UI."""
        if INDEX_HTML.exists():
            return FileResponse(str(INDEX_HTML))
        return {"message": "UI not available", "docs": "/docs"}

    @app.get("/api/v1/", tags=["Info"])
    async def root():
        """API root endpoint providing basic info.
        name: SpotifySaver API
        version: Current version of the API
        description: Brief description of the API
        """
        return {
            "name": "SpotifySaver API",
            "version": __version__,
            "description": "Download music from Spotify via YouTube Music",
            "docs": "/docs",
            "redoc": "/redoc",
        }

    @app.get("/health", tags=["Info"])
    async def health_check():
        """Health check endpoint to verify API is running."""
        return {"status": "healthy", "service": "SpotifySaver API"}

    @app.get("/version", tags=["Info"])
    async def get_version():
        """Get the current version of the API."""
        return {"version": __version__}

    return app
