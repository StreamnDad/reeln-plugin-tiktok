"""TikTok Content Posting API — init, chunked upload, status polling, and creator info."""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log: logging.Logger = logging.getLogger(__name__)

BASE_URL = "https://open.tiktokapis.com"


class UploadError(Exception):
    """Raised when a TikTok upload operation fails."""


@dataclass(frozen=True)
class InitResult:
    """Result of the upload init call."""

    publish_id: str
    upload_url: str


@dataclass(frozen=True)
class UploadResult:
    """Final result of a completed upload."""

    publish_id: str
    status: str
    share_url: str


@dataclass(frozen=True)
class CreatorInfo:
    """Creator account info returned by the creator info query endpoint."""

    creator_username: str
    creator_nickname: str
    privacy_level_options: tuple[str, ...]
    comment_disabled: bool
    duet_disabled: bool
    stitch_disabled: bool
    max_video_post_duration_sec: int


def format_tiktok_error(details: str) -> str:
    """Parse a TikTok API error response into a user-friendly message.

    Args:
        details: Raw response body string.

    Returns:
        A formatted error message.
    """
    if not details:
        return "(empty response)"

    try:
        data = json.loads(details)
    except json.JSONDecodeError:
        return details[:200]

    if not isinstance(data, dict):
        return details[:200]

    error = data.get("error")
    if isinstance(error, dict):
        code = error.get("code", "")
        message = error.get("message", "")
        return f"{code}: {message}" if code else str(message)

    return details[:200]


def _json_post(
    url: str,
    *,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int = 60,
) -> dict[str, Any]:
    """Send a JSON POST and return parsed JSON response.

    Raises:
        UploadError: On HTTP or parsing errors.
    """
    encoded = json.dumps(body).encode()
    merged_headers = {**headers, "Content-Type": "application/json; charset=UTF-8"}
    request = urllib.request.Request(url, data=encoded, headers=merged_headers, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode() if exc.fp else ""
        detail = format_tiktok_error(error_body)
        raise UploadError(f"HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise UploadError(f"Request failed: {exc.reason}") from exc

    try:
        return json.loads(response_body)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise UploadError(f"Invalid JSON response: {response_body[:200]}") from exc


# ------------------------------------------------------------------
# Creator Info
# ------------------------------------------------------------------


def query_creator_info(access_token: str) -> CreatorInfo:
    """Query the authenticated creator's account info.

    Returns privacy level options, toggle states, and max video duration.
    Should be called before upload to validate settings.

    Raises:
        UploadError: On HTTP errors or malformed response.
    """
    url = f"{BASE_URL}/v2/post/publish/creator_info/query/"
    data = _json_post(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        body={},
    )

    inner = data.get("data", {})
    raw_options = inner.get("privacy_level_options", [])
    options = tuple(str(o) for o in raw_options) if isinstance(raw_options, list) else ()

    return CreatorInfo(
        creator_username=str(inner.get("creator_username", "")),
        creator_nickname=str(inner.get("creator_nickname", "")),
        privacy_level_options=options,
        comment_disabled=bool(inner.get("comment_disabled", False)),
        duet_disabled=bool(inner.get("duet_disabled", False)),
        stitch_disabled=bool(inner.get("stitch_disabled", False)),
        max_video_post_duration_sec=int(inner.get("max_video_post_duration_sec", 0)),
    )


# ------------------------------------------------------------------
# Upload init
# ------------------------------------------------------------------


def init_upload(
    *,
    access_token: str,
    caption: str,
    direct_post: bool,
    privacy_level: str,
    disable_duet: bool,
    disable_comment: bool,
    disable_stitch: bool,
    brand_content_toggle: bool,
    brand_organic_toggle: bool,
    video_cover_timestamp_ms: int,
    is_aigc: bool = False,
    video_size: int = 0,
    chunk_size: int = 0,
    video_url: str = "",
) -> InitResult:
    """Initialise a TikTok upload session.

    When *video_url* is provided, uses ``PULL_FROM_URL`` source (TikTok
    fetches the video from the given URL).  Otherwise uses ``FILE_UPLOAD``
    with *video_size* and *chunk_size*.

    Uses the inbox endpoint by default or the direct-post endpoint when
    *direct_post* is ``True``.

    Returns:
        An :class:`InitResult` with ``publish_id`` and ``upload_url``
        (upload_url is empty for ``PULL_FROM_URL``).

    Raises:
        UploadError: On HTTP errors or missing fields in response.
    """
    path = "/v2/post/publish/video/init/" if direct_post else "/v2/post/publish/inbox/video/init/"
    url = f"{BASE_URL}{path}"

    post_info: dict[str, Any] = {
        "title": caption,
        "privacy_level": privacy_level,
        "disable_duet": disable_duet,
        "disable_comment": disable_comment,
        "disable_stitch": disable_stitch,
        "brand_content_toggle": brand_content_toggle,
        "brand_organic_toggle": brand_organic_toggle,
        "video_cover_timestamp_ms": video_cover_timestamp_ms,
    }
    if is_aigc:
        post_info["is_aigc"] = True

    if video_url:
        source_info: dict[str, Any] = {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        }
    else:
        total_chunks = max(1, math.ceil(video_size / chunk_size))
        source_info = {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        }

    body: dict[str, Any] = {
        "post_info": post_info,
        "source_info": source_info,
    }

    data = _json_post(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        body=body,
    )

    inner = data.get("data", {})
    publish_id = inner.get("publish_id", "")
    upload_url_resp = inner.get("upload_url", "")

    if not publish_id:
        raise UploadError(f"Init response missing publish_id: {data}")

    if not video_url and not upload_url_resp:
        raise UploadError(f"Init response missing upload_url for FILE_UPLOAD: {data}")

    return InitResult(publish_id=str(publish_id), upload_url=str(upload_url_resp))


# ------------------------------------------------------------------
# Chunked PUT
# ------------------------------------------------------------------


def put_chunks(
    upload_url: str,
    file_path: Path,
    chunk_size: int,
) -> None:
    """Upload the video file in chunks via PUT requests.

    Each chunk is sent with a ``Content-Range`` header. Single-chunk
    uploads are supported (when file size <= chunk_size).

    Raises:
        UploadError: On HTTP errors during chunk upload.
    """
    total = file_path.stat().st_size

    with file_path.open("rb") as fh:
        offset = 0
        while offset < total:
            chunk = fh.read(chunk_size)
            end = offset + len(chunk) - 1

            headers = {
                "Content-Range": f"bytes {offset}-{end}/{total}",
                "Content-Type": "video/mp4",
            }
            request = urllib.request.Request(
                upload_url,
                data=chunk,
                headers=headers,
                method="PUT",
            )

            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    response.read()
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode() if exc.fp else ""
                raise UploadError(
                    f"Chunk upload failed at offset {offset}: HTTP {exc.code}: {error_body[:200]}"
                ) from exc
            except urllib.error.URLError as exc:
                raise UploadError(f"Chunk upload failed at offset {offset}: {exc.reason}") from exc

            offset += len(chunk)


# ------------------------------------------------------------------
# Status polling
# ------------------------------------------------------------------


def poll_status(
    publish_id: str,
    access_token: str,
    *,
    poll_interval: float = 5.0,
    max_attempts: int = 60,
) -> str:
    """Poll the upload status until a terminal state is reached.

    Returns:
        The final status string (e.g. ``"PUBLISH_COMPLETE"`` or
        ``"SEND_TO_USER_INBOX"``).

    Raises:
        UploadError: If the status is ``FAILED`` or polling times out.
    """
    url = f"{BASE_URL}/v2/post/publish/status/fetch/"
    headers = {"Authorization": f"Bearer {access_token}"}

    terminal_statuses = {"PUBLISH_COMPLETE", "SEND_TO_USER_INBOX", "FAILED"}

    for attempt in range(max_attempts):
        data = _json_post(url, headers=headers, body={"publish_id": publish_id})

        status: str = data.get("data", {}).get("status", "")
        log.info(
            "TikTok upload poll %d/%d: publish_id=%s status=%s",
            attempt + 1,
            max_attempts,
            publish_id,
            status,
        )

        if status == "FAILED":
            fail_reason = data.get("data", {}).get("fail_reason", "unknown")
            raise UploadError(f"Upload failed: {fail_reason}")

        if status in terminal_statuses:
            return status

        if attempt < max_attempts - 1:
            time.sleep(poll_interval)

    raise UploadError(f"Upload not complete after {max_attempts} poll attempts")


# ------------------------------------------------------------------
# High-level upload functions
# ------------------------------------------------------------------


def upload_video(
    *,
    access_token: str,
    file_path: Path,
    caption: str,
    direct_post: bool,
    privacy_level: str,
    disable_duet: bool,
    disable_comment: bool,
    disable_stitch: bool,
    brand_content_toggle: bool,
    brand_organic_toggle: bool,
    video_cover_timestamp_ms: int,
    is_aigc: bool = False,
    chunk_size_bytes: int = 10 * 1024 * 1024,
    poll_interval: float = 5.0,
    max_attempts: int = 60,
) -> UploadResult:
    """Upload a video to TikTok via ``FILE_UPLOAD`` end-to-end.

    1. Initialise the upload session (inbox or direct post).
    2. Upload the file in chunks.
    3. Poll for completion.

    Returns:
        An :class:`UploadResult` with ``publish_id``, ``status``, and
        ``share_url``.

    Raises:
        UploadError: If the file is missing or any API step fails.
    """
    if not file_path.exists():
        raise UploadError(f"File not found: {file_path}")

    video_size = file_path.stat().st_size

    init = init_upload(
        access_token=access_token,
        video_size=video_size,
        chunk_size=chunk_size_bytes,
        caption=caption,
        direct_post=direct_post,
        privacy_level=privacy_level,
        disable_duet=disable_duet,
        disable_comment=disable_comment,
        disable_stitch=disable_stitch,
        brand_content_toggle=brand_content_toggle,
        brand_organic_toggle=brand_organic_toggle,
        video_cover_timestamp_ms=video_cover_timestamp_ms,
        is_aigc=is_aigc,
    )

    put_chunks(init.upload_url, file_path, chunk_size_bytes)

    status = poll_status(
        init.publish_id,
        access_token,
        poll_interval=poll_interval,
        max_attempts=max_attempts,
    )

    return UploadResult(
        publish_id=init.publish_id,
        status=status,
        share_url="",
    )


def upload_video_from_url(
    *,
    access_token: str,
    video_url: str,
    caption: str,
    direct_post: bool,
    privacy_level: str,
    disable_duet: bool,
    disable_comment: bool,
    disable_stitch: bool,
    brand_content_toggle: bool,
    brand_organic_toggle: bool,
    video_cover_timestamp_ms: int,
    is_aigc: bool = False,
    poll_interval: float = 5.0,
    max_attempts: int = 60,
) -> UploadResult:
    """Upload a video to TikTok via ``PULL_FROM_URL`` end-to-end.

    TikTok's servers fetch the video from *video_url* (must be publicly
    accessible).  No chunked PUT is needed.

    1. Initialise the upload session with ``PULL_FROM_URL`` source.
    2. Poll for completion.

    Returns:
        An :class:`UploadResult` with ``publish_id``, ``status``, and
        ``share_url``.

    Raises:
        UploadError: If any API step fails.
    """
    init = init_upload(
        access_token=access_token,
        video_url=video_url,
        caption=caption,
        direct_post=direct_post,
        privacy_level=privacy_level,
        disable_duet=disable_duet,
        disable_comment=disable_comment,
        disable_stitch=disable_stitch,
        brand_content_toggle=brand_content_toggle,
        brand_organic_toggle=brand_organic_toggle,
        video_cover_timestamp_ms=video_cover_timestamp_ms,
        is_aigc=is_aigc,
    )

    status = poll_status(
        init.publish_id,
        access_token,
        poll_interval=poll_interval,
        max_attempts=max_attempts,
    )

    return UploadResult(
        publish_id=init.publish_id,
        status=status,
        share_url="",
    )
