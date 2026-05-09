"""Strava OAuth 2.0 flow and token management.

Supports two modes:
- Local: reads/writes tokens.json, credentials from .env
- Cloud (Streamlit): reads credentials and refresh_token from st.secrets, no file I/O
"""

from __future__ import annotations

import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

TOKENS_PATH = Path(__file__).parent.parent / "tokens.json"
AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"


def _is_cloud() -> bool:
    """Return True when running on Streamlit Cloud (st.secrets available)."""
    try:
        import streamlit as st
        _ = st.secrets["STRAVA_CLIENT_ID"]
        return True
    except Exception:
        return False


def _credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from env or Streamlit secrets."""
    if _is_cloud():
        import streamlit as st
        return st.secrets["STRAVA_CLIENT_ID"], st.secrets["STRAVA_CLIENT_SECRET"]
    from dotenv import load_dotenv
    load_dotenv()
    return os.getenv("STRAVA_CLIENT_ID", ""), os.getenv("STRAVA_CLIENT_SECRET", "")


def get_authorization_url() -> str:
    """Build the Strava OAuth authorization URL."""
    from dotenv import load_dotenv
    load_dotenv()
    client_id = os.getenv("STRAVA_CLIENT_ID")
    redirect_uri = os.getenv("STRAVA_REDIRECT_URI", "http://localhost:8080/callback")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "activity:read_all,profile:read_all",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def run_auth_flow() -> str:
    """Open browser for Strava auth and capture the authorization code via local callback."""
    from dotenv import load_dotenv
    load_dotenv()
    redirect_uri = os.getenv("STRAVA_REDIRECT_URI", "http://localhost:8080/callback")

    auth_url = get_authorization_url()
    print("Opening browser for Strava authorization...")
    print(f"If the browser doesn't open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    code_holder = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Authorization successful. You can close this tab.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h2>Authorization failed. No code received.</h2>")

        def log_message(self, format, *args):
            pass

    port = int(redirect_uri.split(":")[-1].split("/")[0])
    server = HTTPServer(("localhost", port), CallbackHandler)
    print(f"Waiting for Strava callback on port {port}...")
    server.handle_request()

    if "code" not in code_holder:
        raise RuntimeError("No authorization code received from Strava.")
    return code_holder["code"]


def exchange_code_for_tokens(code: str) -> dict:
    """Exchange authorization code for tokens and save to tokens.json."""
    client_id, client_secret = _credentials()
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    })
    resp.raise_for_status()
    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    }
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    print(f"Tokens saved to {TOKENS_PATH}")
    return tokens


def _do_refresh(refresh_token: str) -> dict:
    """Call Strava token endpoint with a refresh token, return new token dict."""
    client_id, client_secret = _credentials()
    resp = requests.post(TOKEN_URL, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    resp.raise_for_status()
    return resp.json()


def refresh_access_token() -> dict:
    """Refresh access token using tokens.json and update the file."""
    tokens = json.loads(TOKENS_PATH.read_text())
    data = _do_refresh(tokens["refresh_token"])
    tokens.update({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    })
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))
    print("Access token refreshed.")
    return tokens


def get_valid_token() -> str:
    """Return a valid Strava access token, refreshing if expired.

    On Streamlit Cloud: refreshes using STRAVA_REFRESH_TOKEN from secrets,
    caching the result in session_state so we only refresh once per session.
    Locally: reads tokens.json, refreshes if expired.
    """
    if _is_cloud():
        import streamlit as st
        cached = st.session_state.get("_strava_token")
        cached_exp = st.session_state.get("_strava_token_exp", 0)
        if cached and time.time() < cached_exp - 60:
            return cached
        data = _do_refresh(st.secrets["STRAVA_REFRESH_TOKEN"])
        st.session_state["_strava_token"] = data["access_token"]
        st.session_state["_strava_token_exp"] = data["expires_at"]
        return data["access_token"]

    if not TOKENS_PATH.exists():
        raise FileNotFoundError(
            f"No tokens.json found at {TOKENS_PATH}. Run:\n  python src/strava_auth.py"
        )
    tokens = json.loads(TOKENS_PATH.read_text())
    if time.time() >= tokens["expires_at"] - 60:
        tokens = refresh_access_token()
    return tokens["access_token"]


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("ERROR: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set in .env")
        raise SystemExit(1)

    print("=== Strava OAuth Flow ===")
    code = run_auth_flow()
    print("Authorization code received.")
    tokens = exchange_code_for_tokens(code)
    print(f"\nSuccess! Expires: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(tokens['expires_at']))}")
    print(f"\nAdd this to Streamlit secrets:\nSTRAVA_REFRESH_TOKEN = \"{tokens['refresh_token']}\"")
