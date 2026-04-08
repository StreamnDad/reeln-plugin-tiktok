"""TikTokPlugin — reeln-cli plugin for TikTok video uploads."""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Any

from reeln.models.auth import AuthCheckResult, AuthStatus
from reeln.models.plugin_schema import ConfigField, PluginConfigSchema
from reeln.plugins.hooks import Hook, HookContext
from reeln.plugins.registry import HookRegistry

from reeln_tiktok_plugin import auth, upload

log: logging.Logger = logging.getLogger(__name__)


class TikTokPlugin:
    """Plugin that provides TikTok upload integration for reeln-cli."""

    name: str = "tiktok"
    version: str = "0.3.0"
    api_version: int = 1

    config_schema: PluginConfigSchema = PluginConfigSchema(
        fields=(
            ConfigField(
                name="upload_shorts",
                field_type="bool",
                default=False,
                description="Enable TikTok upload of portrait renders after POST_RENDER",
            ),
            ConfigField(
                name="upload_videos",
                field_type="bool",
                default=False,
                description="Enable TikTok upload of landscape/square renders after POST_RENDER",
            ),
            ConfigField(
                name="direct_post",
                field_type="bool",
                default=False,
                description="If true, publish directly to feed; otherwise send to inbox/drafts",
            ),
            ConfigField(
                name="dry_run",
                field_type="bool",
                default=False,
                description="Log API calls without executing them",
            ),
            ConfigField(
                name="client_key",
                field_type="str",
                required=True,
                description="TikTok app client key",
            ),
            ConfigField(
                name="client_secret_file",
                field_type="str",
                required=True,
                description="Path to file holding the TikTok client secret",
                secret=True,
            ),
            ConfigField(
                name="credentials_cache",
                field_type="str",
                description="OAuth credentials cache path (default: data_dir/tiktok/oauth.json)",
            ),
            ConfigField(
                name="privacy_level",
                field_type="str",
                default="SELF_ONLY",
                description="Privacy level: PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, SELF_ONLY",
            ),
            ConfigField(
                name="disable_duet",
                field_type="bool",
                default=False,
                description="Disable duets on uploaded videos",
            ),
            ConfigField(
                name="disable_comment",
                field_type="bool",
                default=False,
                description="Disable comments on uploaded videos",
            ),
            ConfigField(
                name="disable_stitch",
                field_type="bool",
                default=False,
                description="Disable stitch on uploaded videos",
            ),
            ConfigField(
                name="brand_content_toggle",
                field_type="bool",
                default=False,
                description="Paid partnership disclosure (branded content)",
            ),
            ConfigField(
                name="brand_organic_toggle",
                field_type="bool",
                default=False,
                description="Your own brand disclosure",
            ),
            ConfigField(
                name="is_aigc",
                field_type="bool",
                default=False,
                description="Flag content as AI-generated (required by TikTok for AI content)",
            ),
            ConfigField(
                name="video_cover_timestamp_ms",
                field_type="int",
                default=1000,
                description="Cover frame offset in milliseconds",
            ),
            ConfigField(
                name="caption_template",
                field_type="str",
                default="",
                description="Caption template with placeholders: {home_team}, {away_team}, {date}, {venue}, {sport}",
            ),
            ConfigField(
                name="upload_poll_interval_seconds",
                field_type="int",
                default=5,
                description="Seconds between upload status polls",
            ),
            ConfigField(
                name="upload_poll_max_attempts",
                field_type="int",
                default=60,
                description="Maximum number of status poll attempts (5-minute ceiling at default interval)",
            ),
            ConfigField(
                name="chunk_size_bytes",
                field_type="int",
                default=10485760,
                description="Chunk size in bytes for video upload (default 10 MB)",
            ),
        )
    )

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config: dict[str, Any] = config or {}
        self._access_token: str | None = None
        self._game_info: object | None = None
        self._creator_info: upload.CreatorInfo | None = None

    def register(self, registry: HookRegistry) -> None:
        """Register hook handlers with the reeln plugin registry."""
        registry.register(Hook.ON_GAME_INIT, self.on_game_init)
        registry.register(Hook.POST_RENDER, self.on_post_render)
        registry.register(Hook.ON_GAME_FINISH, self.on_game_finish)

    # ------------------------------------------------------------------
    # Hook handlers
    # ------------------------------------------------------------------

    def on_game_init(self, context: HookContext) -> None:
        """Handle ``ON_GAME_INIT`` — cache game info for template rendering."""
        game_info = context.data.get("game_info")
        if game_info is None:
            log.warning("TikTok plugin: no game_info in context, skipping")
            return
        self._game_info = game_info

    def on_post_render(self, context: HookContext) -> None:
        """Handle ``POST_RENDER`` — upload rendered video to TikTok.

        Detects portrait (Short) vs landscape from plan dimensions and
        checks the appropriate feature flag before uploading.  Uses
        ``PULL_FROM_URL`` when ``context.shared["video_url"]`` is
        available (e.g. from the Cloudflare plugin), otherwise falls
        back to ``FILE_UPLOAD`` with chunked PUT.
        """
        want_shorts = self._config.get("upload_shorts", False)
        want_videos = self._config.get("upload_videos", False)
        if not want_shorts and not want_videos:
            return

        plan = context.data.get("plan")
        result = context.data.get("result")
        if plan is None or result is None:
            return

        if getattr(plan, "filter_complex", None) is None:
            return

        plan_width = getattr(plan, "width", None)
        plan_height = getattr(plan, "height", None)
        is_portrait = (
            plan_width is not None
            and plan_height is not None
            and plan_width < plan_height
        )

        if is_portrait and not want_shorts:
            return
        if not is_portrait and not want_videos:
            return

        output = getattr(result, "output", None)
        if output is None or not Path(output).exists():
            log.warning("TikTok plugin: render output missing or not found, skipping")
            return

        # Cache game_info from hook data if not already set
        if self._game_info is None:
            hook_game_info = context.data.get("game_info")
            if hook_game_info is not None:
                self._game_info = hook_game_info

        access_token = self._ensure_auth()
        if access_token is None:
            return

        # Pre-flight: query creator info for validation (direct_post only —
        # requires video.publish scope which inbox/drafts mode doesn't have)
        privacy_level = self._config.get("privacy_level", "SELF_ONLY")
        if self._config.get("direct_post", False):
            creator = self._ensure_creator_info(access_token)
            if creator is not None:
                privacy_level = self._validate_privacy(privacy_level, creator)
                duration = getattr(result, "duration_seconds", None)
                if (
                    duration is not None
                    and creator.max_video_post_duration_sec > 0
                    and float(duration) > creator.max_video_post_duration_sec
                ):
                    log.warning(
                        "TikTok plugin: video duration %.1fs exceeds creator max %ds, skipping",
                        float(duration),
                        creator.max_video_post_duration_sec,
                    )
                    return

        metadata = self._resolve_render_metadata(context)
        caption = metadata["caption"]

        if self._config.get("dry_run"):
            log.info(
                "TikTok plugin: [DRY RUN] would upload video — "
                "file=%s, caption=%r, direct_post=%s, privacy=%s",
                output,
                caption,
                self._config.get("direct_post", False),
                privacy_level,
            )
            return

        video_url = context.shared.get("video_url", "")
        common_kwargs: dict[str, Any] = {
            "access_token": access_token,
            "caption": caption,
            "direct_post": self._config.get("direct_post", False),
            "privacy_level": privacy_level,
            "disable_duet": self._config.get("disable_duet", False),
            "disable_comment": self._config.get("disable_comment", False),
            "disable_stitch": self._config.get("disable_stitch", False),
            "brand_content_toggle": self._config.get("brand_content_toggle", False),
            "brand_organic_toggle": self._config.get("brand_organic_toggle", False),
            "video_cover_timestamp_ms": self._config.get("video_cover_timestamp_ms", 1000),
            "is_aigc": self._config.get("is_aigc", False),
            "poll_interval": float(self._config.get("upload_poll_interval_seconds", 5)),
            "max_attempts": self._config.get("upload_poll_max_attempts", 60),
        }

        try:
            if video_url:
                upload_result = upload.upload_video_from_url(
                    video_url=video_url,
                    **common_kwargs,
                )
            else:
                upload_result = upload.upload_video(
                    file_path=Path(output),
                    chunk_size_bytes=self._config.get("chunk_size_bytes", 10485760),
                    **common_kwargs,
                )
        except upload.UploadError as exc:
            log.warning("TikTok plugin: upload failed: %s", exc)
            return

        upload_key = "shorts" if is_portrait else "videos"
        context.shared["uploads"] = context.shared.get("uploads", {})
        tiktok = context.shared["uploads"].setdefault("tiktok", {})
        tiktok.setdefault(upload_key, []).append(
            {
                "publish_id": upload_result.publish_id,
                "share_url": upload_result.share_url,
                "status": upload_result.status,
            }
        )
        log.info(
            "TikTok plugin: uploaded %s publish_id=%s status=%s",
            upload_key.rstrip("s"),
            upload_result.publish_id,
            upload_result.status,
        )

    def on_game_finish(self, context: HookContext) -> None:
        """Handle ``ON_GAME_FINISH`` — reset cached state."""
        self._access_token = None
        self._game_info = None
        self._creator_info = None

    # ------------------------------------------------------------------
    # Auth check / refresh
    # ------------------------------------------------------------------

    def auth_check(self) -> list[AuthCheckResult]:
        """Test TikTok authentication and return check results."""
        client_key = self._config.get("client_key")
        if not client_key:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.NOT_CONFIGURED,
                    message="client_key not configured",
                    hint="Set client_key in plugin config",
                )
            ]

        secret_file = self._config.get("client_secret_file")
        if not secret_file:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.NOT_CONFIGURED,
                    message="client_secret_file not configured",
                    hint="Set client_secret_file in plugin config",
                )
            ]

        cache_str = self._config.get("credentials_cache")
        cache_path = Path(cache_str) if cache_str else auth.default_credentials_path()

        try:
            creds = auth.load_credentials(cache_path)
        except auth.AuthError as exc:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.FAIL,
                    message=str(exc),
                    hint="Run 'reeln plugins auth --refresh tiktok' to re-authenticate",
                )
            ]

        # Refresh if token is expired or within 60s of expiry
        if creds.expires_at - time.time() <= 60:
            try:
                client_secret = auth.read_client_secret(Path(secret_file))
                creds = auth.refresh_if_expired(
                    creds, client_key, client_secret, cache_path
                )
            except auth.AuthError as exc:
                return [
                    AuthCheckResult(
                        service="TikTok",
                        status=AuthStatus.EXPIRED,
                        message=f"Token expired and refresh failed: {exc}",
                        hint="Run 'reeln plugins auth --refresh tiktok' to re-authenticate",
                    )
                ]

        expires_at_str = datetime.datetime.fromtimestamp(
            creds.expires_at, tz=datetime.UTC
        ).isoformat()
        scopes = [s.strip() for s in creds.scope.split(",") if s.strip()]

        # Validate token against TikTok API and get creator identity
        try:
            creator_info = upload.query_creator_info(creds.access_token)
        except upload.UploadError as exc:
            # Token is valid (loaded + not expired) but creator info query
            # failed — likely a missing scope (e.g. video.publish).  Report
            # as OK with a warning rather than a hard failure.
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.WARN,
                    message=f"Token valid but creator info unavailable: {exc}",
                    identity=creds.open_id,
                    expires_at=expires_at_str,
                    scopes=scopes,
                    hint="Creator info requires video.publish scope",
                )
            ]

        return [
            AuthCheckResult(
                service="TikTok",
                status=AuthStatus.OK,
                message="Authenticated",
                identity=f"@{creator_info.creator_username}",
                expires_at=expires_at_str,
                scopes=scopes,
            )
        ]

    def auth_refresh(self) -> list[AuthCheckResult]:
        """Clear cached credentials and re-authenticate via browser OAuth flow."""
        client_key = self._config.get("client_key")
        if not client_key:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.NOT_CONFIGURED,
                    message="client_key not configured",
                    hint="Set client_key in plugin config",
                )
            ]

        secret_file = self._config.get("client_secret_file")
        if not secret_file:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.NOT_CONFIGURED,
                    message="client_secret_file not configured",
                    hint="Set client_secret_file in plugin config",
                )
            ]

        cache_str = self._config.get("credentials_cache")
        cache_path = Path(cache_str) if cache_str else auth.default_credentials_path()

        from reeln_tiktok_plugin import login

        try:
            success = login.login(
                client_key=client_key,
                client_secret_file=Path(secret_file),
                cache_path=cache_path,
            )
        except Exception as exc:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.FAIL,
                    message=f"Login failed: {exc}",
                )
            ]

        if not success:
            return [
                AuthCheckResult(
                    service="TikTok",
                    status=AuthStatus.FAIL,
                    message="Login was cancelled or failed",
                )
            ]

        # Reset cached token so auth_check picks up fresh credentials
        self._access_token = None
        self._creator_info = None

        return self.auth_check()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_auth(self) -> str | None:
        """Return a cached access token, or authenticate and cache it."""
        if self._access_token is not None:
            return self._access_token

        client_key = self._config.get("client_key")
        if not client_key:
            log.warning("TikTok plugin: client_key not configured, skipping")
            return None

        secret_file_str = self._config.get("client_secret_file")
        if not secret_file_str:
            log.warning("TikTok plugin: client_secret_file not configured, skipping")
            return None

        cache_str = self._config.get("credentials_cache")
        cache_path = Path(cache_str) if cache_str else auth.default_credentials_path()

        try:
            self._access_token = auth.get_access_token(
                client_key=client_key,
                client_secret_file=Path(secret_file_str),
                cache_path=cache_path,
            )
        except auth.AuthError as exc:
            log.warning("TikTok plugin: authentication failed: %s", exc)
            return None

        return self._access_token

    def _ensure_creator_info(self, access_token: str) -> upload.CreatorInfo | None:
        """Return cached creator info, or query and cache it.

        Failures are non-fatal — returns ``None`` and logs a warning.
        """
        if self._creator_info is not None:
            return self._creator_info

        try:
            self._creator_info = upload.query_creator_info(access_token)
        except upload.UploadError as exc:
            log.warning("TikTok plugin: creator info query failed (non-fatal): %s", exc)
            return None

        return self._creator_info

    def _validate_privacy(self, privacy_level: str, creator: upload.CreatorInfo) -> str:
        """Validate the configured privacy level against creator options.

        Falls back to the first available option if the configured level
        is not supported by the creator's account.
        """
        if privacy_level in creator.privacy_level_options:
            return privacy_level

        if not creator.privacy_level_options:
            return privacy_level

        fallback = creator.privacy_level_options[0]
        log.warning(
            "TikTok plugin: privacy_level %r not available for creator (options: %s), using %r",
            privacy_level,
            ", ".join(creator.privacy_level_options),
            fallback,
        )
        return fallback

    def _resolve_render_metadata(self, context: HookContext) -> dict[str, str]:
        """Build caption from shared context, template, or game info fallback.

        Priority:
            1. ``context.shared["render_metadata"]["description"]`` (AI-generated)
            2. ``caption_template`` rendered with game info placeholders
            3. Fallback title from game info
        """
        render_meta = context.shared.get("render_metadata", {})
        description = str(render_meta.get("description", ""))
        if description:
            return {"caption": description}

        template = self._config.get("caption_template", "")
        if template:
            return {"caption": self._render_template(template)}

        if self._game_info is not None:
            return {"caption": self._build_title(self._game_info)}

        return {"caption": ""}

    def _build_title(self, game_info: object) -> str:
        """Build a default title from game info fields."""
        home_team = getattr(game_info, "home_team", "")
        away_team = getattr(game_info, "away_team", "")
        date = getattr(game_info, "date", "")
        venue = getattr(game_info, "venue", "")

        title = f"{home_team} vs {away_team} - {date}"
        if venue:
            title += f" @ {venue}"
        return title

    def _render_template(self, template: str) -> str:
        """Render a caption template with game info placeholders.

        Missing keys resolve to empty strings instead of raising
        ``KeyError``.
        """
        game_info = self._game_info
        values: dict[str, str] = {
            "home_team": getattr(game_info, "home_team", "") if game_info else "",
            "away_team": getattr(game_info, "away_team", "") if game_info else "",
            "date": getattr(game_info, "date", "") if game_info else "",
            "venue": getattr(game_info, "venue", "") if game_info else "",
            "sport": getattr(game_info, "sport", "") if game_info else "",
        }

        class SafeDict(dict[str, str]):
            def __missing__(self, key: str) -> str:
                return ""

        return template.format_map(SafeDict(values))
