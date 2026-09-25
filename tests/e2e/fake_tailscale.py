#!/usr/bin/env python3
"""Fake `tailscale` CLI for the e2e suite.

Real airship.py shells out to the literal command `tailscale` (resolved via
PATH) for four things: `status --json` (this node's DNS name), `ip [-4]`
(this node's tailnet IP(s)), `serve status --json` (what currently maps `/`),
and `serve [--https=N] <port>` (the long-lived foreground process that is
supposed to make `https://<node>/` proxy to `127.0.0.1:<port>`).

This script stands in for all four so the e2e suite can drive a real
airship.py subprocess over real HTTPS without touching actual Tailscale,
actual certs, or a real tailnet. `serve` does the one thing airship actually
depends on: it terminates TLS (using the self-signed cert the test fixture
built) on `--https=<port>` and proxies raw bytes to `127.0.0.1:<local-port>`,
exactly like `tailscale serve` proxying to a local port. State is shared
between the long-lived `serve` process and one-shot `serve status --json`
calls via a small JSON file on disk (path in AIRSHIP_TEST_TAILSCALE_STATE).

Configuration, all via environment variables set by the test fixture that
launches airship.py (and therefore inherited by every `tailscale` subprocess
airship spawns):

  AIRSHIP_TEST_TAILSCALE_STATE     path to the shared JSON state file
  AIRSHIP_TEST_TAILSCALE_CERT      PEM cert for the TLS proxy
  AIRSHIP_TEST_TAILSCALE_KEY       PEM key for the TLS proxy
  AIRSHIP_TEST_TAILSCALE_DNSNAME   fake `Self.DNSName` (default "localhost.")
  AIRSHIP_TEST_TAILSCALE_IP        fake tailnet IP (default "100.64.0.1")
"""

from __future__ import annotations

import json
import os
import signal
import socket
import ssl
import sys
import threading
import time

STATE = os.environ.get("AIRSHIP_TEST_TAILSCALE_STATE")
CERT = os.environ.get("AIRSHIP_TEST_TAILSCALE_CERT")
KEY = os.environ.get("AIRSHIP_TEST_TAILSCALE_KEY")
DNSNAME = os.environ.get("AIRSHIP_TEST_TAILSCALE_DNSNAME", "localhost.")
FAKE_IP = os.environ.get("AIRSHIP_TEST_TAILSCALE_IP", "100.64.0.1")
FAKE_HOST = "fake-e2e.ts.net"


def _read_state() -> dict:
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(data: dict) -> None:
    tmp = f"{STATE}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE)


def cmd_status_json() -> None:
    print(json.dumps({"Self": {"DNSName": DNSNAME}}))


def cmd_ip() -> None:
    print(FAKE_IP)


def cmd_serve_status_json() -> None:
    st = _read_state()
    if not st:
        print(json.dumps({}))
        return
    host = f"{FAKE_HOST}:{st['https_port']}"
    out = {
        "Foreground": {
            "e2e-session": {
                "Web": {
                    host: {
                        "Handlers": {
                            "/": {"Proxy": f"http://127.0.0.1:{st['local_port']}"}
                        }
                    }
                }
            }
        }
    }
    print(json.dumps(out))


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(conn: socket.socket, ctx: ssl.SSLContext, local_port: int) -> None:
    try:
        tls = ctx.wrap_socket(conn, server_side=True)
    except (ssl.SSLError, OSError):
        conn.close()
        return
    try:
        backend = socket.create_connection(("127.0.0.1", local_port), timeout=10)
    except OSError:
        tls.close()
        return
    t1 = threading.Thread(target=_pipe, args=(tls, backend), daemon=True)
    t2 = threading.Thread(target=_pipe, args=(backend, tls), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    for s in (tls, backend):
        try:
            s.close()
        except OSError:
            pass


def run_proxy(https_port: int, local_port: int) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", https_port))
    sock.listen(64)
    sock.settimeout(0.5)

    _write_state({"https_port": https_port, "local_port": local_port, "pid": os.getpid()})

    stopping = threading.Event()

    def on_signal(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def accept_loop():
        while not stopping.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=_handle, args=(conn, ctx, local_port), daemon=True
            ).start()

    acceptor = threading.Thread(target=accept_loop, daemon=True)
    acceptor.start()
    try:
        while not stopping.is_set():
            time.sleep(0.1)
    finally:
        stopping.set()
        try:
            sock.close()
        except OSError:
            pass
        acceptor.join(timeout=2)
        # Only clear the mapping if it's still ours — a takeover may already
        # have overwritten it with a newer process's record.
        cur = _read_state()
        if cur.get("pid") == os.getpid():
            _write_state({})


def main(argv: list[str]) -> int:
    if not argv:
        print("fake tailscale: no subcommand", file=sys.stderr)
        return 1
    if argv[0] == "status" and "--json" in argv:
        cmd_status_json()
        return 0
    if argv[0] == "ip":
        cmd_ip()
        return 0
    if argv[0] == "serve":
        rest = argv[1:]
        if rest[:1] == ["status"] and "--json" in rest:
            cmd_serve_status_json()
            return 0
        https_port = None
        local_port = None
        for a in rest:
            if a.startswith("--https="):
                https_port = int(a.split("=", 1)[1])
            else:
                local_port = int(a)
        if https_port is None or local_port is None:
            print(
                "fake tailscale: e2e fixture requires an explicit "
                f"--https-port (got {rest!r})",
                file=sys.stderr,
            )
            return 1
        run_proxy(https_port, local_port)
        return 0
    print(f"fake tailscale: unhandled subcommand {argv!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
