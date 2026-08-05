#!/usr/bin/env python3
"""pi-mcp: an MCP server that bridges Claude to the Raspberry Pi.

Runs either on the Mac (SSH to the Pi) or on the Pi itself (local exec),
selected by PI_MODE. The Mac path needs Tailscale + SSH keys; the Pi path
needs neither, because the commands run in-process.

Transports:
  default        stdio (for claude_desktop_config.json)
  --http         Streamable HTTP on 127.0.0.1:--port, bearer-token gated.
                 Loopback binding is correct behind Tailscale Funnel:
                 Funnel terminates TLS and proxies to localhost.

Config (environment variables):
  PI_MODE         "ssh" (default) or "local". "local" runs commands on this
                  machine and ignores PI_HOST. Set it on the Pi.
  PI_HOST         SSH target: MagicDNS name, ssh-config alias, or user@host.
                  Default: "lukepi". Unused when PI_MODE=local.
  PI_SSH_TIMEOUT  Default per-command timeout in seconds. Default: 15
  PI_MCP_TOKEN    Bearer token. REQUIRED in --http mode, minimum 32 chars.
                  Generate with: python3 -c 'import secrets;print(secrets.token_urlsafe(32))'
"""

import argparse
import asyncio
import hmac
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PI_HOST = os.environ.get("PI_HOST", "lukepi")
PI_MODE = os.environ.get("PI_MODE", "ssh").strip().lower()
DEFAULT_TIMEOUT = int(os.environ.get("PI_SSH_TIMEOUT", "15"))
MAX_OUTPUT = 20_000  # chars kept per stream
MIN_TOKEN_LEN = 32

if PI_MODE not in ("ssh", "local"):
    sys.exit(f"PI_MODE must be 'ssh' or 'local', got {PI_MODE!r}")

# Docker's own name charset. Validating against it means the only regex
# metacharacter that can survive into a --filter expression is '.'.
_DOCKER_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")

_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]

_PUBLIC_HOST = os.environ.get("PI_MCP_PUBLIC_HOST", "").strip()

_allowed_hosts = list(_LOOPBACK_HOSTS)
_allowed_origins = list(_LOOPBACK_ORIGINS)
if _PUBLIC_HOST:
    _allowed_hosts += [_PUBLIC_HOST, f"{_PUBLIC_HOST}:*"]
    _allowed_origins += [f"https://{_PUBLIC_HOST}", f"https://{_PUBLIC_HOST}:*"]

mcp = FastMCP(
    "raspberry-pi",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_allowed_hosts,
        allowed_origins=_allowed_origins,
    ),
)


def _argv(command: str, timeout: int) -> list[str]:
    """Build the argv for one command in whichever mode is active.

    In ssh mode the command is passed as a single trailing argument and the
    remote login shell interprets it. `/bin/sh -c` reproduces that shell
    interpretation locally, so the two modes accept the same command strings.
    Caveat: the Pi's login shell is probably bash while /bin/sh is dash, so
    bashisms ([[ ]], process substitution) behave differently between modes.
    """
    if PI_MODE == "local":
        return ["/bin/sh", "-c", command]
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={min(timeout, 10)}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        PI_HOST,
        command,
    ]


def _run(command: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Run one command, locally or on the Pi. Never raises, never prompts."""
    host = "localhost" if PI_MODE == "local" else PI_HOST

    def fail(msg: str) -> dict:
        return {"host": host, "mode": PI_MODE, "exit_code": -1,
                "stdout": "", "stderr": msg, "truncated": False}

    try:
        # encoding= puts the streams in text mode; errors="replace" keeps
        # non-UTF-8 output from raising UnicodeDecodeError out of this call.
        # Explicit utf-8 avoids depending on whatever locale the parent had.
        p = subprocess.run(
            _argv(command, timeout),
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "host": host,
            "mode": PI_MODE,
            "exit_code": p.returncode,
            "stdout": p.stdout[-MAX_OUTPUT:],
            "stderr": p.stderr[-MAX_OUTPUT:],
            "truncated": len(p.stdout) > MAX_OUTPUT or len(p.stderr) > MAX_OUTPUT,
        }
    except subprocess.TimeoutExpired:
        return fail(f"command timed out after {timeout}s")
    except FileNotFoundError as e:
        return fail(f"executable not found: {e}")
    except Exception as e:  # nothing escapes into the MCP layer
        return fail(f"{type(e).__name__}: {e}")


@mcp.tool()
def pi_status() -> str:
    """Read-only health snapshot of the Raspberry Pi: uptime, load, memory,
    disk usage, CPU temperature, and running Docker containers."""
    cmd = (
        "echo '== uptime =='; uptime; "
        "echo '== memory =='; free -h; "
        "echo '== disk =='; df -h / 2>/dev/null; "
        "echo '== temp =='; (vcgencmd measure_temp 2>/dev/null "
        "|| awk '{printf \"%.1f C\\n\", $1/1000}' /sys/class/thermal/thermal_zone0/temp 2>/dev/null "
        "|| echo n/a); "
        "echo '== docker =='; (docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null "
        "|| echo 'docker unavailable')"
    )
    return json.dumps(_run(cmd, 15), indent=2)


@mcp.tool()
def pi_exec(command: str, timeout: int = 30) -> str:
    """Run a shell command on the Raspberry Pi and return exit code, stdout,
    and stderr as JSON. `timeout` is in seconds and is clamped to 1-120;
    larger values are silently reduced. Output is capped at 20,000 characters
    per stream."""
    timeout = max(1, min(int(timeout), 120))
    return json.dumps(_run(command, timeout), indent=2)


@mcp.tool()
def pi_docker_restart(container: str) -> str:
    """Restart a single Docker container by exact name, then return its fresh
    status. The name must match Docker's charset: alphanumeric first
    character, then alphanumerics, underscore, dot, or hyphen."""
    if not _DOCKER_NAME.match(container):
        return json.dumps(
            {"host": PI_HOST, "mode": PI_MODE, "exit_code": -1, "stdout": "",
             "stderr": f"invalid container name: {container!r}",
             "truncated": False},
            indent=2,
        )
    # `docker restart` already matches exactly. `docker ps --filter name=`
    # does not: it is a Go regexp substring match, so `name=n8n` also reports
    # `n8n-worker`. Anchor it. '.' is the only metacharacter the validation
    # above lets through, so escaping '.' is provably sufficient here.
    anchored = "name=^" + container.replace(".", r"\.") + "$"
    cmd = (
        f"docker restart {shlex.quote(container)} && "
        f"docker ps --filter {shlex.quote(anchored)} "
        f"--format '{{{{.Names}}}}\t{{{{.Status}}}}'"
    )
    return json.dumps(_run(cmd, 60), indent=2)


class _BearerAuth:
    """Pure-ASGI gate in front of the MCP app. Rejects anything without a
    matching Authorization header before it reaches the session manager.

    Non-HTTP scopes pass straight through. That matters: the Starlette app
    returned by streamable_http_app() carries a lifespan that starts the
    session manager, and swallowing lifespan messages here would leave the
    server accepting requests it cannot serve.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        supplied = b""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                supplied = value
                break

        if not hmac.compare_digest(supplied, self._expected):
            body = b'{"error":"unauthorized"}'
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def _tool_names() -> list[str]:
    """Ask the FastMCP registry rather than keeping a parallel list by hand."""
    try:
        return [t.name for t in asyncio.run(mcp.list_tools())]
    except Exception as e:
        return [f"<could not enumerate: {type(e).__name__}: {e}>"]


def main() -> None:
    ap = argparse.ArgumentParser(description="MCP bridge to the Raspberry Pi")
    ap.add_argument("--http", action="store_true",
                    help="serve Streamable HTTP on 127.0.0.1 instead of stdio")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--selftest", action="store_true",
                    help="print config and registered tools, then exit")
    args = ap.parse_args()

    if args.selftest:
        print(f"PI_MODE={PI_MODE}")
        print(f"PI_HOST={PI_HOST}" if PI_MODE == "ssh" else "PI_HOST=(unused)")
        print(f"ssh binary: {shutil.which('ssh') or 'MISSING'}"
              if PI_MODE == "ssh" else "ssh binary: (not needed)")
        token = os.environ.get("PI_MCP_TOKEN", "")
        print(f"PI_MCP_TOKEN: {'set, ' + str(len(token)) + ' chars' if token else 'UNSET'}")
        print(f"tools: {', '.join(_tool_names())}")
        sys.exit(0)

    if args.http:
        token = os.environ.get("PI_MCP_TOKEN", "")
        if len(token) < MIN_TOKEN_LEN:
            sys.exit(
                f"PI_MCP_TOKEN must be set to at least {MIN_TOKEN_LEN} characters "
                f"before --http will start. This tool is full shell access; "
                f"refusing to serve it unauthenticated."
            )
        try:
            import uvicorn
        except ImportError:
            sys.exit("uvicorn is required for --http: .venv/bin/pip install uvicorn")

        mcp.settings.host = "127.0.0.1"  # never bind beyond loopback
        mcp.settings.port = args.port
        app = _BearerAuth(mcp.streamable_http_app(), token)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
