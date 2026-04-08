"""Tests for the auth module."""

from __future__ import annotations

import json
import os
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reeln_tiktok_plugin.auth import (
    AuthError,
    Credentials,
    _save_credentials,
    default_credentials_path,
    get_access_token,
    load_credentials,
    read_client_secret,
    refresh_if_expired,
)


class TestDefaultCredentialsPath:
    def test_returns_path_under_data_dir(self) -> None:
        with patch("reeln_tiktok_plugin.auth.data_dir", return_value=Path("/fake/data")):
            result = default_credentials_path()
        assert result == Path("/fake/data/tiktok/oauth.json")


class TestReadClientSecret:
    def test_reads_secret(self, client_secret_file: Path) -> None:
        assert read_client_secret(client_secret_file) == "test-client-secret-abc"

    def test_strips_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "secret.txt"
        f.write_text("  my-secret  \n")
        assert read_client_secret(f) == "my-secret"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(AuthError, match="not found"):
            read_client_secret(tmp_path / "missing.txt")

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.txt"
        f.write_text("   \n")
        with pytest.raises(AuthError, match="empty"):
            read_client_secret(f)


class TestLoadCredentials:
    def test_loads_valid_json(self, credentials_cache: Path) -> None:
        creds = load_credentials(credentials_cache)
        assert creds.access_token == "act.test-token-123"
        assert creds.refresh_token == "rft.test-refresh-456"
        assert creds.expires_at == 9999999999.0
        assert creds.open_id == "test-open-id"
        assert creds.scope == "video.upload,user.info.basic"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(AuthError, match="not found"):
            load_credentials(tmp_path / "missing.json")

    def test_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.json"
        f.write_text("")
        with pytest.raises(AuthError, match="empty"):
            load_credentials(f)

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.json"
        f.write_text("{invalid")
        with pytest.raises(AuthError, match="Invalid JSON"):
            load_credentials(f)

    def test_missing_access_token(self, tmp_path: Path) -> None:
        f = tmp_path / "no_token.json"
        f.write_text(json.dumps({"refresh_token": "rft.123"}))
        with pytest.raises(AuthError, match="missing access_token"):
            load_credentials(f)

    def test_missing_refresh_token(self, tmp_path: Path) -> None:
        f = tmp_path / "no_refresh.json"
        f.write_text(json.dumps({"access_token": "act.123"}))
        with pytest.raises(AuthError, match="missing access_token or refresh_token"):
            load_credentials(f)

    def test_defaults_for_optional_fields(self, tmp_path: Path) -> None:
        f = tmp_path / "minimal.json"
        f.write_text(json.dumps({"access_token": "act.x", "refresh_token": "rft.y"}))
        creds = load_credentials(f)
        assert creds.expires_at == 0.0
        assert creds.open_id == ""
        assert creds.scope == ""


class TestSaveCredentials:
    def test_saves_json_with_permissions(self, tmp_path: Path) -> None:
        creds = Credentials(
            access_token="act.new",
            refresh_token="rft.new",
            expires_at=1234567890.0,
            open_id="oid",
            scope="video.upload",
        )
        cache = tmp_path / "sub" / "oauth.json"
        _save_credentials(creds, cache)

        data = json.loads(cache.read_text())
        assert data["access_token"] == "act.new"
        assert data["refresh_token"] == "rft.new"
        assert data["expires_at"] == 1234567890.0

        if os.name != "nt":
            assert oct(cache.stat().st_mode & 0o777) == "0o600"

    def test_skips_chmod_on_windows(self, tmp_path: Path) -> None:
        creds = Credentials("a", "r", 0.0, "", "")
        cache = tmp_path / "win" / "oauth.json"
        with patch("reeln_tiktok_plugin.auth.os.name", "nt"):
            _save_credentials(creds, cache)
        assert cache.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        creds = Credentials("a", "r", 0.0, "", "")
        cache = tmp_path / "deep" / "nested" / "oauth.json"
        _save_credentials(creds, cache)
        assert cache.exists()


class TestRefreshIfExpired:
    def _make_creds(self, expires_at: float) -> Credentials:
        return Credentials(
            access_token="act.old",
            refresh_token="rft.old",
            expires_at=expires_at,
            open_id="oid",
            scope="video.upload",
        )

    def test_no_refresh_when_fresh(self, tmp_path: Path) -> None:
        creds = self._make_creds(time.time() + 3600)
        result = refresh_if_expired(creds, "ck", "cs", tmp_path / "o.json")
        assert result is creds

    def test_refreshes_when_expired(self, tmp_path: Path) -> None:
        creds = self._make_creds(time.time() - 10)
        cache = tmp_path / "oauth.json"

        response_data = json.dumps(
            {
                "access_token": "act.new",
                "refresh_token": "rft.new",
                "expires_in": 7200,
                "open_id": "new-oid",
                "scope": "video.upload,user.info.basic",
            }
        ).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = refresh_if_expired(creds, "ck", "cs", cache)

        assert result.access_token == "act.new"
        assert result.refresh_token == "rft.new"
        assert result.open_id == "new-oid"
        assert cache.exists()

    def test_refreshes_within_60s_of_expiry(self, tmp_path: Path) -> None:
        creds = self._make_creds(time.time() + 30)
        cache = tmp_path / "oauth.json"

        response_data = json.dumps(
            {"access_token": "act.refreshed", "expires_in": 7200}
        ).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = refresh_if_expired(creds, "ck", "cs", cache)

        assert result.access_token == "act.refreshed"
        assert result.refresh_token == "rft.old"

    def test_http_error_raises_auth_error(self, tmp_path: Path) -> None:
        import urllib.error

        creds = self._make_creds(time.time() - 10)

        exc = urllib.error.HTTPError(
            url="https://example.com",
            code=401,
            msg="Unauthorized",
            hdrs=None,  # type: ignore[arg-type]
            fp=BytesIO(b'{"error": "invalid"}'),
        )

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(AuthError, match="HTTP 401"):
            refresh_if_expired(creds, "ck", "cs", tmp_path / "o.json")

    def test_url_error_raises_auth_error(self, tmp_path: Path) -> None:
        import urllib.error

        creds = self._make_creds(time.time() - 10)
        exc = urllib.error.URLError(reason="Connection refused")

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(AuthError, match="Connection refused"):
            refresh_if_expired(creds, "ck", "cs", tmp_path / "o.json")

    def test_invalid_json_response(self, tmp_path: Path) -> None:
        creds = self._make_creds(time.time() - 10)

        mock_response = MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            pytest.raises(AuthError, match="Invalid JSON"),
        ):
            refresh_if_expired(creds, "ck", "cs", tmp_path / "o.json")

    def test_missing_access_token_in_response(self, tmp_path: Path) -> None:
        creds = self._make_creds(time.time() - 10)

        response_data = json.dumps({"refresh_token": "rft.new"}).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            pytest.raises(AuthError, match="missing access_token"),
        ):
            refresh_if_expired(creds, "ck", "cs", tmp_path / "o.json")


class TestGetAccessToken:
    def test_returns_token(
        self, client_secret_file: Path, credentials_cache: Path
    ) -> None:
        token = get_access_token("ck", client_secret_file, credentials_cache)
        assert token == "act.test-token-123"

    def test_secret_file_missing(self, tmp_path: Path, credentials_cache: Path) -> None:
        with pytest.raises(AuthError, match="not found"):
            get_access_token("ck", tmp_path / "missing.txt", credentials_cache)

    def test_credentials_missing(self, client_secret_file: Path, tmp_path: Path) -> None:
        with pytest.raises(AuthError, match="not found"):
            get_access_token("ck", client_secret_file, tmp_path / "missing.json")
