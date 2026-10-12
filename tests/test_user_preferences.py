import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from spotifysaver.api.routers.user_preferences import (
    get_download_directory_preferences,
    set_download_directory_preference,
)
from spotifysaver.api.schemas import DownloadDirectoryPreferenceUpdate
from spotifysaver.api.services.download_directory_store import DownloadDirectoryStore


def _request(store):
    app = SimpleNamespace(
        state=SimpleNamespace(
            download_directory_store=store
        )
    )
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/preferences/download-directories",
            "raw_path": b"/api/v1/preferences/download-directories",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 8000),
            "client": ("testclient", 50000),
            "root_path": "",
            "app": app,
        }
    )


def test_download_directory_preferences_are_shared_across_browsers(tmp_path):
    store = DownloadDirectoryStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    watcher_dir = tmp_path / "watcher"
    watcher_dir.mkdir()

    initial = asyncio.run(
        get_download_directory_preferences(_request(store))
    )
    assert initial == {"audio": None, "video": None, "watcher": None}

    updated = asyncio.run(
        set_download_directory_preference(
            "audio",
            DownloadDirectoryPreferenceUpdate(directory=str(audio_dir)),
            _request(store),
        )
    )
    assert updated["audio"] == str(audio_dir.resolve())

    asyncio.run(
        set_download_directory_preference(
            "video",
            DownloadDirectoryPreferenceUpdate(directory=str(video_dir)),
            _request(store),
        )
    )
    asyncio.run(
        set_download_directory_preference(
            "watcher",
            DownloadDirectoryPreferenceUpdate(directory=str(watcher_dir)),
            _request(store),
        )
    )

    reloaded_store = DownloadDirectoryStore(str(tmp_path / "settings.sqlite3"))
    assert asyncio.run(
        get_download_directory_preferences(_request(reloaded_store))
    ) == {
        "audio": str(audio_dir.resolve()),
        "video": str(video_dir.resolve()),
        "watcher": str(watcher_dir.resolve()),
    }


def test_download_directory_preferences_migrate_from_browser_profiles(tmp_path):
    database_path = tmp_path / "watcher.sqlite3"
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    newer_audio_dir = tmp_path / "newer-audio"
    newer_audio_dir.mkdir()
    video_dir = tmp_path / "video"
    video_dir.mkdir()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE user_download_directories (
                profile_id TEXT PRIMARY KEY,
                audio_output_dir TEXT,
                video_output_dir TEXT,
                watcher_output_dir TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO user_download_directories
            VALUES ('older', ?, NULL, NULL)
            """,
            (str(audio_dir),),
        )
        connection.execute(
            """
            INSERT INTO user_download_directories
            VALUES ('newer', ?, ?, NULL)
            """,
            (str(newer_audio_dir), str(video_dir)),
        )

    store = DownloadDirectoryStore(str(database_path))
    store.initialize()
    assert store.get_download_directories() == {
        "audio": str(audio_dir),
        "video": str(video_dir),
        "watcher": None,
    }


def test_deleted_download_directory_falls_back_to_nearest_existing_parent(tmp_path):
    store = DownloadDirectoryStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    existing_parent = tmp_path / "media"
    deleted_parent = existing_parent / "deleted-parent"
    deleted_directory = deleted_parent / "deleted-folder"
    deleted_directory.mkdir(parents=True)
    deleted_directory.rmdir()
    deleted_parent.rmdir()
    store.set_download_directory("audio", str(deleted_directory))

    assert store.get_download_directories()["audio"] == str(existing_parent)
    assert store.get_download_directories()["audio"] == str(existing_parent)


def test_deleted_download_directory_falls_back_to_filesystem_root(tmp_path, monkeypatch):
    store = DownloadDirectoryStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    missing_directory = tmp_path / "deleted" / "folder"
    filesystem_root = missing_directory.anchor
    store.set_download_directory("audio", str(missing_directory))

    monkeypatch.setattr(
        type(missing_directory),
        "is_dir",
        lambda directory: str(directory) == filesystem_root,
    )

    assert store.get_download_directories()["audio"] == filesystem_root


def test_download_directory_preference_rejects_invalid_folder_and_kind(tmp_path):
    store = DownloadDirectoryStore(str(tmp_path / "settings.sqlite3"))
    store.initialize()
    request = _request(store)
    payload = DownloadDirectoryPreferenceUpdate(directory=str(tmp_path / "missing"))

    with pytest.raises(HTTPException) as invalid_folder:
        asyncio.run(
            set_download_directory_preference("audio", payload, request)
        )
    assert invalid_folder.value.status_code == 400

    existing_dir = tmp_path / "existing"
    existing_dir.mkdir()
    with pytest.raises(HTTPException) as invalid_kind:
        asyncio.run(
            set_download_directory_preference(
                "music",
                DownloadDirectoryPreferenceUpdate(directory=str(existing_dir)),
                request,
            )
        )
    assert invalid_kind.value.status_code == 400
