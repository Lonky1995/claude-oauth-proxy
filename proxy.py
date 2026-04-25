#!/usr/bin/env python3
"""
claude-oauth-proxy — Local HTTP proxy that forwards Anthropic /v1/messages requests
using Claude Max/Pro OAuth tokens from ~/.claude/.credentials.json.

Clients (PersonalAgent) call this proxy instead of api.anthropic.com directly.
Proxy injects OAuth Bearer token, Claude Code identity system prompt, and the
required anthropic-beta headers.

Supports:
- /v1/messages (POST, non-streaming + SSE streaming)
- Upstream via HTTPS_PROXY (so VPS reaches api.anthropic.com through mihomo)
"""

import argparse
import json
import os
import sys
import time
import ssl
import socket
import re as _re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

OAUTH_BETAS = "oauth-2025-04-20,claude-code-20250219,fine-grained-tool-streaming-2025-05-14,interleaved-thinking-2025-05-14,context-management-2025-06-27"
CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."
USER_AGENT = "claude-cli/1.0.0"
UPSTREAM_HOST = "api.anthropic.com"
UPSTREAM_PORT = 443


def log(*args):
    print(f"[{time.strftime('%H:%M:%S.%f')[:-3]}]", *args, flush=True)


def load_token(path):
    """Load fresh token each request.

    Priority:
    1. CLAUDE_OAUTH_TOKEN env var (long-lived `claude setup-token` output)
    2. `path` json file (short-lived from `claude login`, auto-refreshed)
    """
    env_token = os.environ.get("CLAUDE_OAUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    with open(path) as f:
        data = json.load(f)
    oauth = data.get("claudeAiOauth", data)
    token = oauth.get("accessToken")
    if not token:
        raise ValueError("no accessToken in credentials file")
    return token


def inject_claude_code_system(body):
    """Convert system to array-of-text-blocks with Claude Code identity FIRST.

    Anthropic OAuth 强制要求：system 字段必须是 array，第一个 block 必须 EXACTLY 是
    Claude Code identity prompt。如果传 string 含自定义内容会返回伪装的 429
    (rate_limit_error "Error")。
    """
    cc_block = {"type": "text", "text": CLAUDE_CODE_SYSTEM}
    existing = body.get("system")

    if existing is None or existing == "":
        body["system"] = [cc_block]
    elif isinstance(existing, str):
        # Strip leading CC identity if user already added it (avoid duplicate)
        stripped = existing
        if stripped.startswith(CLAUDE_CODE_SYSTEM):
            stripped = stripped[len(CLAUDE_CODE_SYSTEM):].lstrip()
        if stripped:
            body["system"] = [cc_block, {"type": "text", "text": stripped}]
        else:
            body["system"] = [cc_block]
    elif isinstance(existing, list):
        # Check if first block is already CC identity
        first = existing[0] if existing else None
        is_cc = isinstance(first, dict) and first.get("text", "").strip() == CLAUDE_CODE_SYSTEM
        if is_cc:
            body["system"] = existing
        else:
            # Filter out any non-leading CC blocks from middle (avoid duplicate)
            cleaned = [b for b in existing if not (isinstance(b, dict) and b.get("text", "").strip() == CLAUDE_CODE_SYSTEM)]
            body["system"] = [cc_block] + cleaned
    return body


def build_headers(token, body_length):
    return {
        "Host": UPSTREAM_HOST,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": OAUTH_BETAS,
        "anthropic-dangerous-direct-browser-access": "true",
        "user-agent": USER_AGENT,
        "Content-Length": str(body_length),
        "Connection": "close",
    }


def connect_upstream():
    """Open TLS connection to api.anthropic.com, optionally via HTTPS_PROXY."""
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")

    if proxy_url:
        m = _re.match(r"https?://([^:/]+):(\d+)", proxy_url)
        if not m:
            raise ValueError(f"bad HTTPS_PROXY: {proxy_url}")
        proxy_host, proxy_port = m.group(1), int(m.group(2))

        sock = socket.create_connection((proxy_host, proxy_port), timeout=30)
        connect_req = f"CONNECT {UPSTREAM_HOST}:{UPSTREAM_PORT} HTTP/1.1\r\nHost: {UPSTREAM_HOST}:{UPSTREAM_PORT}\r\n\r\n"
        sock.sendall(connect_req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("proxy closed during CONNECT")
            resp += chunk
        status_line = resp.split(b"\r\n", 1)[0]
        if b"200" not in status_line:
            raise ConnectionError(f"proxy CONNECT failed: {status_line!r}")
        log(f"  CONNECT tunnel via {proxy_url}")
    else:
        sock = socket.create_connection((UPSTREAM_HOST, UPSTREAM_PORT), timeout=30)

    ctx = ssl.create_default_context()
    tls_sock = ctx.wrap_socket(sock, server_hostname=UPSTREAM_HOST)
    return tls_sock


def forward_request(method, path, headers, body):
    """Send request to api.anthropic.com and return (status, headers, body_bytes)."""
    sock = connect_upstream()
    try:
        header_lines = f"{method} {path} HTTP/1.1\r\n"
        for k, v in headers.items():
            header_lines += f"{k}: {v}\r\n"
        header_lines += "\r\n"
        sock.sendall(header_lines.encode() + body)

        # Read full response
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk

        # Parse status + headers
        header_end = buf.index(b"\r\n\r\n")
        header_section = buf[:header_end].decode("utf-8", errors="replace")
        body_section = buf[header_end + 4:]

        lines = header_section.split("\r\n")
        status_parts = lines[0].split(" ", 2)
        status = int(status_parts[1])

        resp_headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                resp_headers[k.strip().lower()] = v.strip()

        # Handle chunked transfer encoding
        if resp_headers.get("transfer-encoding", "").lower() == "chunked":
            body_section = dechunk(body_section)

        return status, resp_headers, body_section
    finally:
        try:
            sock.close()
        except Exception:
            pass


def dechunk(data):
    """Decode HTTP chunked transfer encoding."""
    out = b""
    pos = 0
    while pos < len(data):
        # Find size line
        nl = data.find(b"\r\n", pos)
        if nl == -1:
            break
        size_hex = data[pos:nl].split(b";")[0].strip()
        try:
            size = int(size_hex, 16)
        except ValueError:
            break
        if size == 0:
            break
        pos = nl + 2
        out += data[pos:pos + size]
        pos += size + 2  # skip trailing CRLF
    return out


class Handler(BaseHTTPRequestHandler):
    token_file = None

    def log_message(self, fmt, *args):
        log(f"  {self.command} {self.path}")

    def do_GET(self):
        if self.path in ("/", "/health"):
            payload = b'{"status":"ok","service":"claude-oauth-proxy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)

    def do_POST(self):
        # Accept both /v1/messages and /messages (some clients strip the prefix)
        if self.path not in ("/v1/messages", "/messages"):
            self.send_error(404, f"unknown path {self.path}")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body_raw = self.rfile.read(content_length)

        try:
            body = json.loads(body_raw)
        except Exception as e:
            self.send_error(400, f"invalid JSON: {e}")
            return

        body = inject_claude_code_system(body)
        new_body = json.dumps(body).encode()

        try:
            token = load_token(self.__class__.token_file)
        except Exception as e:
            log(f"  token load failed: {e}")
            self.send_error(500, f"token load failed: {e}")
            return

        headers = build_headers(token, len(new_body))

        try:
            status, resp_headers, resp_body = forward_request(
                "POST", "/v1/messages", headers, new_body
            )
        except Exception as e:
            log(f"  upstream error: {e}")
            self.send_error(502, f"upstream error: {e}")
            return

        log(f"  -> {status} ({len(resp_body)}b)")

        self.send_response(status)
        for k, v in resp_headers.items():
            if k in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18789)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--token-file", default="/root/.claude/.credentials.json")
    args = parser.parse_args()

    # Token source: env CLAUDE_OAUTH_TOKEN or --token-file
    env_token = os.environ.get("CLAUDE_OAUTH_TOKEN", "").strip()
    if not env_token and not os.path.isfile(args.token_file):
        log(f"ERROR: no CLAUDE_OAUTH_TOKEN env AND no token file {args.token_file}")
        log("Set env CLAUDE_OAUTH_TOKEN=sk-ant-oat01-... OR run `claude login`.")
        sys.exit(1)

    try:
        token = load_token(args.token_file)
        masked = token[:15] + "..." + token[-4:]
        src = "env CLAUDE_OAUTH_TOKEN" if env_token else f"file {args.token_file}"
        log(f"Loaded token ({src}): {masked}")
    except Exception as e:
        log(f"ERROR: cannot load token: {e}")
        sys.exit(1)

    Handler.token_file = args.token_file
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    log(f"claude-oauth-proxy listening on {args.bind}:{args.port}")
    log(f"Test:  curl http://{args.bind}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
