# reeln-plugin-tiktok

A [reeln-cli](https://github.com/StreamnDad/reeln-cli) plugin for uploading rendered videos to TikTok.

## Install

```bash
pip install reeln-plugin-tiktok
```

Or for development:

```bash
git clone https://github.com/StreamnDad/reeln-plugin-tiktok
cd reeln-plugin-tiktok
make dev-install
```

## Configuration

Add the plugin to your reeln config:

```json
{
  "plugins": {
    "enabled": ["tiktok"],
    "settings": {
      "tiktok": {
        "upload_shorts": true,
        "upload_videos": false,
        "client_key": "awxxxxxxxxxxxxxxxx",
        "client_secret_file": "/path/to/tiktok_client_secret.txt",
        "privacy_level": "SELF_ONLY"
      }
    }
  }
}
```

### OAuth Setup

This plugin requires TikTok OAuth2 credentials. For the initial setup:

1. Create a TikTok Developer App at [developers.tiktok.com](https://developers.tiktok.com)
2. Add scopes: `video.upload` (inbox/drafts) and optionally `video.publish` (direct post)
3. Complete the OAuth2 authorization flow to obtain tokens
4. Save the credentials to `~/Library/Application Support/reeln/data/tiktok/oauth.json`:

```json
{
  "access_token": "act.xxx",
  "refresh_token": "rft.xxx",
  "expires_at": 1712345678.0,
  "open_id": "xxx",
  "scope": "video.upload,user.info.basic"
}
```

The plugin automatically refreshes expired tokens using the refresh token.

### Config Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `upload_shorts` | bool | `false` | Upload portrait renders (width < height) |
| `upload_videos` | bool | `false` | Upload landscape/square renders |
| `direct_post` | bool | `false` | Publish directly to feed (vs inbox/drafts) |
| `dry_run` | bool | `false` | Log actions without calling TikTok API |
| `client_key` | str | *required* | TikTok app client key |
| `client_secret_file` | str | *required* | Path to client secret file |
| `credentials_cache` | str | auto | OAuth token cache path |
| `privacy_level` | str | `SELF_ONLY` | `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY` |
| `disable_duet` | bool | `false` | Disable duets |
| `disable_comment` | bool | `false` | Disable comments |
| `disable_stitch` | bool | `false` | Disable stitch |
| `brand_content_toggle` | bool | `false` | Paid partnership disclosure (branded content) |
| `brand_organic_toggle` | bool | `false` | Your own brand disclosure |
| `caption_template` | str | `""` | Template with `{home_team}`, `{away_team}`, `{date}`, `{venue}`, `{sport}` |
| `video_cover_timestamp_ms` | int | `1000` | Cover frame offset (ms) |
| `chunk_size_bytes` | int | `10485760` | Upload chunk size (10 MB) |
| `upload_poll_interval_seconds` | int | `5` | Status poll interval |
| `upload_poll_max_attempts` | int | `60` | Max poll attempts |

## Development

```bash
make dev-install    # uv venv + editable install with dev deps
make test           # pytest with 100% coverage
make lint           # ruff check
make format         # ruff format
make check          # lint + mypy + test
```

## License

AGPL-3.0-only
