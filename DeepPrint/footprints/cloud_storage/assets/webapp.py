#!/usr/bin/env python3
"""Web decoy application for the DeepPrint cloud_storage persona.

Presents a Nextcloud-style file-sharing estate that fingerprints as both a
PHP application server and a Node.js sidecar service. Intended for
deception and WAF evaluation use behind a reverse proxy or WAF.

PHP surface (Nextcloud look-alike):
    /                        redirect to canonical login
    /index.php/login         login page + credential capture
    /index.php/lostpassword  password reset form (stubbed)
    /status.php              Nextcloud status JSON
    /remote.php/dav          WebDAV endpoint (401 Basic auth, creds logged)
    /ocs/v1.php, /ocs/v2.php OCS API stubs
    /cron.php, /robots.txt, /favicon.ico
    everything else          Nextcloud-themed 404

Node.js surface (collaboration sidecar):
    /api/v1/status           Express-style status JSON with node version
    /socket.io/              socket.io v4 polling handshake
    /metrics                 prom-client style exposition
    /api/* (unknown)         Express-style 404 JSON

All requests are logged as JSON lines to stdout and to DECOY_LOG_FILE.
Login form and Basic-auth credentials are captured in the log for
honeypot analysis.

Configuration comes from DECOY_* environment variables; see load_config().
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import struct
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from urllib.parse import parse_qs, urlsplit

LOG_LOCK = threading.Lock()
START_TIME = time.time()
MAX_BODY_BYTES = 1048576


def load_config() -> dict:
    return {
        "site_name": os.environ.get("DECOY_SITE_NAME", "Sparks Manor Cloud"),
        "domain": os.environ.get("DECOY_DOMAIN", "cloud.sparksmanor.com"),
        "nextcloud_version": os.environ.get("DECOY_NEXTCLOUD_VERSION", "30.0.4"),
        "php_version": os.environ.get("DECOY_PHP_VERSION", "8.3.14"),
        "node_version": os.environ.get("DECOY_NODE_VERSION", "v20.11.1"),
        "nginx_version": os.environ.get("DECOY_NGINX_VERSION", "1.24.0"),
        "log_file": os.environ.get("DECOY_LOG_FILE", "/srv/log/decoy.log"),
    }


CONFIG = load_config()


def log_event(event: str, config: dict, **fields: object) -> None:
    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    record.update(fields)
    line = json.dumps(record, sort_keys=True)
    with LOG_LOCK:
        print(line, flush=True)
        log_path = config.get("log_file")
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass


def build_favicon() -> bytes:
    """Build a small blue-cloud 16x16 ICO entirely in memory."""
    width = 16
    height = 16
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            big = (x - 8) ** 2 + (y - 9) ** 2 <= 25
            left = (x - 5) ** 2 + (y - 8) ** 2 <= 9
            right = (x - 11) ** 2 + (y - 8) ** 2 <= 9
            if big or left or right:
                pixels.extend((201, 130, 0, 255))
            else:
                pixels.extend((0, 0, 0, 0))
    and_mask = b"\x00" * (height * 4)
    bitmap_header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height * 2,
        1,
        32,
        0,
        len(pixels) + len(and_mask),
        0,
        0,
        0,
        0,
    )
    image = bitmap_header + bytes(pixels) + and_mask
    icon_dir = struct.pack("<HHH", 0, 1, 1)
    icon_entry = struct.pack(
        "<BBBBHHII",
        width,
        height,
        0,
        0,
        1,
        32,
        len(image),
        22,
    )
    return icon_dir + icon_entry + image


FAVICON_BYTES = build_favicon()

LOGO_SVG = (
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" '
    'width="52" height="52" aria-hidden="true">\n'
    '  <g fill="#0082c9">\n'
    '    <circle cx="24" cy="38" r="11"/>\n'
    '    <circle cx="40" cy="34" r="13"/>\n'
    '    <rect x="24" y="38" width="16" height="12" rx="6"/>\n'
    "  </g>\n"
    "</svg>"
)

PAGE_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, minimum-scale=1.0">
    <meta name="robots" content="noindex, nofollow">
    <title>$site</title>
    <style>
        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                Oxygen, Ubuntu, Cantarell, 'Open Sans', 'Frutiger', sans-serif;
            background: #f5f5f5;
            color: #222222;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
        }

        header {
            background: #ffffff;
            box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
            padding: 12px 24px;
            display: flex;
            align-items: center;
            gap: 12px;
        }

        header .sitename {
            font-size: 1.25rem;
            font-weight: 600;
            color: #222222;
            text-decoration: none;
        }

        main {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 48px 16px;
        }

        .guest-box {
            width: 100%;
            max-width: 320px;
        }

        h2 {
            text-align: center;
            font-size: 1.35rem;
            margin: 0 0 24px;
            font-weight: 600;
        }

        fieldset {
            border: none;
            margin: 0;
            padding: 0;
        }

        .field {
            margin-bottom: 12px;
        }

        input[type="text"],
        input[type="email"],
        input[type="password"] {
            width: 100%;
            padding: 12px 14px;
            border: 1px solid #dbdbdb;
            border-radius: 8px;
            font-size: 0.95rem;
            background: #ffffff;
            outline: none;
        }

        input:focus {
            border-color: #0082c9;
        }

        button.primary {
            width: 100%;
            padding: 12px;
            background: #0082c9;
            color: #ffffff;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
        }

        button.primary:hover,
        button.primary:focus {
            background: #0073b0;
        }

        .update {
            background: #fcbfbf;
            border: 1px solid #e9322d;
            border-radius: 8px;
            padding: 10px 12px;
            margin-bottom: 16px;
            color: #8f1d19;
        }

        .update p {
            margin: 0;
            font-size: 0.9rem;
        }

        .links {
            margin-top: 20px;
            text-align: center;
        }

        .links a {
            color: #0082c9;
            text-decoration: none;
            font-size: 0.9rem;
        }

        footer {
            text-align: center;
            padding: 16px;
            color: #777777;
            font-size: 0.8rem;
        }

        footer a {
            color: #777777;
        }
    </style>
</head>
<body>
    <header>
        <div class="logo">$logo</div>
        <a class="sitename" href="/">$site</a>
    </header>
    <main>
        <div class="guest-box">
            $body
        </div>
    </main>
    <footer>$site &middot; powered by <a href="https://nextcloud.com"
    rel="noreferrer noopener">Nextcloud</a> $nc on PHP $php</footer>
</body>
</html>
"""
)

LOGIN_FORM_TEMPLATE = Template(
    """<form method="post" action="/index.php/login" name="login">
    <fieldset>
        <h2>$site</h2>
        $error_box
        <div class="field">
            <input type="text" name="user" value=""
                placeholder="Username or email" autocomplete="username"
                autocapitalize="off" autocorrect="off" required>
        </div>
        <div class="field">
            <input type="password" name="password" value=""
                placeholder="Password" autocomplete="current-password" required>
        </div>
        <input type="hidden" name="requesttoken" value="$token">
        <input type="hidden" name="redirect_url" value="">
        <button class="primary" type="submit" id="submit" name="submit">
            Log in</button>
    </fieldset>
</form>
<div class="links">
    <a href="/index.php/lostpassword">Forgot password?</a>
</div>
"""
)

LOSTPASSWORD_FORM_TEMPLATE = Template(
    """<form method="post" action="/index.php/lostpassword" name="lostpassword">
    <fieldset>
        <h2>Forgot password?</h2>
        <div class="field">
            <input type="email" name="email" value=""
                placeholder="Email address of your account" autocomplete="email"
                required>
        </div>
        <button class="primary" type="submit" name="submit">Send password reset</button>
    </fieldset>
</form>
<div class="links">
    <a href="/index.php/login">Back to login</a>
</div>
"""
)

LOSTPASSWORD_SENT_TEMPLATE = Template(
    """<fieldset>
    <h2>Forgot password?</h2>
    <div class="update">
        <p>If this address exists in our user directory, a password reset
        message has been sent to it.</p>
    </div>
</fieldset>
<div class="links">
    <a href="/index.php/login">Back to login</a>
</div>
"""
)

NOT_FOUND_TEMPLATE = Template(
    """<fieldset>
    <h2>$site</h2>
    <div class="update">
        <p>Page not found. Contact your administrator if this message persists.</p>
    </div>
</fieldset>
<div class="links">
    <a href="/index.php/login">Log in</a>
</div>
"""
)

DAV_UNAUTHORIZED_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<d:error xmlns:d=\"DAV:\" xmlns:s=\"http://sabredav.org/ns\">\n"
    "    <s:exception>Sabre\\DAV\\Exception\\NotAuthenticated</s:exception>\n"
    "    <s:message>No 'Authorization: Basic' header found. Either the client "
    "didn't send one, or the server was misconfigured</s:message>\n"
    "</d:error>\n"
)

OCS_INVALID_QUERY_BODY = (
    '{"message":"Invalid query, please check the syntax. API specification is '
    'here: http://www.freedesktop.org/wiki/Specifications/'
    'open-collaboration-services."}'
)


def is_node_path(path: str) -> bool:
    return (
        path == "/api"
        or path.startswith("/api/")
        or path == "/socket.io"
        or path.startswith("/socket.io/")
        or path == "/metrics"
        or path == "/healthz"
    )


class DecoyRequestHandler(BaseHTTPRequestHandler):
    config: dict = CONFIG
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:
        return "nginx/" + str(self.config["nginx_version"])

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    # ------------------------------------------------------------------
    # HTTP method dispatch
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._handle("DELETE")

    def do_PROPFIND(self) -> None:  # noqa: N802
        self._handle("PROPFIND")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._handle("OPTIONS")

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _handle(self, method: str) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path or "/"
        query = parsed.query
        started = time.time()
        self._status = 0
        self._sent = 0
        try:
            if is_node_path(path):
                self._route_node(method, path, query)
            else:
                self._route_php(method, path, query)
        except (BrokenPipeError, ConnectionResetError):
            return
        log_event(
            "request",
            self.config,
            client=self.client_address[0],
            method=method,
            path=path,
            query=query,
            user_agent=self.headers.get("User-Agent", ""),
            referer=self.headers.get("Referer", ""),
            status=self._status,
            stack="node" if is_node_path(path) else "php",
            bytes=self._sent,
            duration_ms=round((time.time() - started) * 1000, 2),
        )

    def _route_node(self, method: str, path: str, query: str) -> None:
        if method not in ("GET", "HEAD"):
            self._send_node_405(method)
            return

        if path in ("/api/v1/status", "/api/status", "/api/v1/health", "/healthz"):
            self._send_node_status()
            return

        if path == "/metrics":
            self._send_metrics()
            return

        if path == "/socket.io" or path.startswith("/socket.io/"):
            self._send_socketio(query)
            return

        self._send_express_404(method, path)

    def _route_php(self, method: str, path: str, query: str) -> None:
        if path in ("/", "/index.php", "/login", "/index.php/apps", "/apps"):
            self._send(
                302,
                "",
                "text/html; charset=UTF-8",
                self._php_headers() + [("Location", "/index.php/login")],
            )
            return

        if path == "/index.php/login":
            if method in ("GET", "HEAD"):
                self._send_login_page("wrongpwd" in parse_qs(query))
            elif method == "POST":
                self._handle_login_post(path)
            else:
                self._send_node_405(method, php=True)
            return

        if path == "/index.php/lostpassword":
            if method in ("GET", "HEAD"):
                self._send_lostpassword_page()
            elif method == "POST":
                self._handle_lostpassword_post(path)
            else:
                self._send_node_405(method, php=True)
            return

        if path == "/status.php":
            if method in ("GET", "HEAD"):
                payload = {
                    "installed": True,
                    "maintenance": False,
                    "needsDbUpgrade": False,
                    "version": self.config["nextcloud_version"],
                    "versionstring": self.config["nextcloud_version"],
                    "edition": "",
                    "productname": "Nextcloud",
                }
                self._send(
                    200,
                    json.dumps(payload),
                    "application/json; charset=utf-8",
                    self._php_headers(),
                )
            else:
                self._send_node_405(method, php=True)
            return

        if path.startswith("/remote.php"):
            self._handle_dav(method, path)
            return

        if path.startswith("/ocs/v1.php") or path.startswith("/ocs/v2.php"):
            self._capture_basic_auth(path)
            if path.endswith("/core/getapppassword"):
                self._send(
                    401,
                    '{"message":"Current user is not logged in"}',
                    "application/json; charset=utf-8",
                    self._php_headers(),
                )
            else:
                self._send(
                    404,
                    OCS_INVALID_QUERY_BODY,
                    "application/json; charset=utf-8",
                    self._php_headers(),
                )
            return

        if path == "/cron.php":
            if method in ("GET", "HEAD"):
                self._send(200, "", "text/html; charset=UTF-8", self._php_headers())
            else:
                self._send_node_405(method, php=True)
            return

        if path == "/robots.txt":
            self._send(
                200,
                "User-agent: *\nDisallow: /\n",
                "text/plain; charset=UTF-8",
                self._php_headers(),
            )
            return

        if path == "/favicon.ico":
            self._send(
                200,
                FAVICON_BYTES,
                "image/x-icon",
                self._php_headers() + [("Cache-Control", "max-age=604800")],
            )
            return

        if (
            path.startswith("/apps/")
            or path.startswith("/index.php/apps/")
            or path.startswith("/settings")
            or path.startswith("/index.php/settings")
            or path.startswith("/s/")
            or path.startswith("/index.php/s/")
            or path.startswith("/avatar")
            or path.startswith("/logout")
            or path.startswith("/index.php/logout")
        ):
            self._send(
                302,
                "",
                "text/html; charset=UTF-8",
                self._php_headers() + [("Location", "/index.php/login")],
            )
            return

        self._send_themed_404()

    # ------------------------------------------------------------------
    # PHP-zone handlers
    # ------------------------------------------------------------------

    def _send_login_page(self, error: bool) -> None:
        error_box = ""
        if error:
            error_box = (
                '        <div class="update" role="alert">\n'
                "            <p>Wrong username or password.</p>\n"
                "        </div>\n"
            )
        body = LOGIN_FORM_TEMPLATE.substitute(
            site=self.config["site_name"],
            error_box=error_box,
            token=secrets.token_hex(16),
        )
        html = PAGE_TEMPLATE.substitute(
            site=self.config["site_name"],
            logo=LOGO_SVG,
            body=body,
            nc=self.config["nextcloud_version"],
            php=self.config["php_version"],
        )
        self._send(
            200,
            html,
            "text/html; charset=UTF-8",
            self._php_headers(with_session=True),
        )

    def _send_lostpassword_page(self) -> None:
        body = LOSTPASSWORD_FORM_TEMPLATE.substitute()
        html = PAGE_TEMPLATE.substitute(
            site=self.config["site_name"],
            logo=LOGO_SVG,
            body=body,
            nc=self.config["nextcloud_version"],
            php=self.config["php_version"],
        )
        self._send(
            200,
            html,
            "text/html; charset=UTF-8",
            self._php_headers(with_session=True),
        )

    def _send_lostpassword_sent_page(self) -> None:
        body = LOSTPASSWORD_SENT_TEMPLATE.substitute()
        html = PAGE_TEMPLATE.substitute(
            site=self.config["site_name"],
            logo=LOGO_SVG,
            body=body,
            nc=self.config["nextcloud_version"],
            php=self.config["php_version"],
        )
        self._send(
            200,
            html,
            "text/html; charset=UTF-8",
            self._php_headers(with_session=True),
        )

    def _send_themed_404(self) -> None:
        body = NOT_FOUND_TEMPLATE.substitute(site=self.config["site_name"])
        html = PAGE_TEMPLATE.substitute(
            site=self.config["site_name"],
            logo=LOGO_SVG,
            body=body,
            nc=self.config["nextcloud_version"],
            php=self.config["php_version"],
        )
        self._send(404, html, "text/html; charset=UTF-8", self._php_headers())

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        length = max(0, min(length, MAX_BODY_BYTES))
        if length == 0:
            return b""
        return self.rfile.read(length)

    def _parse_credentials(self, body: bytes) -> tuple:
        content_type = (self.headers.get("Content-Type") or "").split(";")[0]
        content_type = content_type.strip().lower()
        user = ""
        password = ""
        if content_type == "application/json":
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                data = {}
            if isinstance(data, dict):
                user = str(
                    data.get("user") or data.get("username") or data.get("login") or ""
                )
                password = str(data.get("password") or data.get("pass") or "")
        else:
            fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
            user = (fields.get("user") or fields.get("username") or [""])[0]
            password = (fields.get("password") or fields.get("pass") or [""])[0]
        return user, password

    def _handle_login_post(self, path: str) -> None:
        body = self._read_body()
        user, password = self._parse_credentials(body)
        log_event(
            "login_attempt",
            self.config,
            client=self.client_address[0],
            user=user,
            password=password,
            path=path,
            user_agent=self.headers.get("User-Agent", ""),
            content_type=(self.headers.get("Content-Type") or "").split(";")[0].strip(),
        )
        self._send(
            302,
            "",
            "text/html; charset=UTF-8",
            self._php_headers(with_session=True)
            + [("Location", "/index.php/login?wrongpwd=1")],
        )

    def _handle_lostpassword_post(self, path: str) -> None:
        body = self._read_body()
        fields = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        email = (fields.get("email") or [""])[0]
        log_event(
            "password_reset_attempt",
            self.config,
            client=self.client_address[0],
            email=email,
            path=path,
            user_agent=self.headers.get("User-Agent", ""),
        )
        self._send_lostpassword_sent_page()

    def _capture_basic_auth(self, path: str) -> None:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return
        user, _, password = decoded.partition(":")
        log_event(
            "basic_auth_attempt",
            self.config,
            client=self.client_address[0],
            user=user,
            password=password,
            path=path,
            user_agent=self.headers.get("User-Agent", ""),
        )

    def _handle_dav(self, method: str, path: str) -> None:
        if method == "OPTIONS":
            self._send(
                200,
                "",
                "text/html; charset=UTF-8",
                self._php_headers()
                + [
                    ("DAV", "1, 3, extended-mkcol"),
                    ("Allow", "OPTIONS, GET, HEAD, DELETE, PROPFIND, PUT, PROPPATCH"),
                    ("MS-Author-Via", "DAV"),
                ],
            )
            return
        self._capture_basic_auth(path)
        self._send(
            401,
            DAV_UNAUTHORIZED_BODY,
            'application/xml; charset=utf-8',
            self._php_headers()
            + [("WWW-Authenticate", 'Basic realm="Nextcloud"')],
        )

    # ------------------------------------------------------------------
    # Node-zone handlers
    # ------------------------------------------------------------------

    def _send_node_status(self) -> None:
        uptime = time.time() - START_TIME
        payload = {
            "status": "ok",
            "service": "collab-bridge",
            "site": self.config["site_name"],
            "node": self.config["node_version"],
            "uptime_seconds": round(uptime, 2),
            "pid": 1,
            "memory": {
                "rss_bytes": 53248000 + int(uptime * 137) % 900000,
                "heap_used_bytes": 23101200 + int(uptime * 53) % 500000,
            },
        }
        self._send(
            200,
            json.dumps(payload),
            "application/json; charset=utf-8",
            self._node_headers(),
        )

    def _send_metrics(self) -> None:
        uptime = time.time() - START_TIME
        node_version = self.config["node_version"]
        version_parts = node_version.lstrip("v").split(".")
        while len(version_parts) < 3:
            version_parts.append("0")
        lines = [
            "# HELP process_resident_memory_bytes Resident memory size in bytes.",
            "# TYPE process_resident_memory_bytes gauge",
            "process_resident_memory_bytes "
            + str(53248000 + int(uptime * 137) % 900000),
            "# HELP process_cpu_seconds_total Total user and system CPU time.",
            "# TYPE process_cpu_seconds_total counter",
            "process_cpu_seconds_total " + str(round(uptime * 0.06, 2)),
            "# HELP nodejs_eventloop_lag_seconds The lag of the event loop.",
            "# TYPE nodejs_eventloop_lag_seconds gauge",
            "nodejs_eventloop_lag_seconds "
            + str(round(0.004 + (int(uptime * 10) % 40) / 10000, 4)),
            "# HELP nodejs_heap_size_used_bytes Process heap size used in bytes.",
            "# TYPE nodejs_heap_size_used_bytes gauge",
            "nodejs_heap_size_used_bytes "
            + str(23101200 + int(uptime * 53) % 500000),
            "# HELP nodejs_external_memory_bytes External memory size in bytes.",
            "# TYPE nodejs_external_memory_bytes gauge",
            "nodejs_external_memory_bytes " + str(3145700 + int(uptime * 7) % 90000),
            "# HELP nodejs_gc_duration_seconds GC duration in seconds.",
            "# TYPE nodejs_gc_duration_seconds histogram",
            'nodejs_gc_duration_seconds_bucket{le="0.001"} 3',
            'nodejs_gc_duration_seconds_bucket{le="0.01"} 6',
            'nodejs_gc_duration_seconds_bucket{le="0.1"} 8',
            "nodejs_gc_duration_seconds_bucket{le=\"1\"} 8",
            "nodejs_gc_duration_seconds_sum 0.0192",
            "nodejs_gc_duration_seconds_count 8",
            "# HELP nodejs_version_info Node.js version info.",
            "# TYPE nodejs_version_info gauge",
            'nodejs_version_info{version="' + node_version + '",major="'
            + version_parts[0] + '",minor="' + version_parts[1] + '",patch="'
            + version_parts[2] + '"} 1',
        ]
        self._send(
            200,
            "\n".join(lines) + "\n",
            "text/plain; version=0.0.4; charset=utf-8",
            self._node_headers(),
        )

    def _send_socketio(self, query: str) -> None:
        params = parse_qs(query)
        transport = (params.get("transport") or ["polling"])[0]
        if transport != "polling":
            self._send(
                400,
                '{"code":0,"message":"Transport unknown"}',
                "application/json; charset=utf-8",
                self._node_headers(),
            )
            return
        sid = secrets.token_hex(10)
        handshake = (
            '0{"sid":"'
            + sid
            + '","upgrades":["websocket"],"pingInterval":25000,'
            '"pingTimeout":20000,"maxPayload":1000000}'
        )
        self._send(
            200,
            handshake,
            "text/plain; charset=UTF-8",
            self._node_headers(),
        )

    def _send_express_404(self, method: str, path: str) -> None:
        body = '{"message":"Cannot ' + method + " " + path + '"}'
        self._send(
            404,
            body,
            "application/json; charset=utf-8",
            self._node_headers(),
        )

    def _send_node_405(self, method: str, php: bool = False) -> None:
        headers = self._php_headers() if php else self._node_headers()
        headers = headers + [("Allow", "GET, HEAD")]
        self._send(405, "", "text/html; charset=UTF-8", headers)

    # ------------------------------------------------------------------
    # Header and response plumbing
    # ------------------------------------------------------------------

    def _php_headers(self, with_session: bool = False) -> list:
        headers = [
            ("X-Powered-By", "PHP/" + str(self.config["php_version"])),
            ("X-Content-Type-Options", "nosniff"),
            ("X-Frame-Options", "SAMEORIGIN"),
            ("X-Robots-Tag", "noindex, nofollow"),
            ("X-Request-Id", secrets.token_hex(8)),
        ]
        if with_session:
            headers.append(
                (
                    "Set-Cookie",
                    "PHPSESSID="
                    + secrets.token_hex(13)
                    + "; path=/; HttpOnly; SameSite=Lax",
                )
            )
        return headers

    def _node_headers(self) -> list:
        return [
            ("X-Powered-By", "Express"),
            ("ETag", 'W/"' + secrets.token_hex(4) + "-" + secrets.token_hex(6) + '"'),
        ]

    def _send(
        self,
        status: int,
        body,
        content_type: str,
        extra_headers: list | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for header in extra_headers or []:
            self.send_header(header[0], header[1])
        self.end_headers()
        self._status = status
        if self.command == "HEAD":
            self._sent = 0
            return
        self.wfile.write(body)
        self._sent = len(body)


def build_server(
    bind: str = "0.0.0.0",
    port: int = 8081,
    config: dict | None = None,
) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer bound to the given decoy configuration."""
    cfg = dict(config) if config is not None else load_config()
    handler = type("BoundDecoyHandler", (DecoyRequestHandler,), {"config": cfg})
    httpd = ThreadingHTTPServer((bind, port), handler)
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    bind = os.environ.get("DECOY_BIND", "0.0.0.0")
    port = int(os.environ.get("DECOY_PORT", "8081"))
    httpd = build_server(bind, port)
    log_event(
        "decoy_started",
        CONFIG,
        bind=bind,
        port=port,
        site=CONFIG["site_name"],
        domain=CONFIG["domain"],
        nextcloud_version=CONFIG["nextcloud_version"],
        php_version=CONFIG["php_version"],
        node_version=CONFIG["node_version"],
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
