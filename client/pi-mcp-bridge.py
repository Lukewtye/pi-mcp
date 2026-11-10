#!/usr/bin/env python3
"""stdio <-> Streamable HTTP bridge so Claude Desktop can reach pi-mcp.

Claude Desktop only launches local stdio servers and cannot attach custom
headers to remote ones. This process speaks stdio to Desktop and forwards each
JSON-RPC message to the Pi over HTTPS with the headers produced by the same
helper Claude Code uses (~/.claude/pi-mcp-headers.sh), so no secret ever lands
in claude_desktop_config.json, argv, or a log.

Stdlib only. http.client never follows redirects, so headers cannot leak to
another host via a 3xx.

Env overrides:
  PI_MCP_URL          default https://mcp.luketye.dev/mcp
  PI_MCP_HEADERS_CMD  default ~/.claude/pi-mcp-headers.sh (must print a JSON object)
  PI_MCP_TIMEOUT      seconds, default 180 (pi_exec caps at 120)
"""
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.parse

URL = os.environ.get("PI_MCP_URL", "https://mcp.luketye.dev/mcp")
HEADERS_CMD = os.environ.get(
    "PI_MCP_HEADERS_CMD", os.path.expanduser("~/.claude/pi-mcp-headers.sh")
)
TIMEOUT = float(os.environ.get("PI_MCP_TIMEOUT", "180"))

_u = urllib.parse.urlsplit(URL)
HTTPS = _u.scheme == "https"
HOST, PORT = _u.hostname, _u.port
PATH = (_u.path or "/") + (f"?{_u.query}" if _u.query else "")

out_lock = threading.Lock()
state_lock = threading.Lock()
reinit_lock = threading.Lock()
headers = {}
session_id = None
protocol_version = None
init_msg = None
reinit_count = 0


def log(text):
    print(f"[pi-mcp-bridge] {text}", file=sys.stderr, flush=True)


# Control characters, and the RFC 9110 token charset for header names.
_CTL = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def load_headers():
    """Run the helper and validate its output.

    A value that http.client would reject must fail here, at startup.
    http.client puts the full rejected value in its ValueError message,
    and that message would otherwise reach the Desktop log and the
    JSON-RPC error. Error messages below name the header, never its value.
    """
    r = subprocess.run(HEADERS_CMD, shell=True, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(f"headers helper exited {r.returncode}: {r.stderr.strip()[:200]}")
    h = json.loads(r.stdout)
    if not isinstance(h, dict) or not h:
        raise RuntimeError("headers helper did not print a non-empty JSON object")
    out = {}
    for k, v in h.items():
        if not _HEADER_NAME.fullmatch(k):
            raise RuntimeError(f"headers helper printed an invalid header name ({len(k)} chars)")
        if not isinstance(v, str):
            raise RuntimeError(f"header {k!r}: value is not a string")
        if _CTL.search(v):
            raise RuntimeError(f"header {k!r} contains control characters")
        if not v.strip() or (k.lower() == "authorization" and not v.partition(" ")[2].strip()):
            raise RuntimeError(f"header {k!r} is empty")
        try:
            v.encode("latin-1")  # the encoding http.client uses for header values
        except UnicodeEncodeError:
            raise RuntimeError(f"header {k!r} contains non-Latin-1 characters") from None
        out[k] = v
    return out


def redact(text):
    """Remove header values from text: raw, str-repr, bytes-repr, and the bare token.

    Backstop for error messages that quote a header. The repr forms matter:
    a ValueError shows a newline as the two characters backslash-n, so a
    plain search for the raw value would miss it.
    """
    text = str(text)
    forms = set()
    for v in list(headers.values()):
        parts = [v, v.partition(" ")[2]]
        parts += re.split(r"[\x00-\x20\x7f]+", v)  # pieces around spaces and control chars
        for p in filter(None, parts):
            forms.add(p)
            forms.add(repr(p)[1:-1])
            forms.add(repr(p.encode("latin-1", "backslashreplace"))[2:-1])
    for f in sorted((f for f in forms if len(f) >= 8), key=len, reverse=True):
        text = text.replace(f, "<redacted>")
    return text


def emit(obj):
    line = json.dumps(obj, separators=(",", ":"))
    with out_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def is_request(msg):
    return isinstance(msg, dict) and "method" in msg and "id" in msg


def is_init(msg):
    return isinstance(msg, dict) and msg.get("method") == "initialize"


def fail(msg, text):
    """Answer a request with a JSON-RPC error so Desktop never waits forever."""
    if is_request(msg):
        emit({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32000, "message": text}})


def open_conn():
    cls = http.client.HTTPSConnection if HTTPS else http.client.HTTPConnection
    return cls(HOST, PORT, timeout=TIMEOUT)


def post(msg):
    h = dict(headers)
    h["Content-Type"] = "application/json"
    h["Accept"] = "application/json, text/event-stream"
    with state_lock:
        sid = session_id
        if sid and not is_init(msg):
            h["Mcp-Session-Id"] = sid
        if protocol_version and not is_init(msg):
            h["MCP-Protocol-Version"] = protocol_version
    conn = open_conn()
    conn.request("POST", PATH, body=json.dumps(msg).encode(), headers=h)
    return conn, conn.getresponse(), sid


def relay(resp, on_msg):
    ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
    if ctype == "application/json":
        raw = resp.read()
        data = json.loads(raw) if raw else None
        for m in data if isinstance(data, list) else [data]:
            if m is not None:
                on_msg(m)
    elif ctype == "text/event-stream":
        buf = []
        for raw in resp:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                if buf:
                    on_msg(json.loads("\n".join(buf)))
                    buf = []
            elif line.startswith("data:"):
                v = line[5:]
                buf.append(v[1:] if v.startswith(" ") else v)
        if buf:
            on_msg(json.loads("\n".join(buf)))
    else:
        resp.read()
        log(f"unexpected Content-Type {ctype!r}")


def capture_init(resp, req_id):
    """Record session id and negotiated protocol version from an initialize reply."""
    global session_id

    with state_lock:
        session_id = resp.getheader("Mcp-Session-Id")

    def on_msg(m, forward):
        global protocol_version
        if m.get("id") == req_id and isinstance(m.get("result"), dict):
            with state_lock:
                protocol_version = m["result"].get("protocolVersion")
        if forward:
            emit(m)

    return on_msg


def reinit(stale_sid):
    """Server dropped our session (Pi restart, idle reap). Replay the handshake once."""
    global reinit_count, session_id
    with reinit_lock:
        with state_lock:
            if session_id != stale_sid:
                return  # another thread already did it
            session_id = None
        if init_msg is None:
            raise RuntimeError("session lost before initialize was ever seen")
        reinit_count += 1
        replay = dict(init_msg, id=f"bridge-reinit-{reinit_count}")
        conn, resp, _ = post(replay)
        try:
            if resp.status != 200:
                raise RuntimeError(f"re-initialize got HTTP {resp.status}")
            cb = capture_init(resp, replay["id"])
            relay(resp, lambda m: cb(m, forward=False))
        finally:
            conn.close()
        conn, resp, _ = post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        resp.read()
        conn.close()
        log("session re-established")


def handle(msg, retry=True):
    global headers
    label = msg.get("method", "response") if isinstance(msg, dict) else "batch"
    try:
        conn, resp, sid = post(msg)
        try:
            if resp.status == 200:
                if is_init(msg):
                    cb = capture_init(resp, msg.get("id"))
                    relay(resp, lambda m: cb(m, forward=True))
                else:
                    relay(resp, emit)
                return
            if resp.status == 202:
                resp.read()
                return
            resp.read(2048)
            if resp.status == 404 and sid and retry and not is_init(msg):
                log(f"session gone (404) on {label}; re-initializing")
                reinit(sid)
                return handle(msg, retry=False)
            if resp.status in (401, 403) and retry:
                log(f"HTTP {resp.status} on {label}; reloading headers from helper")
                headers = load_headers()
                return handle(msg, retry=False)
            if 300 <= resp.status < 400:
                log(f"HTTP {resp.status} redirect on {label}; refusing to follow")
            else:
                log(f"HTTP {resp.status} on {label}")
            fail(msg, f"pi-mcp bridge: upstream HTTP {resp.status}")
        finally:
            conn.close()
    except Exception as e:  # never let a thread die silently with Desktop waiting
        text = redact(f"{type(e).__name__}: {e}")
        log(f"{label}: {text}")
        fail(msg, f"pi-mcp bridge: {text}")


def shutdown(*_):
    """Tell the server to drop the session (mcp<1.30 never reaps idle sessions)."""
    with state_lock:
        sid = session_id
    if sid:
        try:
            conn = open_conn()
            conn.request("DELETE", PATH, headers=dict(headers, **{"Mcp-Session-Id": sid}))
            conn.getresponse().read()
            conn.close()
        except Exception as e:
            log(f"session DELETE failed: {type(e).__name__}")
    os._exit(0)


def main():
    global headers, init_msg
    try:
        headers = load_headers()
    except Exception as e:
        log(f"cannot load headers: {type(e).__name__}: {e}")
        sys.exit(1)
    log(f"forwarding to {URL} with {len(headers)} header(s): {', '.join(sorted(headers))}")
    signal.signal(signal.SIGTERM, shutdown)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            log("dropped a non-JSON line from stdin")
            continue
        if is_init(msg):
            init_msg = msg
            handle(msg)  # synchronous: later calls need the session id
        elif is_request(msg):
            # requests run concurrently so a 120 s pi_exec does not block pings
            threading.Thread(target=handle, args=(msg,), daemon=True).start()
        else:
            handle(msg)  # notifications/responses keep their order
    shutdown()


if __name__ == "__main__":
    main()
