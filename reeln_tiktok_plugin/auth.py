"""OAuth2 token management for the TikTok Content Posting API."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reeln.core.config import data_dir

log: logging.Logger = logging.getLogger(__name__)

TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


class AuthError(Exception):
    """Raised when authentication fails."""


@dataclass(frozen=True)
class Credentials:
    """Cached OAuth2 credentials for TikTok."""

    access_token: str
    refresh_token: str
    expires_at: float
    open_id: str
    scope: str


def default_credentials_path() -> Path:
    """Return the default TikTok credentials cache path."""
    return Path(data_dir() / "tiktok" / "oauth.json")


def read_client_secret(path: Path) -> str:
    """Read the client secret from a file.

    Raises:
        AuthError: If the file is missing or empty.
    """
    if not path.exists():
        raise AuthError(f"Client secret file not found: {path}")

    secret = path.read_text().strip()
    if not secret:
        raise AuthError(f"Client secret file is empty: {path}")

    return secret


def load_credentials(cache_path: Path) -> Credentials:
    """Load credentials from a JSON cache file.

    Expected JSON format::

        {
            "access_token": "...",
            "refresh_token": "...",
            "expires_at": 1712345678.0,
            "open_id": "...",
            "scope": "video.upload,user.info.basic"
        }

    Raises:
        AuthError: If the file is missing, empty, or malformed.
    """
    if not cache_path.exists():
        raise AuthError(f"Credentials file not found: {cache_path}")

    text = cache_path.read_text().strip()
    if not text:
        raise AuthError(f"Credentials file is empty: {cache_path}")

    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthError(f"Invalid JSON in credentials file: {cache_path}") from exc

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    if not access_token or not refresh_token:
        raise AuthError(f"Credentials file missing access_token or refresh_token: {cache_path}")

    return Credentials(
        access_token=str(access_token),
        refresh_token=str(refresh_token),
        expires_at=float(data.get("expires_at", 0)),
        open_id=str(data.get("open_id", "")),
        scope=str(data.get("scope", "")),
    )


def _save_credentials(creds: Credentials, cache_path: Path) -> None:
    """Persist credentials to *cache_path* with ``0o600`` permissions."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "access_token": creds.access_token,
            "refresh_token": creds.refresh_token,
            "expires_at": creds.expires_at,
            "open_id": creds.open_id,
            "scope": creds.scope,
        },
        indent=2,
    )
    cache_path.write_text(payload)
    if os.name != "nt":
        cache_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def refresh_if_expired(
    creds: Credentials,
    client_key: str,
    client_secret: str,
    cache_path: Path,
) -> Credentials:
    """Refresh the access token if it is within 60 seconds of expiry.

    Sends ``grant_type=refresh_token`` to TikTok's token endpoint and
    persists the new credentials back to *cache_path*.

    Raises:
        AuthError: If the refresh request fails.
    """
    if creds.expires_at - time.time() > 60:
        return creds

    payload = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": creds.refresh_token,
        }
    ).encode()

    request = urllib.request.Request(
        TIKTOK_TOKEN_URL,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        raise AuthError(f"Token refresh HTTP {exc.code}: {error_body[:200]}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"Token refresh request failed: {exc.reason}") from exc

    try:
        data: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise AuthError(f"Invalid JSON in token response: {body[:200]}") from exc

    new_access = data.get("access_token", "")
    new_refresh = data.get("refresh_token", "")
    expires_in = int(data.get("expires_in", 0))
    if not new_access:
        raise AuthError(f"Token response missing access_token: {body[:200]}")

    new_creds = Credentials(
        access_token=str(new_access),
        refresh_token=str(new_refresh) if new_refresh else creds.refresh_token,
        expires_at=time.time() + expires_in,
        open_id=str(data.get("open_id", creds.open_id)),
        scope=str(data.get("scope", creds.scope)),
    )

    _save_credentials(new_creds, cache_path)
    return new_creds


def get_access_token(
    client_key: str,
    client_secret_file: Path,
    cache_path: Path,
) -> str:
    """Load credentials, refresh if needed, and return a bearer access token.

    Raises:
        AuthError: If loading, reading the secret, or refreshing fails.
    """
    client_secret = read_client_secret(client_secret_file)
    creds = load_credentials(cache_path)
    creds = refresh_if_expired(creds, client_key, client_secret, cache_path)
    return creds.access_token
