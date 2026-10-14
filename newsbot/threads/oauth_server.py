import json
import logging
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from newsbot.config import load_settings


log = logging.getLogger("threads.oauth")

ROOT = Path(__file__).resolve().parents[2]

STATE_LOCK = threading.Lock()
CURRENT_STATE = None


def _env_path():
    return ROOT / ".env"


def _update_env(values):
    path = _env_path()

    lines = (
        path.read_text(encoding="utf-8").splitlines()
        if path.exists()
        else []
    )

    result = []
    remaining = dict(values)

    for line in lines:
        if "=" not in line:
            result.append(line)
            continue

        key = line.split("=", 1)[0]

        if key in remaining:
            result.append(
                f"{key}={remaining.pop(key)}"
            )
        else:
            result.append(line)

    for key, value in remaining.items():
        result.append(f"{key}={value}")

    path.write_text(
        "\n".join(result) + "\n",
        encoding="utf-8",
    )


def _json_error(response):
    try:
        data = response.json()
    except Exception:
        return response.text[:1000]

    return json.dumps(
        data,
        ensure_ascii=False,
    )[:1000]


@dataclass(frozen=True)
class OAuthConfig:
    app_id: str
    app_secret: str
    redirect_uri: str
    scopes: str
    api_base_url: str


def _config():
    settings = load_settings()

    return OAuthConfig(
        app_id=getattr(
            settings,
            "threads_app_id",
            "",
        ),
        app_secret=getattr(
            settings,
            "threads_app_secret",
            "",
        ),
        redirect_uri=getattr(
            settings,
            "threads_redirect_uri",
            "",
        ),
        scopes=getattr(
            settings,
            "threads_oauth_scopes",
            (
                "threads_basic,"
                "threads_content_publish"
            ),
        ),
        api_base_url=getattr(
            settings,
            "threads_api_base_url",
            "https://graph.threads.net",
        ).rstrip("/"),
    )


def _authorization_url(cfg, state):
    params = {
        "client_id": cfg.app_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "response_type": "code",
        "state": state,
    }

    return (
        "https://threads.net/oauth/authorize?"
        + urlencode(params)
    )


def _exchange_code(cfg, code):
    response = requests.post(
        f"{cfg.api_base_url}/oauth/access_token",
        data={
            "client_id": cfg.app_id,
            "client_secret": cfg.app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": cfg.redirect_uri,
            "code": code,
        },
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            "Code exchange failed: "
            + _json_error(response)
        )

    data = response.json()

    token = data.get("access_token")

    if not token:
        raise RuntimeError(
            "Code exchange returned no access_token"
        )

    return token


def _exchange_long_lived(cfg, short_token):
    response = requests.get(
        f"{cfg.api_base_url}/access_token",
        params={
            "grant_type": "th_exchange_token",
            "client_secret": cfg.app_secret,
            "access_token": short_token,
        },
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            "Long-lived exchange failed: "
            + _json_error(response)
        )

    data = response.json()

    token = data.get("access_token")

    if not token:
        raise RuntimeError(
            "Long-lived exchange returned no access_token"
        )

    return token


def _verify_token(cfg, token):
    response = requests.get(
        f"{cfg.api_base_url}/me",
        params={
            "fields": "id,username",
        },
        headers={
            "Authorization": f"Bearer {token}",
        },
        timeout=30,
    )

    if not response.ok:
        raise RuntimeError(
            "Token verification failed: "
            + _json_error(response)
        )

    return response.json()


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info(
            "%s - %s",
            self.address_string(),
            fmt % args,
        )

    def _html(self, status, body):
        encoded = body.encode("utf-8")

        self.send_response(status)

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8",
        )

        self.send_header(
            "Content-Length",
            str(len(encoded)),
        )

        self.end_headers()

        self.wfile.write(encoded)

    def do_GET(self):
        global CURRENT_STATE

        parsed = urlparse(self.path)

        if parsed.path == "/threads/start":
            cfg = _config()

            if not (
                cfg.app_id
                and cfg.app_secret
                and cfg.redirect_uri
            ):
                self._html(
                    500,
                    "<h1>Threads OAuth is not configured</h1>"
                    "<p>Check THREADS_APP_ID, "
                    "THREADS_APP_SECRET and "
                    "THREADS_REDIRECT_URI.</p>",
                )
                return

            state = secrets.token_urlsafe(32)

            with STATE_LOCK:
                CURRENT_STATE = state

            self.send_response(302)

            self.send_header(
                "Location",
                _authorization_url(cfg, state),
            )

            self.end_headers()

            return

        if parsed.path == "/threads/callback":
            params = parse_qs(parsed.query)

            error = (
                params.get("error", [""])[0]
            )

            if error:
                message = (
                    params.get(
                        "error_message",
                        [""],
                    )[0]
                )

                self._html(
                    400,
                    "<h1>Threads authorization failed</h1>"
                    f"<p>{error}</p>"
                    f"<p>{message}</p>",
                )
                return

            code = (
                params.get("code", [""])[0]
            )

            state = (
                params.get("state", [""])[0]
            )

            with STATE_LOCK:
                expected_state = CURRENT_STATE
                CURRENT_STATE = None

            if not (
                code
                and state
                and expected_state
                and secrets.compare_digest(
                    state,
                    expected_state,
                )
            ):
                self._html(
                    400,
                    "<h1>Invalid OAuth state</h1>",
                )
                return

            try:
                cfg = _config()

                short_token = _exchange_code(
                    cfg,
                    code,
                )

                long_token = _exchange_long_lived(
                    cfg,
                    short_token,
                )

                profile = _verify_token(
                    cfg,
                    long_token,
                )

                _update_env(
                    {
                        "THREADS_ACCESS_TOKEN":
                            long_token,
                    }
                )

                username = profile.get(
                    "username",
                    "",
                )

                user_id = profile.get(
                    "id",
                    "",
                )

                log.info(
                    "Threads OAuth completed "
                    "user_id=%s username=%s",
                    user_id,
                    username,
                )

                self._html(
                    200,
                    "<h1>Threads connected successfully</h1>"
                    "<p>You can close this page.</p>"
                    "<p>Token was saved on the server.</p>",
                )

            except Exception:
                log.exception(
                    "Threads OAuth callback failed"
                )

                self._html(
                    500,
                    "<h1>Threads OAuth failed</h1>"
                    "<p>Check server logs.</p>",
                )

            return

        self._html(
            404,
            "<h1>Not found</h1>",
        )


def main():
    server = ThreadingHTTPServer(
        ("127.0.0.1", 8091),
        Handler,
    )

    log.info(
        "Threads OAuth server listening "
        "on 127.0.0.1:8091"
    )

    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
