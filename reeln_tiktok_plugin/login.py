"""Interactive OAuth2 login flow for TikTok.

Runs a local HTTP server to capture the authorization callback, exchanges
the code for tokens, and saves them to the credentials cache.

Usage::

    python -m reeln_tiktok_plugin.login \\
        --client-key YOUR_CLIENT_KEY \\
        --client-secret-file /path/to/secret.txt

Or via Makefile::

    make login CLIENT_KEY=... CLIENT_SECRET_FILE=...
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import secrets
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

from reeln.core.config import data_dir

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REDIRECT_URI = "http://localhost:8484/callback/"
DEFAULT_SCOPES = "video.upload,user.info.basic"


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    TikTok requires **hex-encoded** SHA256 (not base64url as per RFC 7636).
    """
    code_verifier = secrets.token_urlsafe(64)[:128]
    code_challenge = hashlib.sha256(code_verifier.encode("ascii")).hexdigest()
    return code_verifier, code_challenge


def _build_auth_url(client_key: str, scopes: str, state: str, code_challenge: str) -> str:
    """Build the TikTok authorization URL.

    Encodes parameters individually to avoid over-encoding commas in
    scopes and slashes in the redirect URI — TikTok's auth endpoint
    is sensitive to encoding.
    """
    q = urllib.parse.quote
    parts = [
        f"client_key={q(client_key)}",
        f"scope={q(scopes, safe=',')}",
        "response_type=code",
        f"redirect_uri={q(REDIRECT_URI, safe='')}",
        f"state={q(state)}",
        f"code_challenge={q(code_challenge)}",
        "code_challenge_method=S256",
    ]
    return f"{TIKTOK_AUTH_URL}?{'&'.join(parts)}"


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth callback parameters."""

    auth_code: str | None = None
    auth_error: str | None = None
    auth_scopes: str = ""
    received_state: str = ""

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        error = params.get("error", [None])[0]
        if error:
            _CallbackHandler.auth_error = error
            self._respond(
                "Authorization denied by TikTok.\n\n"
                f"Error: {error}\n"
                f"Description: {params.get('error_description', [''])[0]}\n\n"
                "You can close this tab and retry in the terminal."
            )
            return

        code = params.get("code", [None])[0]
        if not code:
            _CallbackHandler.auth_error = "no_code"
            self._respond("No authorization code received. You can close this tab.")
            return

        _CallbackHandler.auth_code = code
        _CallbackHandler.auth_scopes = params.get("scopes", [""])[0]
        _CallbackHandler.received_state = params.get("state", [""])[0]
        self._respond(
            "Authorization successful! You can close this tab.\n\n"
            f"Scopes granted: {_CallbackHandler.auth_scopes}"
        )

    def _respond(self, message: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(message.encode())

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Suppress default access log


def _wait_for_callback(expected_state: str) -> tuple[str, str]:
    """Start a local server and wait for the OAuth callback.

    Returns:
        Tuple of (authorization_code, granted_scopes).

    Raises:
        RuntimeError: If authorization was denied or state mismatch.
    """
    _CallbackHandler.auth_code = None
    _CallbackHandler.auth_error = None
    _CallbackHandler.auth_scopes = ""
    _CallbackHandler.received_state = ""

    server = http.server.HTTPServer(("localhost", 8484), _CallbackHandler)
    server.timeout = 300  # 5 minute timeout

    print("  Waiting for authorization (timeout: 5 minutes)...")
    server.handle_request()
    server.server_close()

    if _CallbackHandler.auth_error:
        raise RuntimeError(f"Authorization failed: {_CallbackHandler.auth_error}")

    if not _CallbackHandler.auth_code:
        raise RuntimeError("No authorization code received")

    if _CallbackHandler.received_state != expected_state:
        raise RuntimeError("State mismatch — possible CSRF attack")

    return _CallbackHandler.auth_code, _CallbackHandler.auth_scopes


def _exchange_code(
    code: str,
    client_key: str,
    client_secret: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange authorization code for access/refresh tokens.

    Raises:
        RuntimeError: On HTTP errors or missing tokens.
    """
    payload = urllib.parse.urlencode(
        {
            "client_key": client_key,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
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
        raise RuntimeError(f"Token exchange failed (HTTP {exc.code}): {error_body[:300]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Token exchange request failed: {exc.reason}") from exc

    data: dict[str, Any] = json.loads(body)

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")

    if not access_token:
        raise RuntimeError(f"Token response missing access_token: {body[:300]}")

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + int(data.get("expires_in", 0)),
        "open_id": data.get("open_id", ""),
        "scope": data.get("scope", ""),
    }


def _save_credentials(creds: dict[str, Any], cache_path: Path) -> None:
    """Write credentials to cache file with 0o600 permissions."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(creds, indent=2))
    if os.name != "nt":
        cache_path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def login(
    client_key: str,
    client_secret_file: Path,
    cache_path: Path | None = None,
    scopes: str = DEFAULT_SCOPES,
) -> bool:
    """Run the full interactive OAuth2 login flow.

    Opens a browser for authorization, captures the callback, exchanges
    the code for tokens, and saves them.  Supports retry on denial.

    Returns:
        True if login succeeded, False if the user chose to quit.
    """
    secret = client_secret_file.read_text().strip()
    if not secret:
        print(f"Error: client secret file is empty: {client_secret_file}")
        return False

    if cache_path is None:
        cache_path = Path(data_dir()) / "tiktok" / "oauth.json"

    while True:
        state = secrets.token_urlsafe(32)
        code_verifier, code_challenge = _generate_pkce()

        auth_url = _build_auth_url(client_key, scopes, state, code_challenge)

        print("\n" + "=" * 60)
        print("  TikTok OAuth2 Login")
        print("=" * 60)
        print(f"\n  Client key:  {client_key}")
        print(f"  Scopes:      {scopes}")
        print(f"  Redirect:    {REDIRECT_URI}")
        print(f"  Cache path:  {cache_path}")
        print("\n  Opening browser for authorization...")
        print("  (If browser doesn't open, copy this URL):\n")
        print(f"  {auth_url}\n")

        webbrowser.open(auth_url)

        try:
            code, granted_scopes = _wait_for_callback(state)
        except RuntimeError as exc:
            print(f"\n  Error: {exc}")
            retry = input("\n  Retry? [Y/n] ").strip().lower()
            if retry in ("n", "no"):
                print("  Aborted.")
                return False
            continue

        print("\n  Authorization code received!")
        print(f"  Scopes granted: {granted_scopes}")
        print("  Exchanging code for tokens...")

        try:
            creds = _exchange_code(code, client_key, secret, code_verifier)
        except RuntimeError as exc:
            print(f"\n  Error: {exc}")
            retry = input("\n  Retry? [Y/n] ").strip().lower()
            if retry in ("n", "no"):
                print("  Aborted.")
                return False
            continue

        _save_credentials(creds, cache_path)

        print(f"\n  Credentials saved to: {cache_path}")
        print(f"  Open ID:     {creds.get('open_id', '')}")
        print(f"  Scope:       {creds.get('scope', '')}")
        print(f"  Expires at:  {time.ctime(creds['expires_at'])}")
        print("\n  Login complete!")
        print("=" * 60)
        return True


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="TikTok OAuth2 login for reeln-plugin-tiktok")
    parser.add_argument("--client-key", required=True, help="TikTok app client key")
    parser.add_argument("--client-secret-file", required=True, help="Path to client secret file")
    parser.add_argument("--cache-path", default=None, help="OAuth cache path (default: data_dir/tiktok/oauth.json)")
    parser.add_argument("--scopes", default=DEFAULT_SCOPES, help=f"Comma-separated scopes (default: {DEFAULT_SCOPES})")
    args = parser.parse_args()

    cache = Path(args.cache_path) if args.cache_path else None
    success = login(
        client_key=args.client_key,
        client_secret_file=Path(args.client_secret_file),
        cache_path=cache,
        scopes=args.scopes,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
