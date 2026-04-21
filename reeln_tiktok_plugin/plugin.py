"""TikTokPlugin — reeln-cli plugin for TikTok video uploads."""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Any

from reeln.models.auth import AuthCheckResult, AuthStatus
from reeln.models.plugin_schema import ConfigField, PluginConfigSchema
from reeln.plugins.capabilities import UploaderSkipped
from reeln.plugins.hooks import Hook, HookContext
from reeln.plugins.registry import HookRegistry

from reeln_tiktok_plugin import auth, upload
from reeln_tiktok_plugin.upload import CreatorInfo

log: logging.Logger = logging.getLogger(__name__)


class TikTokPlugin:
    """Plugin that provides TikTok upload integration for reeln-cli."""

    name: str = "tiktok"
    version: str = "0.4.0"
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
        self._creator_info: CreatorInfo | None = None
        # Most recent upload result, captured by upload() for
        # on_post_render to populate context.shared["uploads"].
        self._last_upload_result: upload.UploadResult | None = None

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
        if context.data.get("regenerate_image_only", False):
            return

        game_info = context.data.get("game_info")
        if game_info is None:
            log.warning("TikTok plugin: no game_info in context, skipping")
            return
        self._game_info = game_info

    def upload(
        self, path: Path, *, metadata: dict[str, Any] | None = None
    ) -> str:
        """Upload a rendered video to TikTok and return its share URL.

        Implements the :class:`reeln.plugins.capabilities.Uploader` protocol
        so the plugin can be used by ``reeln queue publish`` for truthful
        per-target status reporting.

        Detects portrait (Short) vs landscape from ``metadata["format"]``
        (e.g. ``"1080x1920"``) and gates on ``upload_shorts`` or
        ``upload_videos`` accordingly. Uses the TikTok ``PULL_FROM_URL``
        upload flow when ``metadata["video_url"]`` is populated (by
        cloudflare), otherwise falls back to chunked ``FILE_UPLOAD``.

        Raises:
            UploaderSkipped: when the relevant feature flag is disabled,
                or the video duration exceeds the creator's max (direct_post).
            FileNotFoundError: when the source file does not exist.
            RuntimeError: when authentication fails.
            reeln_tiktok_plugin.upload.UploadError: on upload failure.
        """
        meta = metadata or {}

        is_portrait = self._is_portrait_from_metadata(meta)
        want_shorts = self._config.get("upload_shorts", False)
        want_videos = self._config.get("upload_videos", False)
        if is_portrait and not want_shorts:
            raise UploaderSkipped(
                "upload_shorts disabled in tiktok plugin config"
            )
        if not is_portrait and not want_videos:
            raise UploaderSkipped(
                "upload_videos disabled in tiktok plugin config"
            )

        if not path.exists():
            raise FileNotFoundError(f"TikTok upload source not found: {path}")

        access_token = self._ensure_auth()
        if access_token is None:
            raise RuntimeError(
                "TikTok plugin: authentication failed "
                "(check client_key/client_secret and OAuth credentials)"
            )

        # Pre-flight creator info check for direct_post (video.publish scope).
        privacy_level = self._config.get("privacy_level", "SELF_ONLY")
        if self._config.get("direct_post", False):
            creator = self._ensure_creator_info(access_token)
            if creator is not None:
                privacy_level = self._validate_privacy(privacy_level, creator)
                duration = meta.get("duration_seconds")
                if (
                    duration is not None
                    and creator.max_video_post_duration_sec > 0
                    and float(duration) > creator.max_video_post_duration_sec
                ):
                    raise UploaderSkipped(
                        f"video duration {float(duration):.1f}s exceeds "
                        f"tiktok creator max {creator.max_video_post_duration_sec}s"
                    )

        caption = self._build_caption_from_metadata(meta)
        video_url = str(meta.get("video_url", ""))

        if self._config.get("dry_run"):
            log.info(
                "TikTok plugin: [DRY RUN] would upload video — "
                "file=%s, caption=%r, direct_post=%s, privacy=%s",
                path,
                caption,
                self._config.get("direct_post", False),
                privacy_level,
            )
            return "tiktok:dry_run"

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

        if video_url:
            upload_result = upload.upload_video_from_url(
                video_url=video_url,
                **common_kwargs,
            )
        else:
            upload_result = upload.upload_video(
                file_path=path,
                chunk_size_bytes=self._config.get("chunk_size_bytes", 10485760),
                **common_kwargs,
            )

        self._last_upload_result = upload_result
        log.info(
            "TikTok plugin: uploaded publish_id=%s status=%s",
            upload_result.publish_id,
            upload_result.status,
        )
        return upload_result.share_url or f"tiktok:{upload_result.publish_id}"

    @staticmethod
    def _is_portrait_from_metadata(metadata: dict[str, Any]) -> bool:
        """Detect portrait orientation from a ``"WxH"`` format string."""
        fmt = metadata.get("format", "")
        if not isinstance(fmt, str) or "x" not in fmt:
            return False
        try:
            width_str, height_str = fmt.split("x", 1)
            return int(width_str) < int(height_str)
        except (ValueError, TypeError):
            return False

    def _build_caption_from_metadata(self, metadata: dict[str, Any]) -> str:
        """Build the TikTok caption from metadata/template/game_info.

        Mirrors :meth:`_resolve_render_metadata` but reads the description
        directly from the metadata dict instead of ``context.shared``.
        """
        description = str(metadata.get("description", ""))
        if description:
            return description

        template = self._config.get("caption_template", "")
        if template:
            # Hydrate game_info from metadata for template rendering if needed.
            self._hydrate_game_info_from_metadata(metadata)
            return self._render_template(template)

        if self._game_info is not None:
            return self._build_title(self._game_info)

        # Last resort: hydrate from metadata and build a title.
        self._hydrate_game_info_from_metadata(metadata)
        if self._game_info is not None:
            return self._build_title(self._game_info)
        return ""

    def _hydrate_game_info_from_metadata(
        self, metadata: dict[str, Any]
    ) -> None:
        """Populate ``self._game_info`` from a publish metadata dict.

        The manual publish path instantiates plugins fresh, so
        ``_game_info`` is None and template rendering would otherwise
        produce empty strings. This builds a minimal stand-in with the
        attributes templates and titles actually use.
        """
        if self._game_info is not None:
            return
        if not metadata:
            return

        class _MetaGameInfo:
            def __init__(self, **kwargs: Any) -> None:
                for key, value in kwargs.items():
                    setattr(self, key, value)

        self._game_info = _MetaGameInfo(
            home_team=str(metadata.get("home_team", "")),
            away_team=str(metadata.get("away_team", "")),
            date=str(metadata.get("date", "")),
            venue="",
            sport=str(metadata.get("sport", "")),
        )

    def on_post_render(self, context: HookContext) -> None:
        """Handle ``POST_RENDER`` — delegate to :meth:`upload`.

        Preserves the auto-publish-during-render contract: exceptions
        are swallowed (a TikTok failure must never break the render
        pipeline) and the upload record is written into
        ``context.shared["uploads"]["tiktok"]`` on success.
        """
        plan = context.data.get("plan")
        result = context.data.get("result")
        if plan is None or result is None:
            return

        if getattr(plan, "filter_complex", None) is None:
            return

        output = getattr(result, "output", None)
        if output is None or not Path(output).exists():
            log.warning(
                "TikTok plugin: render output missing or not found, skipping"
            )
            return

        # Cache game_info from hook data if not already set.
        if self._game_info is None:
            hook_game_info = context.data.get("game_info")
            if hook_game_info is not None:
                self._game_info = hook_game_info

        # Build metadata dict from hook data for the delegated call.
        plan_width = getattr(plan, "width", None)
        plan_height = getattr(plan, "height", None)
        format_str = (
            f"{plan_width}x{plan_height}"
            if plan_width is not None and plan_height is not None
            else ""
        )
        render_meta = self._resolve_render_metadata(context)
        metadata: dict[str, Any] = {
            "description": render_meta.get("caption", ""),
        }
        if format_str:
            metadata["format"] = format_str
        duration = getattr(result, "duration_seconds", None)
        if duration is not None:
            metadata["duration_seconds"] = duration
        video_url = context.shared.get("video_url", "")
        if video_url:
            metadata["video_url"] = str(video_url)

        try:
            self.upload(Path(output), metadata=metadata)
        except UploaderSkipped as exc:
            log.info("TikTok plugin: %s", exc)
            return
        except Exception as exc:
            log.warning("TikTok plugin: upload failed (non-fatal): %s", exc)
            return

        # Persist upload record into shared context for downstream plugins
        # (matches legacy behavior).
        upload_result = getattr(self, "_last_upload_result", None)
        if upload_result is None:
            return
        is_portrait = (
            plan_width is not None
            and plan_height is not None
            and plan_width < plan_height
        )
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

    def _ensure_creator_info(self, access_token: str) -> CreatorInfo | None:
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

    def _validate_privacy(self, privacy_level: str, creator: CreatorInfo) -> str:
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
