"""FastAPI Application Factory"""

from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .routers import (
    authentication,
    backups,
    channel_watcher,
    download,
    jellyfin,
    notifications,
    spotify_settings,
    storage,
    user_preferences,
)
from .config import APIConfig
from .services import (
    ChannelWatcherService,
    DownloadDirectoryStore,
    JellyfinLibraryScanService,
    JellyfinLibraryScanStore,
    NotificationHub,
    StorageProtectionService,
    StorageProtectionStore,
)
from .services.security_store import SecurityStore
from ..config import Config
from ..spotlog import get_logger
from .. import __version__

logger = get_logger("API")

# Get the absolute path to the UI directory
UI_DIR = Path(__file__).parent.parent / "ui"
STATIC_DIR = UI_DIR / "static"
INDEX_HTML = UI_DIR / "index.html"

def create_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        FastAPI: Configured FastAPI application instance
    """
    Config.SPOTIFY_AUTO_OPEN_BROWSER = False

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        security_store = SecurityStore()
        setup_token = security_store.initialize()
        app.state.security_store = security_store
        if setup_token:
            logger.warning(
                "First-run administrator setup token (available to server log readers): %s",
                setup_token,
            )
        _stored_spotify_settings = security_store.get_spotify_settings()
        if _stored_spotify_settings:
            Config.SPOTIFY_CLIENT_ID = _stored_spotify_settings["client_id"]
            Config.SPOTIFY_CLIENT_SECRET = _stored_spotify_settings["client_secret"]
            Config.SPOTIFY_REDIRECT_URI = _stored_spotify_settings["redirect_uri"]

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
        watcher = ChannelWatcherService(
            enqueue_video=download.enqueue_watched_video,
            publish_notification=notification_hub.publish,
        )
        app.state.channel_watcher = watcher
        storage_store = StorageProtectionStore(
            APIConfig.get_channel_watcher_db_path()
        )
        storage_store.initialize()

        def storage_paths() -> list[dict[str, str]]:
            directories = download_directory_store.get_download_directories()
            channels = watcher.store.list_channels()
            paths = [
                {
                    "label": "Audio downloads",
                    "path": directories["audio"] or APIConfig.get_output_dir(),
                },
                {
                    "label": "Video downloads",
                    "path": directories["video"] or APIConfig.get_video_dir(),
                },
                {
                    "label": "Channel Watcher default",
                    "path": directories["watcher"] or APIConfig.get_video_dir(),
                },
            ]
            paths.extend(
                {
                    "label": f"Watched channel: {channel.get('title') or channel['url']}",
                    "path": channel["output_dir"],
                }
                for channel in channels
            )
            paths.extend(download.get_known_download_paths())
            return paths

        storage_protection = StorageProtectionService(
            store=storage_store,
            paths_provider=storage_paths,
            notification_hub=notification_hub,
            queue_kick=download._try_start_next,
        )
        app.state.storage_protection = storage_protection
        download.set_storage_protection_service(storage_protection)
        try:
            await jellyfin_scan.start()
            watcher.start()
            await storage_protection.start()
            yield
        finally:
            try:
                await storage_protection.stop()
                download.set_storage_protection_service(None)
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

    public_api_paths = {
        "/api/v1/auth/status",
        "/api/v1/auth/setup",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/spotify/callback",
    }

    @app.middleware("http")
    async def require_authentication(request: Request, call_next):
        path = request.url.path
        if (
            path in {"/", "/health", "/version", "/favicon.ico"}
            or path.startswith("/static/")
            or path in public_api_paths
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin:
                forwarded_proto = (
                    request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
                )
                forwarded_host = (
                    request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
                )
                expected_scheme = forwarded_proto or request.url.scheme
                expected_host = forwarded_host or request.url.netloc
                expected_origin = (
                    f"{expected_scheme}://{expected_host}"
                ).rstrip("/")
                request_origin = origin.rstrip("/")
                if (
                    request_origin != expected_origin
                    and request_origin not in APIConfig.ALLOWED_ORIGINS
                ):
                    return JSONResponse(
                        {"detail": "Cross-origin request denied"},
                        status_code=403,
                    )

        security_store = getattr(request.app.state, "security_store", None)
        session_token = request.cookies.get(authentication.SESSION_COOKIE)
        if security_store is None or not security_store.is_valid_session(session_token):
            detail = (
                "Initial setup required"
                if security_store is None or not security_store.is_configured()
                else "Authentication required"
            )
            return JSONResponse({"detail": detail}, status_code=401)
        return await call_next(request)

    # Include routers
    app.include_router(authentication.router, prefix="/api/v1", tags=["authentication"])
    app.include_router(backups.router, prefix="/api/v1", tags=["configuration-backup"])
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
    app.include_router(
        spotify_settings.router, prefix="/api/v1", tags=["spotify-settings"]
    )
    app.include_router(storage.router, prefix="/api/v1", tags=["storage"])

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
