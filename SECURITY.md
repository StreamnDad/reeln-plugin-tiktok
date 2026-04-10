# Security Policy

## Supported Versions

reeln-plugin-tiktok is pre-1.0 software. Security fixes are published
against the latest release only. We recommend always running the most
recent version from
[PyPI](https://pypi.org/project/reeln-plugin-tiktok/) or the
[Releases page](https://github.com/StreamnDad/reeln-plugin-tiktok/releases).

| Version | Supported          |
| ------- | ------------------ |
| latest release | :white_check_mark: |
| older   | :x:                |

## Scope

reeln-plugin-tiktok is a reeln-cli plugin that uploads video clips to
TikTok via the TikTok Content Posting API. It runs inside `reeln-cli`
on a livestreamer's local machine and makes outbound HTTPS requests to
TikTok using OAuth 2.0 access tokens stored on disk.

In-scope concerns include, but are not limited to:
- Leakage of TikTok client secrets, access tokens, or refresh tokens via
  logs, error messages, cached responses, or saved state
- Insecure file permissions on the on-disk token store
- OAuth redirect / state handling flaws during the initial authorization
  flow (e.g. open redirect, missing CSRF state validation)
- Scope abuse — requesting broader TikTok API scopes than necessary,
  or failing to honor revoked tokens
- Unsafe deserialization of TikTok API responses or cached metadata
- Command injection or path traversal in upload staging directories,
  chunked-upload artifacts, or metadata written to disk
- Dependency confusion or typosquatting on the PyPI package name

Out of scope:
- Vulnerabilities in the TikTok Content Posting API itself or in any
  upstream TikTok SDK — report those to TikTok
- Vulnerabilities in reeln-cli or other reeln plugins — report those to
  the respective repository
- Issues that require an attacker to already have local code execution
  on the user's machine or access to the stored OAuth tokens

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub
issues, discussions, or pull requests.**

Report vulnerabilities using GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/StreamnDad/reeln-plugin-tiktok/security)
   of this repository
2. Click **"Report a vulnerability"**
3. Fill in as much detail as you can: affected version, reproduction steps,
   impact, and any suggested mitigation

If you cannot use GitHub's reporting, email **git-security@email.remitz.us**
instead.

### What to include

A good report contains:
- The version of reeln-plugin-tiktok, reeln-cli, and Python you tested
  against
- Your operating system and architecture (macOS / Windows / Linux, arch)
- Steps to reproduce the issue
- What you expected to happen vs. what actually happened
- The potential impact (token leakage, account takeover, scope abuse,
  data loss, etc.)
- Any proof-of-concept code, if applicable

### What to expect

This plugin is maintained by a small team, so all timelines below are
best-effort rather than hard guarantees:

- **Acknowledgement:** typically within a week of your report
- **Initial assessment:** usually within two to three weeks, including
  whether we consider the report in scope and our planned next steps
- **Status updates:** roughly every few weeks until the issue is resolved
- **Fix & disclosure:** coordinated with you. We aim to ship a patch
  release reasonably quickly for high-severity issues, with lower-severity
  issues addressed in a future release. Credit will be given in the
  release notes and CHANGELOG unless you prefer to remain anonymous.

If a report is declined, we will explain why. You are welcome to disagree
and provide additional context.
