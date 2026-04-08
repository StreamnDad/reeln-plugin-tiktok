"""Tests for the TikTokPlugin module."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from reeln.models.auth import AuthStatus
from reeln.plugins.hooks import Hook, HookContext
from reeln.plugins.registry import HookRegistry

from reeln_tiktok_plugin import auth
from reeln_tiktok_plugin.auth import AuthError
from reeln_tiktok_plugin.plugin import TikTokPlugin
from reeln_tiktok_plugin.upload import CreatorInfo, UploadError, UploadResult
from tests.conftest import FakeGameInfo, FakePlan, FakeResult

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_UPLOAD_RESULT = UploadResult(publish_id="pub1", status="SEND_TO_USER_INBOX", share_url="")


def _portrait_context(video_file: Path, game_info: FakeGameInfo | None = None) -> HookContext:
    """Return a POST_RENDER HookContext with a portrait plan."""
    data: dict[str, Any] = {
        "plan": FakePlan(width=1080, height=1920, output=video_file, filter_complex="overlay"),
        "result": FakeResult(output=video_file),
    }
    if game_info is not None:
        data["game_info"] = game_info
    return HookContext(hook=Hook.POST_RENDER, data=data)


def _landscape_context(video_file: Path, game_info: FakeGameInfo | None = None) -> HookContext:
    """Return a POST_RENDER HookContext with a landscape plan."""
    data: dict[str, Any] = {
        "plan": FakePlan(width=1920, height=1080, output=video_file, filter_complex="overlay"),
        "result": FakeResult(output=video_file),
    }
    if game_info is not None:
        data["game_info"] = game_info
    return HookContext(hook=Hook.POST_RENDER, data=data)


# ------------------------------------------------------------------
# Attributes & init
# ------------------------------------------------------------------


class TestTikTokPluginAttributes:
    def test_name(self) -> None:
        assert TikTokPlugin().name == "tiktok"

    def test_version(self) -> None:
        from reeln_tiktok_plugin import __version__

        assert TikTokPlugin().version == __version__

    def test_api_version(self) -> None:
        assert TikTokPlugin().api_version == 1

    def test_config_schema_has_fields(self) -> None:
        names = [f.name for f in TikTokPlugin.config_schema.fields]
        assert "upload_shorts" in names
        assert "upload_videos" in names
        assert "client_key" in names
        assert "privacy_level" in names
        assert "is_aigc" in names

    def test_config_schema_defaults(self) -> None:
        defaults = TikTokPlugin.config_schema.defaults_dict()
        assert defaults["upload_shorts"] is False
        assert defaults["upload_videos"] is False
        assert defaults["direct_post"] is False
        assert defaults["privacy_level"] == "SELF_ONLY"


class TestPluginInit:
    def test_no_config(self) -> None:
        plugin = TikTokPlugin()
        assert plugin._config == {}

    def test_empty_config(self) -> None:
        plugin = TikTokPlugin({})
        assert plugin._config == {}

    def test_with_config(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        assert plugin._config == plugin_config


# ------------------------------------------------------------------
# Registration
# ------------------------------------------------------------------


class TestRegister:
    def test_registers_game_init(self) -> None:
        registry = HookRegistry()
        TikTokPlugin().register(registry)
        assert registry.has_handlers(Hook.ON_GAME_INIT)

    def test_registers_post_render(self) -> None:
        registry = HookRegistry()
        TikTokPlugin().register(registry)
        assert registry.has_handlers(Hook.POST_RENDER)

    def test_registers_game_finish(self) -> None:
        registry = HookRegistry()
        TikTokPlugin().register(registry)
        assert registry.has_handlers(Hook.ON_GAME_FINISH)

    def test_does_not_register_other_hooks(self) -> None:
        registry = HookRegistry()
        TikTokPlugin().register(registry)
        assert not registry.has_handlers(Hook.ON_GAME_READY)


# ------------------------------------------------------------------
# on_game_init
# ------------------------------------------------------------------


class TestOnGameInit:
    def test_caches_game_info(self) -> None:
        plugin = TikTokPlugin()
        gi = FakeGameInfo()
        context = HookContext(hook=Hook.ON_GAME_INIT, data={"game_info": gi})
        plugin.on_game_init(context)
        assert plugin._game_info is gi

    def test_no_game_info_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        plugin = TikTokPlugin()
        context = HookContext(hook=Hook.ON_GAME_INIT, data={})
        with caplog.at_level(logging.WARNING):
            plugin.on_game_init(context)
        assert "no game_info" in caplog.text


# ------------------------------------------------------------------
# on_game_finish
# ------------------------------------------------------------------


class TestOnGameFinish:
    def test_resets_state(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        plugin._access_token = "tok"
        plugin._game_info = FakeGameInfo()
        plugin._creator_info = CreatorInfo("u", "n", (), False, False, False, 0)
        plugin.on_game_finish(HookContext(hook=Hook.ON_GAME_FINISH))
        assert plugin._access_token is None
        assert plugin._game_info is None
        assert plugin._creator_info is None


# ------------------------------------------------------------------
# on_post_render — guards
# ------------------------------------------------------------------


class TestOnPostRenderGuards:
    def test_both_flags_off_returns(self, video_file: Path) -> None:
        plugin = TikTokPlugin({"upload_shorts": False, "upload_videos": False})
        context = _portrait_context(video_file)
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_no_plan_returns(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = HookContext(hook=Hook.POST_RENDER, data={"result": FakeResult()})
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_no_result_returns(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = HookContext(hook=Hook.POST_RENDER, data={"plan": FakePlan()})
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_no_filter_complex_returns(self, plugin_config: dict[str, Any], video_file: Path) -> None:
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(output=video_file, filter_complex=None)
        context = HookContext(
            hook=Hook.POST_RENDER,
            data={"plan": plan, "result": FakeResult(output=video_file)},
        )
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_portrait_with_shorts_off_returns(self, video_file: Path) -> None:
        plugin = TikTokPlugin({"upload_shorts": False, "upload_videos": True})
        context = _portrait_context(video_file)
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_landscape_with_videos_off_returns(self, video_file: Path) -> None:
        plugin = TikTokPlugin({"upload_shorts": True, "upload_videos": False})
        context = _landscape_context(video_file)
        plugin.on_post_render(context)
        assert "uploads" not in context.shared

    def test_output_missing_warns(
        self, plugin_config: dict[str, Any], tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(output=tmp_path / "gone.mp4", filter_complex="overlay")
        result = FakeResult(output=tmp_path / "gone.mp4")
        context = HookContext(hook=Hook.POST_RENDER, data={"plan": plan, "result": result})
        with caplog.at_level(logging.WARNING):
            plugin.on_post_render(context)
        assert "missing or not found" in caplog.text

    def test_output_none_warns(
        self, plugin_config: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(filter_complex="overlay")
        result = FakeResult(output=None)
        context = HookContext(hook=Hook.POST_RENDER, data={"plan": plan, "result": result})
        with caplog.at_level(logging.WARNING):
            plugin.on_post_render(context)
        assert "missing or not found" in caplog.text


# ------------------------------------------------------------------
# on_post_render — auth
# ------------------------------------------------------------------


class TestOnPostRenderAuth:
    def test_no_client_key_warns(
        self, video_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = TikTokPlugin({"upload_shorts": True})
        context = _portrait_context(video_file)
        with caplog.at_level(logging.WARNING):
            plugin.on_post_render(context)
        assert "client_key not configured" in caplog.text

    def test_no_secret_file_warns(
        self, video_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = TikTokPlugin({"upload_shorts": True, "client_key": "ck"})
        context = _portrait_context(video_file)
        with caplog.at_level(logging.WARNING):
            plugin.on_post_render(context)
        assert "client_secret_file not configured" in caplog.text

    def test_auth_error_warns(
        self,
        plugin_config: dict[str, Any],
        video_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.get_access_token",
                side_effect=AuthError("bad token"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            plugin.on_post_render(context)
        assert "authentication failed" in caplog.text

    def test_token_cached_across_calls(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        plugin._access_token = "cached-token"
        context = _portrait_context(video_file)
        with patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT):
            plugin.on_post_render(context)
        assert plugin._access_token == "cached-token"

    def test_default_credentials_path_used(
        self, client_secret_file: Path, credentials_cache: Path, video_file: Path
    ) -> None:
        config: dict[str, Any] = {
            "upload_shorts": True,
            "client_key": "ck",
            "client_secret_file": str(client_secret_file),
        }
        plugin = TikTokPlugin(config)
        context = _portrait_context(video_file)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.get_access_token",
                return_value="tok",
            ) as mock_auth,
            patch(
                "reeln_tiktok_plugin.plugin.upload.upload_video",
                return_value=_UPLOAD_RESULT,
            ),
        ):
            plugin.on_post_render(context)
        # Should have called with default_credentials_path()
        call_kwargs = mock_auth.call_args
        assert "cache_path" in (call_kwargs.kwargs if call_kwargs.kwargs else {}) or len(call_kwargs.args) == 3


# ------------------------------------------------------------------
# on_post_render — dry run
# ------------------------------------------------------------------


class TestOnPostRenderDryRun:
    def test_dry_run_logs_no_upload(
        self,
        plugin_config: dict[str, Any],
        video_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        plugin_config["dry_run"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with caplog.at_level(logging.INFO):
            plugin.on_post_render(context)
        assert "DRY RUN" in caplog.text
        assert "uploads" not in context.shared


# ------------------------------------------------------------------
# on_post_render — success paths
# ------------------------------------------------------------------


class TestOnPostRenderSuccess:
    def test_portrait_upload_writes_shorts(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert context.shared["uploads"]["tiktok"]["shorts"] == [
            {"publish_id": "pub1", "share_url": "", "status": "SEND_TO_USER_INBOX"}
        ]

    def test_landscape_upload_writes_videos(self, video_file: Path, plugin_config: dict[str, Any]) -> None:
        plugin_config["upload_videos"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _landscape_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert context.shared["uploads"]["tiktok"]["videos"] == [
            {"publish_id": "pub1", "share_url": "", "status": "SEND_TO_USER_INBOX"}
        ]

    def test_both_flags_uploads_portrait(self, video_file: Path, plugin_config: dict[str, Any]) -> None:
        plugin_config["upload_videos"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert "shorts" in context.shared["uploads"]["tiktok"]

    def test_both_flags_uploads_landscape(self, video_file: Path, plugin_config: dict[str, Any]) -> None:
        plugin_config["upload_videos"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _landscape_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert "videos" in context.shared["uploads"]["tiktok"]

    def test_multiple_uploads_append(self, plugin_config: dict[str, Any], video_file: Path) -> None:
        plugin = TikTokPlugin(plugin_config)
        result2 = UploadResult(publish_id="pub2", status="PUBLISH_COMPLETE", share_url="https://tiktok/v2")

        context = _portrait_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)

        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=result2,
        ):
            plugin.on_post_render(context)

        assert len(context.shared["uploads"]["tiktok"]["shorts"]) == 2

    def test_direct_post_flag_passed(self, plugin_config: dict[str, Any], video_file: Path) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ) as mock_upload:
            plugin.on_post_render(context)
        assert mock_upload.call_args.kwargs["direct_post"] is True


# ------------------------------------------------------------------
# on_post_render — upload failure
# ------------------------------------------------------------------


class TestOnPostRenderUploadFailure:
    def test_upload_error_warns(
        self,
        plugin_config: dict[str, Any],
        video_file: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.upload.upload_video",
                side_effect=UploadError("fail"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            plugin.on_post_render(context)
        assert "upload failed" in caplog.text
        assert "uploads" not in context.shared


# ------------------------------------------------------------------
# on_post_render — game_info caching from hook data
# ------------------------------------------------------------------


class TestOnPostRenderGameInfoCaching:
    def test_caches_game_info_from_context_data(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        gi = FakeGameInfo()
        context = _portrait_context(video_file, game_info=gi)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert plugin._game_info is gi

    def test_does_not_overwrite_existing_game_info(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        original = FakeGameInfo(home_team="Original")
        plugin._game_info = original

        new_gi = FakeGameInfo(home_team="New")
        context = _portrait_context(video_file, game_info=new_gi)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            plugin.on_post_render(context)
        assert plugin._game_info is original


# ------------------------------------------------------------------
# _resolve_render_metadata
# ------------------------------------------------------------------


class TestResolveRenderMetadata:
    def test_ai_generated_description(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = HookContext(
            hook=Hook.POST_RENDER,
            data={},
            shared={"render_metadata": {"description": "AI caption here"}},
        )
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "AI caption here"

    def test_template(self, plugin_config: dict[str, Any]) -> None:
        plugin_config["caption_template"] = "{home_team} vs {away_team}"
        plugin = TikTokPlugin(plugin_config)
        plugin._game_info = FakeGameInfo()
        context = HookContext(hook=Hook.POST_RENDER, data={})
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "Eagles vs Hawks"

    def test_game_info_fallback(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        plugin._game_info = FakeGameInfo()
        context = HookContext(hook=Hook.POST_RENDER, data={})
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "Eagles vs Hawks - 2026-01-15"

    def test_game_info_with_venue(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        plugin._game_info = FakeGameInfo(venue="Ice Arena")
        context = HookContext(hook=Hook.POST_RENDER, data={})
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "Eagles vs Hawks - 2026-01-15 @ Ice Arena"

    def test_no_metadata_empty_caption(self, plugin_config: dict[str, Any]) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = HookContext(hook=Hook.POST_RENDER, data={})
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == ""

    def test_ai_takes_priority_over_template(self, plugin_config: dict[str, Any]) -> None:
        plugin_config["caption_template"] = "{home_team}"
        plugin = TikTokPlugin(plugin_config)
        plugin._game_info = FakeGameInfo()
        context = HookContext(
            hook=Hook.POST_RENDER,
            data={},
            shared={"render_metadata": {"description": "AI wins"}},
        )
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "AI wins"

    def test_template_takes_priority_over_game_info(self, plugin_config: dict[str, Any]) -> None:
        plugin_config["caption_template"] = "Go {home_team}!"
        plugin = TikTokPlugin(plugin_config)
        plugin._game_info = FakeGameInfo()
        context = HookContext(hook=Hook.POST_RENDER, data={})
        result = plugin._resolve_render_metadata(context)
        assert result["caption"] == "Go Eagles!"


# ------------------------------------------------------------------
# _render_template
# ------------------------------------------------------------------


class TestRenderTemplate:
    def test_all_placeholders(self) -> None:
        plugin = TikTokPlugin()
        plugin._game_info = FakeGameInfo(venue="Ice Arena")
        result = plugin._render_template("{home_team} vs {away_team} - {sport} at {venue}")
        assert result == "Eagles vs Hawks - hockey at Ice Arena"

    def test_missing_key_resolves_empty(self) -> None:
        plugin = TikTokPlugin()
        plugin._game_info = FakeGameInfo()
        result = plugin._render_template("{home_team} {unknown_key}")
        assert result == "Eagles "

    def test_no_game_info(self) -> None:
        plugin = TikTokPlugin()
        result = plugin._render_template("{home_team} vs {away_team}")
        assert result == " vs "


# ------------------------------------------------------------------
# Integration with HookRegistry
# ------------------------------------------------------------------


class TestIntegration:
    def test_full_lifecycle(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        registry = HookRegistry()
        plugin.register(registry)

        # 1. Game init
        gi = FakeGameInfo(home_team="Storm", away_team="Thunder")
        init_ctx = HookContext(hook=Hook.ON_GAME_INIT, data={"game_info": gi})
        registry.emit(Hook.ON_GAME_INIT, init_ctx)
        assert plugin._game_info is gi

        # 2. Post render
        render_ctx = _portrait_context(video_file)
        with patch(
            "reeln_tiktok_plugin.plugin.upload.upload_video",
            return_value=_UPLOAD_RESULT,
        ):
            registry.emit(Hook.POST_RENDER, render_ctx)
        assert "tiktok" in render_ctx.shared.get("uploads", {})

        # 3. Game finish
        finish_ctx = HookContext(hook=Hook.ON_GAME_FINISH)
        registry.emit(Hook.ON_GAME_FINISH, finish_ctx)
        assert plugin._access_token is None
        assert plugin._game_info is None


# ------------------------------------------------------------------
# Creator Info pre-flight
# ------------------------------------------------------------------

_CREATOR = CreatorInfo(
    creator_username="user1",
    creator_nickname="User One",
    privacy_level_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY"),
    comment_disabled=False,
    duet_disabled=False,
    stitch_disabled=False,
    max_video_post_duration_sec=600,
)


class TestCreatorInfoPreflight:
    def test_creator_info_skipped_for_inbox_mode(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info") as mock_ci,
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
        ):
            plugin.on_post_render(context)
        mock_ci.assert_not_called()

    def test_creator_info_called_for_direct_post(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR) as mock_ci,
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
        ):
            plugin.on_post_render(context)
        mock_ci.assert_called_once()

    def test_creator_info_cached(self, plugin_config: dict[str, Any], video_file: Path) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR) as mock_ci,
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
        ):
            plugin.on_post_render(context)
            plugin.on_post_render(context)
        assert mock_ci.call_count == 1

    def test_creator_info_failure_nonfatal(
        self, plugin_config: dict[str, Any], video_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.upload.query_creator_info",
                side_effect=UploadError("fail"),
            ),
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
            caplog.at_level(logging.WARNING),
        ):
            plugin.on_post_render(context)
        assert "creator info query failed" in caplog.text
        assert "tiktok" in context.shared["uploads"]


# ------------------------------------------------------------------
# Privacy validation
# ------------------------------------------------------------------


class TestPrivacyValidation:
    def test_valid_privacy_unchanged(self) -> None:
        plugin = TikTokPlugin()
        result = plugin._validate_privacy("SELF_ONLY", _CREATOR)
        assert result == "SELF_ONLY"

    def test_invalid_privacy_falls_back(self, caplog: pytest.LogCaptureFixture) -> None:
        plugin = TikTokPlugin()
        with caplog.at_level(logging.WARNING):
            result = plugin._validate_privacy("FOLLOWER_OF_CREATOR", _CREATOR)
        assert result == "PUBLIC_TO_EVERYONE"
        assert "not available" in caplog.text

    def test_empty_options_keeps_original(self) -> None:
        empty = CreatorInfo("u", "n", (), False, False, False, 0)
        plugin = TikTokPlugin()
        result = plugin._validate_privacy("SELF_ONLY", empty)
        assert result == "SELF_ONLY"


# ------------------------------------------------------------------
# Duration validation
# ------------------------------------------------------------------


class TestDurationValidation:
    def test_duration_exceeds_max_skips(
        self, plugin_config: dict[str, Any], video_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(width=1080, height=1920, output=video_file, filter_complex="overlay")
        long_result = FakeResult(output=video_file)
        long_result.duration_seconds = 700.0  # type: ignore[attr-defined]
        context = HookContext(
            hook=Hook.POST_RENDER,
            data={"plan": plan, "result": long_result},
        )
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR),
            caplog.at_level(logging.WARNING),
        ):
            plugin.on_post_render(context)
        assert "exceeds creator max" in caplog.text
        assert "uploads" not in context.shared

    def test_duration_within_limit_uploads(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin_config["direct_post"] = True
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(width=1080, height=1920, output=video_file, filter_complex="overlay")
        short_result = FakeResult(output=video_file)
        short_result.duration_seconds = 30.0  # type: ignore[attr-defined]
        context = HookContext(
            hook=Hook.POST_RENDER,
            data={"plan": plan, "result": short_result},
        )
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR),
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
        ):
            plugin.on_post_render(context)
        assert "tiktok" in context.shared["uploads"]

    def test_zero_max_duration_skips_check(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin_config["direct_post"] = True
        zero_dur = CreatorInfo("u", "n", ("SELF_ONLY",), False, False, False, 0)
        plugin = TikTokPlugin(plugin_config)
        plan = FakePlan(width=1080, height=1920, output=video_file, filter_complex="overlay")
        result = FakeResult(output=video_file)
        result.duration_seconds = 9999.0  # type: ignore[attr-defined]
        context = HookContext(hook=Hook.POST_RENDER, data={"plan": plan, "result": result})
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=zero_dur),
            patch("reeln_tiktok_plugin.plugin.upload.upload_video", return_value=_UPLOAD_RESULT),
        ):
            plugin.on_post_render(context)
        assert "tiktok" in context.shared["uploads"]


# ------------------------------------------------------------------
# PULL_FROM_URL (video_url in shared context)
# ------------------------------------------------------------------


class TestPullFromUrl:
    def test_uses_upload_video_from_url_when_video_url_present(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        context.shared["video_url"] = "https://cdn.example.com/video.mp4"
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR),
            patch(
                "reeln_tiktok_plugin.plugin.upload.upload_video_from_url",
                return_value=_UPLOAD_RESULT,
            ) as mock_url,
            patch("reeln_tiktok_plugin.plugin.upload.upload_video") as mock_file,
        ):
            plugin.on_post_render(context)
        mock_url.assert_called_once()
        mock_file.assert_not_called()
        assert mock_url.call_args.kwargs["video_url"] == "https://cdn.example.com/video.mp4"

    def test_falls_back_to_file_upload_without_video_url(
        self, plugin_config: dict[str, Any], video_file: Path
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR),
            patch(
                "reeln_tiktok_plugin.plugin.upload.upload_video",
                return_value=_UPLOAD_RESULT,
            ) as mock_file,
            patch("reeln_tiktok_plugin.plugin.upload.upload_video_from_url") as mock_url,
        ):
            plugin.on_post_render(context)
        mock_file.assert_called_once()
        mock_url.assert_not_called()

    def test_url_upload_error_nonfatal(
        self, plugin_config: dict[str, Any], video_file: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        plugin = TikTokPlugin(plugin_config)
        context = _portrait_context(video_file)
        context.shared["video_url"] = "https://cdn.example.com/video.mp4"
        with (
            patch("reeln_tiktok_plugin.plugin.upload.query_creator_info", return_value=_CREATOR),
            patch(
                "reeln_tiktok_plugin.plugin.upload.upload_video_from_url",
                side_effect=UploadError("url fail"),
            ),
            caplog.at_level(logging.WARNING),
        ):
            plugin.on_post_render(context)
        assert "upload failed" in caplog.text
        assert "uploads" not in context.shared


# ------------------------------------------------------------------
# auth_check
# ------------------------------------------------------------------

_AUTH_CREDS = auth.Credentials(
    access_token="act.test-token-123",
    refresh_token="rft.test-refresh-456",
    expires_at=9999999999.0,
    open_id="test-open-id",
    scope="video.upload,user.info.basic",
)

_AUTH_CREDS_EXPIRED = auth.Credentials(
    access_token="act.expired-token",
    refresh_token="rft.test-refresh-456",
    expires_at=0.0,
    open_id="test-open-id",
    scope="video.upload,user.info.basic",
)

_AUTH_CREATOR = CreatorInfo(
    creator_username="testuser",
    creator_nickname="Test User",
    privacy_level_options=("PUBLIC_TO_EVERYONE", "SELF_ONLY"),
    comment_disabled=False,
    duet_disabled=False,
    stitch_disabled=False,
    max_video_post_duration_sec=600,
)


class TestAuthCheck:
    def test_client_key_not_configured(self) -> None:
        """auth_check returns NOT_CONFIGURED when client_key is missing."""
        plugin = TikTokPlugin({})
        results = plugin.auth_check()
        assert len(results) == 1
        assert results[0].status == AuthStatus.NOT_CONFIGURED
        assert results[0].service == "TikTok"
        assert "client_key" in results[0].message

    def test_client_secret_file_not_configured(self) -> None:
        """auth_check returns NOT_CONFIGURED when client_key set but no secret file."""
        plugin = TikTokPlugin({"client_key": "ck"})
        results = plugin.auth_check()
        assert len(results) == 1
        assert results[0].status == AuthStatus.NOT_CONFIGURED
        assert "client_secret_file" in results[0].message

    def test_load_credentials_fails(self, plugin_config: dict[str, Any]) -> None:
        """auth_check returns FAIL when load_credentials raises AuthError."""
        plugin = TikTokPlugin(plugin_config)
        with patch(
            "reeln_tiktok_plugin.plugin.auth.load_credentials",
            side_effect=AuthError("file not found"),
        ):
            results = plugin.auth_check()
        assert len(results) == 1
        assert results[0].status == AuthStatus.FAIL
        assert "file not found" in results[0].message
        assert "re-authenticate" in results[0].hint

    def test_token_expired_refresh_fails(self, plugin_config: dict[str, Any]) -> None:
        """auth_check returns EXPIRED when token is expired and refresh fails."""
        plugin = TikTokPlugin(plugin_config)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.load_credentials",
                return_value=_AUTH_CREDS_EXPIRED,
            ),
            patch(
                "reeln_tiktok_plugin.plugin.auth.read_client_secret",
                return_value="secret",
            ),
            patch(
                "reeln_tiktok_plugin.plugin.auth.refresh_if_expired",
                side_effect=AuthError("refresh denied"),
            ),
        ):
            results = plugin.auth_check()
        assert len(results) == 1
        assert results[0].status == AuthStatus.EXPIRED
        assert "refresh failed" in results[0].message
        assert "re-authenticate" in results[0].hint

    def test_creator_info_query_fails_warns(self, plugin_config: dict[str, Any]) -> None:
        """auth_check returns WARN when creator info fails (token still valid)."""
        plugin = TikTokPlugin(plugin_config)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.load_credentials",
                return_value=_AUTH_CREDS,
            ),
            patch(
                "reeln_tiktok_plugin.plugin.upload.query_creator_info",
                side_effect=UploadError("scope_not_authorized"),
            ),
        ):
            results = plugin.auth_check()
        assert len(results) == 1
        assert results[0].status == AuthStatus.WARN
        assert "creator info unavailable" in results[0].message
        assert results[0].identity == _AUTH_CREDS.open_id
        assert results[0].scopes
        assert results[0].expires_at

    def test_success(self, plugin_config: dict[str, Any]) -> None:
        """auth_check returns OK with identity, expires_at, and scopes on success."""
        plugin = TikTokPlugin(plugin_config)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.load_credentials",
                return_value=_AUTH_CREDS,
            ),
            patch(
                "reeln_tiktok_plugin.plugin.upload.query_creator_info",
                return_value=_AUTH_CREATOR,
            ),
        ):
            results = plugin.auth_check()
        assert len(results) == 1
        r = results[0]
        assert r.status == AuthStatus.OK
        assert r.service == "TikTok"
        assert r.message == "Authenticated"
        assert r.identity == "@testuser"
        # expires_at is ISO 8601 string
        assert "T" in r.expires_at
        assert r.expires_at.endswith("+00:00")
        # scopes populated from credential scope string
        assert r.scopes == ["video.upload", "user.info.basic"]

    def test_token_not_yet_expired_skips_refresh(self, plugin_config: dict[str, Any]) -> None:
        """auth_check does not call refresh_if_expired when token is still valid."""
        plugin = TikTokPlugin(plugin_config)
        with (
            patch(
                "reeln_tiktok_plugin.plugin.auth.load_credentials",
                return_value=_AUTH_CREDS,
            ),
            patch(
                "reeln_tiktok_plugin.plugin.auth.refresh_if_expired",
            ) as mock_refresh,
            patch(
                "reeln_tiktok_plugin.plugin.upload.query_creator_info",
                return_value=_AUTH_CREATOR,
            ),
        ):
            results = plugin.auth_check()
        mock_refresh.assert_not_called()
        assert results[0].status == AuthStatus.OK


# ------------------------------------------------------------------
# auth_refresh
# ------------------------------------------------------------------


class TestAuthRefresh:
    def test_not_configured_no_client_key(self) -> None:
        """auth_refresh returns NOT_CONFIGURED when client_key is missing."""
        plugin = TikTokPlugin({})
        results = plugin.auth_refresh()
        assert len(results) == 1
        assert results[0].status == AuthStatus.NOT_CONFIGURED
        assert "client_key" in results[0].message

    def test_not_configured_no_secret_file(self) -> None:
        """auth_refresh returns NOT_CONFIGURED when client_secret_file is missing."""
        plugin = TikTokPlugin({"client_key": "ck"})
        results = plugin.auth_refresh()
        assert len(results) == 1
        assert results[0].status == AuthStatus.NOT_CONFIGURED
        assert "client_secret_file" in results[0].message

    def test_login_fails_returns_false(self, plugin_config: dict[str, Any]) -> None:
        """auth_refresh returns FAIL when login.login returns False."""
        plugin = TikTokPlugin(plugin_config)
        with patch("reeln_tiktok_plugin.login.login", return_value=False):
            results = plugin.auth_refresh()
        assert len(results) == 1
        assert results[0].status == AuthStatus.FAIL
        assert "cancelled or failed" in results[0].message

    def test_login_raises_exception(self, plugin_config: dict[str, Any]) -> None:
        """auth_refresh returns FAIL when login.login raises an exception."""
        plugin = TikTokPlugin(plugin_config)
        with patch(
            "reeln_tiktok_plugin.login.login",
            side_effect=RuntimeError("browser failed"),
        ):
            results = plugin.auth_refresh()
        assert len(results) == 1
        assert results[0].status == AuthStatus.FAIL
        assert "browser failed" in results[0].message

    def test_success_runs_auth_check(self, plugin_config: dict[str, Any]) -> None:
        """auth_refresh returns OK after successful login, delegating to auth_check."""
        plugin = TikTokPlugin(plugin_config)
        # Pre-set cached state to confirm it gets cleared
        plugin._access_token = "old-token"
        plugin._creator_info = _AUTH_CREATOR

        with (
            patch("reeln_tiktok_plugin.login.login", return_value=True),
            patch(
                "reeln_tiktok_plugin.plugin.auth.load_credentials",
                return_value=_AUTH_CREDS,
            ),
            patch(
                "reeln_tiktok_plugin.plugin.upload.query_creator_info",
                return_value=_AUTH_CREATOR,
            ),
        ):
            results = plugin.auth_refresh()
        assert len(results) == 1
        r = results[0]
        assert r.status == AuthStatus.OK
        assert r.identity == "@testuser"
        assert "T" in r.expires_at
        assert r.scopes == ["video.upload", "user.info.basic"]
        # Cached state was cleared before auth_check
        assert plugin._access_token is None
        assert plugin._creator_info is None
