# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.3.0] - 2026-04-07

### Added

- `auth_check()` and `auth_refresh()` methods implementing the `Authenticator` protocol — enables `reeln plugins auth tiktok` for credential verification and OAuth token renewal

## [0.2.0] - 2026-04-05

### Added

- `is_aigc` config flag for AI-generated content disclosure (TikTok compliance)
- Creator Info pre-flight query (`/v2/post/publish/creator_info/query/`) — validates privacy level and max video duration before upload
- `PULL_FROM_URL` upload source — when `context.shared["video_url"]` is set (e.g. from Cloudflare plugin), TikTok fetches from CDN instead of chunked PUT
- `upload_video_from_url()` function for CDN-based uploads
- `query_creator_info()` function returning `CreatorInfo` dataclass
- Privacy level auto-fallback when configured level not available for creator

## [0.1.0] - 2026-04-05

### Added

- `TikTokPlugin` class with `POST_RENDER`, `ON_GAME_INIT`, and `ON_GAME_FINISH` hooks
- `upload_shorts` and `upload_videos` feature flags for portrait/landscape render uploads
- `direct_post` flag to choose between inbox/drafts (default) and direct-post endpoints
- `dry_run` mode for logging API calls without executing
- `auth.py` — OAuth2 token loading and refresh via TikTok's `/v2/oauth/token/` endpoint
- `upload.py` — TikTok Content Posting API v2: init → chunked PUT → status polling
- Configurable privacy level, duet/comment/stitch toggles, brand content disclosure
- Caption resolution: AI-generated (`render_metadata`) → template → game info fallback
- Shared context writes to `context.shared["uploads"]["tiktok"]["shorts"|"videos"]`
- 100% line + branch test coverage
