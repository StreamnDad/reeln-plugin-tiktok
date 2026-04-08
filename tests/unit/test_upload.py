"""Tests for the upload module."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from reeln_tiktok_plugin.upload import (
    CreatorInfo,
    InitResult,
    UploadError,
    UploadResult,
    format_tiktok_error,
    init_upload,
    poll_status,
    put_chunks,
    query_creator_info,
    upload_video,
    upload_video_from_url,
)

# ------------------------------------------------------------------
# format_tiktok_error
# ------------------------------------------------------------------


class TestFormatTiktokError:
    def test_empty_string(self) -> None:
        assert format_tiktok_error("") == "(empty response)"

    def test_plain_text(self) -> None:
        assert format_tiktok_error("something went wrong") == "something went wrong"

    def test_json_error_with_code(self) -> None:
        body = json.dumps({"error": {"code": "access_denied", "message": "bad token"}})
        assert format_tiktok_error(body) == "access_denied: bad token"

    def test_json_error_without_code(self) -> None:
        body = json.dumps({"error": {"message": "unknown"}})
        assert format_tiktok_error(body) == "unknown"

    def test_json_non_dict_error(self) -> None:
        body = json.dumps({"error": "flat string"})
        assert format_tiktok_error(body) == body[:200]

    def test_non_dict_json(self) -> None:
        body = json.dumps([1, 2, 3])
        assert format_tiktok_error(body) == body[:200]

    def test_truncates_long_text(self) -> None:
        body = "x" * 500
        assert len(format_tiktok_error(body)) == 200


# ------------------------------------------------------------------
# _json_post (tested indirectly via init_upload / poll_status)
# ------------------------------------------------------------------


def _mock_urlopen(response_body: bytes) -> MagicMock:
    """Return a mock suitable for ``urllib.request.urlopen``."""
    mock_response = MagicMock()
    mock_response.read.return_value = response_body
    mock_response.__enter__ = MagicMock(return_value=mock_response)
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


# ------------------------------------------------------------------
# init_upload
# ------------------------------------------------------------------


class TestInitUpload:
    _DEFAULTS: ClassVar[dict[str, Any]] = {
        "access_token": "act.tok",
        "video_size": 1024,
        "chunk_size": 1024,
        "caption": "Hello TikTok",
        "direct_post": False,
        "privacy_level": "SELF_ONLY",
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_cover_timestamp_ms": 1000,
    }

    def test_inbox_happy_path(self) -> None:
        resp = json.dumps(
            {"data": {"publish_id": "pub123", "upload_url": "https://up.tiktok.com/vid"}}
        ).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            result = init_upload(**self._DEFAULTS)

        assert result == InitResult(publish_id="pub123", upload_url="https://up.tiktok.com/vid")

    def test_direct_post_uses_video_init_path(self) -> None:
        resp = json.dumps(
            {"data": {"publish_id": "pub456", "upload_url": "https://up.tiktok.com/vid2"}}
        ).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            init_upload(**{**self._DEFAULTS, "direct_post": True})

        req = mock_open.call_args[0][0]
        assert "/v2/post/publish/video/init/" in req.full_url
        assert "inbox" not in req.full_url

    def test_inbox_uses_inbox_path(self) -> None:
        resp = json.dumps(
            {"data": {"publish_id": "p", "upload_url": "https://u"}}
        ).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            init_upload(**self._DEFAULTS)

        req = mock_open.call_args[0][0]
        assert "/v2/post/publish/inbox/video/init/" in req.full_url

    def test_missing_publish_id(self) -> None:
        resp = json.dumps({"data": {"upload_url": "https://u"}}).encode()

        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            pytest.raises(UploadError, match="missing publish_id"),
        ):
            init_upload(**self._DEFAULTS)

    def test_missing_upload_url_file_upload(self) -> None:
        resp = json.dumps({"data": {"publish_id": "p"}}).encode()

        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            pytest.raises(UploadError, match="missing upload_url for FILE_UPLOAD"),
        ):
            init_upload(**self._DEFAULTS)

    def test_http_error(self) -> None:
        import urllib.error

        exc = urllib.error.HTTPError(
            url="https://x", code=400, msg="Bad", hdrs=None, fp=BytesIO(b"")  # type: ignore[arg-type]
        )

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(UploadError, match="HTTP 400"):
            init_upload(**self._DEFAULTS)

    def test_url_error(self) -> None:
        import urllib.error

        exc = urllib.error.URLError(reason="DNS fail")

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(UploadError, match="DNS fail"):
            init_upload(**self._DEFAULTS)

    def test_invalid_json(self) -> None:
        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(b"not-json")),
            pytest.raises(UploadError, match="Invalid JSON"),
        ):
            init_upload(**self._DEFAULTS)

    def test_multi_chunk_count(self) -> None:
        resp = json.dumps(
            {"data": {"publish_id": "p", "upload_url": "https://u"}}
        ).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            init_upload(**{**self._DEFAULTS, "video_size": 2500, "chunk_size": 1000})

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["source_info"]["total_chunk_count"] == 3


# ------------------------------------------------------------------
# put_chunks
# ------------------------------------------------------------------


class TestPutChunks:
    def test_single_chunk(self, video_file: Path) -> None:
        mock_resp = _mock_urlopen(b"")

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            put_chunks("https://up.tiktok.com/vid", video_file, chunk_size=2048)

        assert mock_open.call_count == 1
        req = mock_open.call_args[0][0]
        assert req.get_header("Content-range") == "bytes 0-1023/1024"
        assert req.get_header("Content-type") == "video/mp4"

    def test_multi_chunk(self, tmp_path: Path) -> None:
        video = tmp_path / "big.mp4"
        video.write_bytes(b"\x00" * 2500)

        mock_resp = _mock_urlopen(b"")

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_open:
            put_chunks("https://up.tiktok.com/vid", video, chunk_size=1000)

        assert mock_open.call_count == 3
        ranges = [
            mock_open.call_args_list[i][0][0].get_header("Content-range") for i in range(3)
        ]
        assert ranges == [
            "bytes 0-999/2500",
            "bytes 1000-1999/2500",
            "bytes 2000-2499/2500",
        ]

    def test_http_error(self, video_file: Path) -> None:
        import urllib.error

        exc = urllib.error.HTTPError(
            url="https://x", code=500, msg="ISE", hdrs=None, fp=BytesIO(b"err")  # type: ignore[arg-type]
        )

        with (
            patch("urllib.request.urlopen", side_effect=exc),
            pytest.raises(UploadError, match="Chunk upload failed at offset 0"),
        ):
            put_chunks("https://up.tiktok.com/vid", video_file, chunk_size=2048)

    def test_url_error(self, video_file: Path) -> None:
        import urllib.error

        exc = urllib.error.URLError(reason="timeout")

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(UploadError, match="timeout"):
            put_chunks("https://up.tiktok.com/vid", video_file, chunk_size=2048)


# ------------------------------------------------------------------
# poll_status
# ------------------------------------------------------------------


class TestPollStatus:
    def test_complete_on_first_poll(self) -> None:
        resp = json.dumps({"data": {"status": "PUBLISH_COMPLETE"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            status = poll_status("pub1", "act.tok", poll_interval=0, max_attempts=3)

        assert status == "PUBLISH_COMPLETE"

    def test_inbox_status(self) -> None:
        resp = json.dumps({"data": {"status": "SEND_TO_USER_INBOX"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            status = poll_status("pub1", "act.tok", poll_interval=0, max_attempts=3)

        assert status == "SEND_TO_USER_INBOX"

    def test_complete_after_retries(self) -> None:
        processing = _mock_urlopen(json.dumps({"data": {"status": "PROCESSING_UPLOAD"}}).encode())
        complete = _mock_urlopen(json.dumps({"data": {"status": "PUBLISH_COMPLETE"}}).encode())

        with patch("urllib.request.urlopen", side_effect=[processing, processing, complete]):
            status = poll_status("pub1", "act.tok", poll_interval=0, max_attempts=5)

        assert status == "PUBLISH_COMPLETE"

    def test_failed_raises(self) -> None:
        resp = json.dumps(
            {"data": {"status": "FAILED", "fail_reason": "video too long"}}
        ).encode()

        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            pytest.raises(UploadError, match="video too long"),
        ):
            poll_status("pub1", "act.tok", poll_interval=0, max_attempts=3)

    def test_failed_unknown_reason(self) -> None:
        resp = json.dumps({"data": {"status": "FAILED"}}).encode()

        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            pytest.raises(UploadError, match="unknown"),
        ):
            poll_status("pub1", "act.tok", poll_interval=0, max_attempts=3)

    def test_timeout(self) -> None:
        resp = json.dumps({"data": {"status": "PROCESSING_UPLOAD"}}).encode()

        with (
            patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)),
            pytest.raises(UploadError, match="not complete after 2"),
        ):
            poll_status("pub1", "act.tok", poll_interval=0, max_attempts=2)


# ------------------------------------------------------------------
# upload_video (end-to-end with mocks)
# ------------------------------------------------------------------


class TestUploadVideo:
    _DEFAULTS: ClassVar[dict[str, Any]] = {
        "access_token": "act.tok",
        "caption": "Test",
        "direct_post": False,
        "privacy_level": "SELF_ONLY",
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_cover_timestamp_ms": 1000,
        "chunk_size_bytes": 2048,
        "poll_interval": 0,
        "max_attempts": 3,
    }

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(UploadError, match="not found"):
            upload_video(file_path=tmp_path / "nope.mp4", **self._DEFAULTS)

    def test_full_flow(self, video_file: Path) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", "https://up"),
            ),
            patch("reeln_tiktok_plugin.upload.put_chunks"),
            patch(
                "reeln_tiktok_plugin.upload.poll_status",
                return_value="SEND_TO_USER_INBOX",
            ),
        ):
            result = upload_video(file_path=video_file, **self._DEFAULTS)

        assert result == UploadResult(
            publish_id="pub1", status="SEND_TO_USER_INBOX", share_url=""
        )

    def test_init_failure_propagates(self, video_file: Path) -> None:
        with patch(
            "reeln_tiktok_plugin.upload.init_upload",
            side_effect=UploadError("init fail"),
        ), pytest.raises(UploadError, match="init fail"):
            upload_video(file_path=video_file, **self._DEFAULTS)

    def test_put_failure_propagates(self, video_file: Path) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", "https://up"),
            ),
            patch(
                "reeln_tiktok_plugin.upload.put_chunks",
                side_effect=UploadError("chunk fail"),
            ),
            pytest.raises(UploadError, match="chunk fail"),
        ):
            upload_video(file_path=video_file, **self._DEFAULTS)

    def test_poll_failure_propagates(self, video_file: Path) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", "https://up"),
            ),
            patch("reeln_tiktok_plugin.upload.put_chunks"),
            patch(
                "reeln_tiktok_plugin.upload.poll_status",
                side_effect=UploadError("poll fail"),
            ),
            pytest.raises(UploadError, match="poll fail"),
        ):
            upload_video(file_path=video_file, **self._DEFAULTS)


# ------------------------------------------------------------------
# query_creator_info
# ------------------------------------------------------------------


class TestQueryCreatorInfo:
    def test_happy_path(self) -> None:
        resp = json.dumps(
            {
                "data": {
                    "creator_username": "user1",
                    "creator_nickname": "User One",
                    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
                    "comment_disabled": False,
                    "duet_disabled": True,
                    "stitch_disabled": False,
                    "max_video_post_duration_sec": 600,
                }
            }
        ).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            info = query_creator_info("act.tok")

        assert info == CreatorInfo(
            creator_username="user1",
            creator_nickname="User One",
            privacy_level_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY"),
            comment_disabled=False,
            duet_disabled=True,
            stitch_disabled=False,
            max_video_post_duration_sec=600,
        )

    def test_empty_options(self) -> None:
        resp = json.dumps({"data": {}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            info = query_creator_info("act.tok")

        assert info.privacy_level_options == ()
        assert info.max_video_post_duration_sec == 0

    def test_non_list_options(self) -> None:
        resp = json.dumps({"data": {"privacy_level_options": "not_a_list"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            info = query_creator_info("act.tok")

        assert info.privacy_level_options == ()

    def test_http_error_raises(self) -> None:
        import urllib.error

        exc = urllib.error.HTTPError(
            url="https://x", code=401, msg="Unauth", hdrs=None, fp=BytesIO(b"")  # type: ignore[arg-type]
        )

        with patch("urllib.request.urlopen", side_effect=exc), pytest.raises(UploadError, match="HTTP 401"):
            query_creator_info("act.tok")


# ------------------------------------------------------------------
# init_upload — PULL_FROM_URL and is_aigc
# ------------------------------------------------------------------


class TestInitUploadPullFromUrl:
    _BASE: ClassVar[dict[str, Any]] = {
        "access_token": "act.tok",
        "caption": "Hello",
        "direct_post": False,
        "privacy_level": "SELF_ONLY",
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_cover_timestamp_ms": 1000,
    }

    def test_pull_from_url_source(self) -> None:
        resp = json.dumps({"data": {"publish_id": "pub1"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            result = init_upload(**self._BASE, video_url="https://cdn.example.com/v.mp4")

        assert result.publish_id == "pub1"
        assert result.upload_url == ""
        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["source_info"]["source"] == "PULL_FROM_URL"
        assert body["source_info"]["video_url"] == "https://cdn.example.com/v.mp4"

    def test_pull_from_url_no_upload_url_ok(self) -> None:
        resp = json.dumps({"data": {"publish_id": "pub1"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)):
            result = init_upload(**self._BASE, video_url="https://cdn/v.mp4")

        assert result.publish_id == "pub1"

    def test_is_aigc_flag_included(self) -> None:
        resp = json.dumps({"data": {"publish_id": "p", "upload_url": "https://u"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            init_upload(**self._BASE, video_size=1024, chunk_size=1024, is_aigc=True)

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert body["post_info"]["is_aigc"] is True

    def test_is_aigc_omitted_when_false(self) -> None:
        resp = json.dumps({"data": {"publish_id": "p", "upload_url": "https://u"}}).encode()

        with patch("urllib.request.urlopen", return_value=_mock_urlopen(resp)) as mock_open:
            init_upload(**self._BASE, video_size=1024, chunk_size=1024, is_aigc=False)

        req = mock_open.call_args[0][0]
        body = json.loads(req.data.decode())
        assert "is_aigc" not in body["post_info"]


# ------------------------------------------------------------------
# upload_video_from_url (end-to-end with mocks)
# ------------------------------------------------------------------


class TestUploadVideoFromUrl:
    _DEFAULTS: ClassVar[dict[str, Any]] = {
        "access_token": "act.tok",
        "video_url": "https://cdn.example.com/video.mp4",
        "caption": "Test",
        "direct_post": False,
        "privacy_level": "SELF_ONLY",
        "disable_duet": False,
        "disable_comment": False,
        "disable_stitch": False,
        "brand_content_toggle": False,
        "brand_organic_toggle": False,
        "video_cover_timestamp_ms": 1000,
        "poll_interval": 0,
        "max_attempts": 3,
    }

    def test_full_flow(self) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", ""),
            ),
            patch(
                "reeln_tiktok_plugin.upload.poll_status",
                return_value="PUBLISH_COMPLETE",
            ),
        ):
            result = upload_video_from_url(**self._DEFAULTS)

        assert result == UploadResult(publish_id="pub1", status="PUBLISH_COMPLETE", share_url="")

    def test_no_put_chunks_called(self) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", ""),
            ),
            patch("reeln_tiktok_plugin.upload.put_chunks") as mock_put,
            patch(
                "reeln_tiktok_plugin.upload.poll_status",
                return_value="PUBLISH_COMPLETE",
            ),
        ):
            upload_video_from_url(**self._DEFAULTS)

        mock_put.assert_not_called()

    def test_init_failure_propagates(self) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                side_effect=UploadError("init fail"),
            ),
            pytest.raises(UploadError, match="init fail"),
        ):
            upload_video_from_url(**self._DEFAULTS)

    def test_poll_failure_propagates(self) -> None:
        with (
            patch(
                "reeln_tiktok_plugin.upload.init_upload",
                return_value=InitResult("pub1", ""),
            ),
            patch(
                "reeln_tiktok_plugin.upload.poll_status",
                side_effect=UploadError("poll fail"),
            ),
            pytest.raises(UploadError, match="poll fail"),
        ):
            upload_video_from_url(**self._DEFAULTS)
