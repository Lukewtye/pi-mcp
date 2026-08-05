# pi-mcp

An MCP server that exposes a small, controlled set of operations on a Raspberry Pi to an LLM client over authenticated HTTP.

Three tools: a read-only host health snapshot, a shell exec with output capping and UTF-8 hardening, and a single-container Docker restart with name validation. It runs as a systemd user unit bound to loopback, behind a bearer gate, reachable over a Tailscale private network.

This is a single-user homelab service, not a product. It is published because the debugging is more interesting than the code.


## Why it exists

The original deployment ran on a Mac as an MCP stdio server: the client spawned a local Python process and spoke to it over a pipe, which then SSH'd to the Pi for every call.

That transport wedged twice, with the same signature both times: a four-minute timeout, no log entry on either side, and no evidence the request ever reached Python. Several hypotheses were tested and eliminated, including client rebinding, competing MCP clients, an unanswered permission dialog, and broken SSH. None held.

Rather than keep chasing an intermittent failure with no reproduction, the fix was to remove the failure surface. HTTP has no stdin to reach EOF, no long-lived pipe to break, and no desktop-app process relaying bytes. The server moved onto the Pi itself and now speaks Streamable HTTP directly.

The wedge was never root-caused. This does not claim to have fixed it. It removes the component it lived in.


## Security model

Three layers, in order:

1. Bearer token. A shared secret in the `Authorization` header, minimum 32 characters. The server refuses to start without one. Middleware ordering puts this in front of everything else, so an unauthenticated request never reaches tool dispatch.
2. Host validation. The MCP SDK's DNS-rebinding protection is left enabled, with the public hostname supplied through an environment variable rather than hardcoded. Disabling it was considered and rejected: on a reachable endpoint it is a real second layer.
3. Network scope. Tailnet-only by default. Public exposure is opt-in, per-port, and off unless actively needed.

### What it does not do

- The token is static. No rotation, no expiry, no revocation list. Rotation currently means editing an environment file and restarting.
- No rate limiting on authentication attempts.
- Secrets live in a mode-600 environment file on an microSD.

Mitigations that were deliberately chosen over alternatives: the secret is never placed in a URL path (it would land in third-party config, transport logs, and error reports), and the token is transferred by pipe rather than retyped.


## Operations

A liveness monitor runs every 60 seconds on a systemd timer. It is a push monitor rather than an HTTP check, for a specific reason: the monitoring container sits on a Docker bridge network, while the service binds loopback. No container-side address can reach a loopback socket — including `host.docker.internal`, which resolves the host gateway correctly and still cannot connect. Inverting the direction, so a host-side script performs the check and pushes the result out, keeps the loopback bind intact and keeps the bearer token out of the monitoring system's database.

A healthy response is HTTP 406, not 200. A bare request with a valid token passes the bearer gate and is then refused by the MCP transport for lacking the right `Accept` header. That makes 406 a strictly better liveness signal than 200 would be: it proves authentication ran and the application is processing requests. A monitor configured to expect 200 would alarm continuously against a perfectly healthy service.


## Debugging notes

The parts worth reading.

A 421 that only appeared after authentication succeeded

Over the public URL, a request with a valid token returned `421 Misdirected Request`, while the same token over loopback returned the expected 406. An invalid token returned 401 on both.

That asymmetry was the clue, meaning something was rejecting the request after the auth layer accepted it. After reading the installed SDK source, when `transport_security` is unset and the bind host is loopback, FastMCP silently constructs a `TransportSecuritySettings` allowing only loopback `Host` headers. Tailscale forwards the original public hostname, which is not in that list, so the middleware rejects it.

Fixed by passing explicit settings with the public host appended from an environment variable, protection left on.

### A systemd unit file missing its section header

Running `systemd-analyze verify` on a newly added unit reported errors in a different file: the long-running service it declared an `After=` dependency on. That file had no `[Unit]` header, so `Description`, `After`, `Wants=network-online.target`, and both start-rate-limit directives had been silently discarded since the day it was written.

The service ran fine, because `[Service]` and `[Install]` were intact. Nothing about its behaviour looked wrong. It only surfaced because a new unit referenced it and the verifier followed the dependency graph.

Two lessons: `systemd-analyze verify` validates the graph, not just the file you name, and "it runs" is not evidence that a config file was parsed the way you intended.

### An unpinned install that fails at import

`pip install mcp uvicorn` without version pins resolves to `mcp` 2.0.0, which does not contain `FastMCP` at all — the 2.x line restructured the package. The failure is a hard `ModuleNotFoundError` at import, not a subtle behaviour change.

The repo carries both a `requirements.txt` naming the two direct dependencies and a `requirements.lock.txt` with the full 29-package resolved closure, generated on the deployment target rather than on a development machine running a different Python version. The lock file was validated by building a throwaway virtualenv from it and importing the two symbols the application actually needs.

## Layout

```
pi_mcp.py                        server and tool definitions
healthcheck.sh                   liveness probe, pushes to the monitoring system
requirements.txt                 direct dependencies, pinned
requirements.lock.txt            full resolved closure
systemd/pi-mcp.service           the service unit
systemd/pi-mcp-health.service    oneshot probe unit
systemd/pi-mcp-health.timer      60s schedule for the probe
```

## Running it

Requires Python 3.13, a virtualenv (Debian's Python is PEP 668-managed), and a Tailscale node.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt
```

Create an environment file, mode `600`:

```
PI_MODE=local
PI_MCP_TOKEN=<at least 32 characters, generated, not chosen>
PI_MCP_PUBLIC_HOST=<your-tailscale-hostname>:<port>
```

Install the units under `~/.config/systemd/user/`, adjusting paths, then:

```sh
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now pi-mcp.service
```

Expose it over Tailscale:

```sh
tailscale serve --bg --https=<port> 8799     # tailnet only
tailscale funnel --bg --https=<port> 8799    # public; understand the section above first
```

Verify:

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8799/mcp                          # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" \
     http://127.0.0.1:8799/mcp                                                              # 406
```

401 without a token and 406 with one is the correct result.

## Status

Working and in use. Known gaps: no command allowlist, and the original stdio wedge remains undiagnosed.
