"""Shared test fixtures for the TikTok plugin."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass
class FakeGameInfo:
    """Minimal stand-in for ``reeln.models.game.GameInfo``."""

    date: str = "2026-01-15"
    home_team: str = "Eagles"
    away_team: str = "Hawks"
    sport: str = "hockey"
    game_number: int = 1
    venue: str = ""
    game_time: str = ""
    description: str = ""
    thumbnail: str = ""


@dataclass
class FakePlan:
    """Minimal stand-in for ``RenderPlan``."""

    width: int | None = 1080
    height: int | None = 1920
    output: Path | None = None
    filter_complex: str | None = "overlay"


@dataclass
class FakeResult:
    """Minimal stand-in for ``RenderResult``."""

    output: Path | None = None


@pytest.fixture()
def game_info() -> FakeGameInfo:
    return FakeGameInfo()


@pytest.fixture()
def client_secret_file(tmp_path: Path) -> Path:
    secret = tmp_path / "client_secret.txt"
    secret.write_text("test-client-secret-abc")
    return secret


@pytest.fixture()
def credentials_cache(tmp_path: Path) -> Path:
    cache = tmp_path / "oauth.json"
    cache.write_text(
        json.dumps(
            {
                "access_token": "act.test-token-123",
                "refresh_token": "rft.test-refresh-456",
                "expires_at": 9999999999.0,
                "open_id": "test-open-id",
                "scope": "video.upload,user.info.basic",
            }
        )
    )
    return cache


@pytest.fixture()
def plugin_config(client_secret_file: Path, credentials_cache: Path) -> dict[str, Any]:
    """Return a config with upload_shorts enabled and valid credential paths."""
    return {
        "upload_shorts": True,
        "client_key": "test-client-key",
        "client_secret_file": str(client_secret_file),
        "credentials_cache": str(credentials_cache),
    }


@pytest.fixture()
def video_file(tmp_path: Path) -> Path:
    """Create a small fake video file."""
    video = tmp_path / "short.mp4"
    video.write_bytes(b"\x00" * 1024)
    return video
