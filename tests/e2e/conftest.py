"""Fixtures for the e2e suite: a real airship.py subprocess, driven over real
HTTPS with httpx, with fakes standing in for `tailscale`, `varlock`, `curl`,
`xcrun`, and `security`.

See tests/e2e/fake_tailscale.py, fake_varlock.py, fake_curl.py, and
fake_forbidden.py for what each fake does and why. Nothing here ever reads or
writes real Tailscale state, real certs, a real machine's
`$TMPDIR/airship-instance-*.json`, a real `.env.local`, or the real network —
every path is rooted under a pytest tmp_path, `TMPDIR` itself is redirected
into that same isolated area for the child (so airship's own internal
staging dir can never land in the real `$TMPDIR` either), and the fake
`varlock`/`curl` block anything that would reach Pushover's real API before
it ever leaves the machine.

httpx is imported lazily (inside functions), not at module level: this file
is a conftest.py for the WHOLE tests/ tree, and importing httpx eagerly here
would break the README's default `pytest tests/` command for anyone who
hasn't added `--with httpx` (which the default command intentionally
doesn't, since only the e2e tests need it).
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AIRSHIP_PY = REPO_ROOT / "airship.py"
E2E_DIR = Path(__file__).resolve().parent
FAKE_SOURCES = {
    "tailscale": E2E_DIR / "fake_tailscale.py",
    "varlock": E2E_DIR / "fake_varlock.py",
    "curl": E2E_DIR / "fake_curl.py",
    "xcrun": E2E_DIR / "fake_forbidden.py",
    "security": E2E_DIR / "fake_forbidden.py",
}

BUNDLE_ID = "com.airship.e2e"
BUNDLE_VERSION = "42"
APP_TITLE = "E2E"

# Blackholed on purpose (item 2): port 9 ("discard") on loopback refuses
# every connection instantly, so anything that tries to honor a proxy env var
# fails fast and loud instead of hanging or silently reaching the network.
DEAD_PROXY = "http://127.0.0.1:9"


def _free_tcp_port() -> int:
    """An ephemeral port that's free right now. Small TOCTOU race is fine for
    tests: the process that binds it next is airship.py or one of our fakes,
    started immediately after this returns."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_ipa_bytes(bundle_id: str = BUNDLE_ID, version: str = BUNDLE_VERSION) -> bytes:
    """A minimal but valid .ipa: a zip with Payload/<App>.app/Info.plist
    carrying a bundle id and version, matching what read_ipa_metadata()
    requires (Payload/*.app/Info.plist, one level deep)."""
    import io

    info = {
        "CFBundleIdentifier": bundle_id,
        "CFBundleVersion": version,
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": APP_TITLE,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"Payload/{APP_TITLE}.app/Info.plist", plistlib.dumps(info))
        zf.writestr(f"Payload/{APP_TITLE}.app/{APP_TITLE}", b"not a real binary, just fixture bytes")
    return buf.getvalue()


@pytest.fixture
def fixture_ipa(tmp_path: Path) -> Path:
    """A fresh, deterministic .ipa file for one test."""
    ipa_path = tmp_path / "E2E.ipa"
    ipa_path.write_bytes(build_ipa_bytes())
    return ipa_path


@pytest.fixture(scope="session")
def self_signed_cert(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A throwaway self-signed cert+key for "localhost", generated once per
    test session with the system `openssl` (present on macOS; airship.py
    itself never touches this — it's only used by fake_tailscale.py's TLS
    proxy, and passed through to the real curl for loopback probes via
    CURL_CA_BUNDLE). Never written anywhere near a real cert store."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not found on PATH — required to generate the e2e TLS cert")
    d = tmp_path_factory.mktemp("e2e-cert")
    cert = d / "cert.pem"
    key = d / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


@pytest.fixture(scope="session")
def fake_bin(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory containing executables named `tailscale`, `varlock`,
    `curl`, `xcrun`, and `security` that airship.py's subprocess calls
    resolve to when this directory is put first on PATH. See the individual
    fake_*.py sources for what each one does."""
    d = tmp_path_factory.mktemp("e2e-fakebin")
    for name, src in FAKE_SOURCES.items():
        target = d / name
        shutil.copy(src, target)
        target.chmod(0o755)
    return d


class AirshipInstance:
    """A live airship.py subprocess, reachable over real HTTPS through the
    fake tailscale TLS proxy."""

    def __init__(
        self,
        proc: subprocess.Popen,
        https_port: int,
        instance_dir: Path,
        cert_path: Path,
        stdout_path: Path,
        forbidden_marker: Path,
        child_tmp_dir: Path,
    ) -> None:
        import httpx  # lazy: see module docstring

        self.proc = proc
        self.https_port = https_port
        self.base_url = f"https://localhost:{https_port}"
        self.instance_dir = instance_dir
        self.instance_path = instance_dir / f"airship-instance-{https_port}.json"
        self.cert_path = cert_path
        self.stdout_path = stdout_path
        self.forbidden_marker = forbidden_marker
        self.child_tmp_dir = child_tmp_dir
        import ssl

        ssl_ctx = ssl.create_default_context(cafile=str(cert_path))
        self.client = httpx.Client(verify=ssl_ctx, timeout=10.0)

    def stdout(self) -> str:
        return self.stdout_path.read_text(errors="replace")

    def forbidden_calls(self) -> list[dict]:
        """Every call our fake varlock/curl/xcrun/security intercepted
        instead of letting reach a real credential or the real network."""
        try:
            text = self.forbidden_marker.read_text()
        except OSError:
            return []
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def wait_ready(self, timeout: float = 30.0) -> None:
        """Wait for the FULL handoff sequence to finish, not just the first
        200. airship's local HTTP server (and the fake tailscale TLS proxy in
        front of it) are already answering requests well before write_instance
        / _print_handoff / probe_landing / notify_ready run — those happen
        between start_serve() and the final "Serving —" print in run(). A
        wait that stops at the first 200 races all four of those, and a test
        that then immediately calls stop() can SIGINT mid-handoff. Wait for
        the "Serving —" line first (proof the whole sequence completed), then
        confirm the HTTPS endpoint answers.
        """
        deadline = time.monotonic() + timeout
        printed_serving = False
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"airship.py exited early (code {self.proc.returncode}) "
                    f"before serving:\n{self.stdout()}"
                )
            if "Serving —" in self.stdout():
                printed_serving = True
                break
            time.sleep(0.05)
        if not printed_serving:
            raise TimeoutError(
                f"airship.py never printed 'Serving —' within {timeout}s "
                f"(write_instance/_print_handoff/probe_landing/notify_ready "
                f"never completed):\n{self.stdout()}"
            )

        last_exc: Exception | None = None
        deadline = time.monotonic() + timeout
        import httpx

        while time.monotonic() < deadline:
            try:
                resp = self.client.get(f"{self.base_url}/")
                if resp.status_code == 200:
                    return
                last_exc = RuntimeError(f"unexpected status {resp.status_code}")
            except httpx.TransportError as exc:
                last_exc = exc
            time.sleep(0.05)
        raise TimeoutError(
            f"airship.py printed 'Serving —' but {self.base_url} never "
            f"answered: {last_exc}\nstdout so far:\n{self.stdout()}"
        )

    def wait_instance_file(self, present: bool, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.instance_path.exists() == present:
                return True
            time.sleep(0.1)
        return self.instance_path.exists() == present

    def stop(self, timeout: float = 15.0) -> int:
        """Graceful shutdown via SIGINT — the same signal README.md documents
        as the safe way to stop a backgrounded airship (SIGTERM only reaches
        uv's wrapper and leaves the real cleanup unrun; uv forwards SIGINT to
        the python process it runs). If that doesn't finish within `timeout`
        (a hung or misbehaving child), fall back to killing the WHOLE process
        group — the subprocess was started with start_new_session=True
        precisely so this can't leave uv, the real python airship process, or
        the fake tailscale TLS proxy child orphaned. A forced kill skips
        airship's own `finally:` cleanup, so we also sweep the staging dir(s)
        it would have removed — safe because TMPDIR was redirected into our
        own per-instance directory for this child, never the real $TMPDIR.
        """
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                for leftover in self.child_tmp_dir.glob("airship-*"):
                    shutil.rmtree(leftover, ignore_errors=True)
        self.client.close()
        return self.proc.returncode


@pytest.fixture
def airship_factory(
    tmp_path_factory: pytest.TempPathFactory,
    self_signed_cert: tuple[Path, Path],
    fake_bin: Path,
):
    """Factory fixture: call it to launch a real airship.py subprocess bound
    to 127.0.0.1, published over real HTTPS by the fake tailscale proxy, with
    every path isolated to this test. Tracks every instance it starts and
    stops (SIGINT, then force-kill-the-process-group as a fallback) any
    still running at teardown, so a failing test can never leak a process."""
    cert_path, key_path = self_signed_cert
    instances: list[AirshipInstance] = []

    def _spawn(
        ipa_path: Path,
        https_port: int | None = None,
        extra_env: dict[str, str] | None = None,
        extra_args: list[str] | None = None,
        airship_path: Path = AIRSHIP_PY,
    ) -> AirshipInstance:
        https_port = https_port if https_port is not None else _free_tcp_port()
        work_dir = tmp_path_factory.mktemp("e2e-run")
        instance_dir = work_dir / "instances"
        instance_dir.mkdir()
        child_tmp_dir = work_dir / "tmp"
        child_tmp_dir.mkdir()
        stdout_path = work_dir / "stdout.log"
        forbidden_marker = work_dir / "forbidden-calls.jsonl"

        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
        env["AIRSHIP_INSTANCE_DIR"] = str(instance_dir)
        # AIRSHIP_PREFERRED_PORT: always set to a port WE chose and don't hold
        # open, unless the test overrides it (the free-port-fallback test
        # holds its own chosen port open via extra_env). Live real ships on
        # this machine hold 4190/4443/4444 — never let a test instance's
        # local server try to bind those, or its own https_port claim walk
        # the same AUTO_HTTPS_PORTS a real ship might be using. (--https-port
        # here is always our own random free port, so the AUTO_HTTPS_PORTS
        # walk is never exercised regardless.)
        env.setdefault("AIRSHIP_PREFERRED_PORT", str(_free_tcp_port()))
        # TMPDIR: redirects airship's OWN internal staging dir
        # (tempfile.mkdtemp(prefix="airship-") in stage_artifacts) and the
        # ServeChild log tempfile into our per-instance directory, so even a
        # forced kill (see AirshipInstance.stop) can never leave debris in
        # the real machine's $TMPDIR.
        env["TMPDIR"] = str(child_tmp_dir)
        env["AIRSHIP_TEST_TAILSCALE_STATE"] = str(work_dir / "tailscale-state.json")
        env["AIRSHIP_TEST_TAILSCALE_CERT"] = str(cert_path)
        env["AIRSHIP_TEST_TAILSCALE_KEY"] = str(key_path)
        env["AIRSHIP_TEST_TAILSCALE_DNSNAME"] = "localhost."
        env["AIRSHIP_TEST_TAILSCALE_IP"] = "100.64.0.1"
        # Item 1/9: fake varlock/curl/xcrun/security record anything they
        # intercept here instead of reaching a real credential or network.
        env["AIRSHIP_TEST_FORBIDDEN_MARKER"] = str(forbidden_marker)
        # Loopback probes (probe_landing against our own fake tailscale proxy)
        # go through the REAL curl via fake_curl.py's passthrough, which needs
        # to trust our self-signed cert.
        env["CURL_CA_BUNDLE"] = str(cert_path)
        # Item 2: block real network access and uv's own network use from the
        # child entirely. Port 9 (discard) on loopback refuses connections
        # instantly, so anything that tries to honor these fails fast.
        env["HTTP_PROXY"] = DEAD_PROXY
        env["HTTPS_PROXY"] = DEAD_PROXY
        env["ALL_PROXY"] = DEAD_PROXY
        env["http_proxy"] = DEAD_PROXY
        env["https_proxy"] = DEAD_PROXY
        env["all_proxy"] = DEAD_PROXY
        env["NO_PROXY"] = "127.0.0.1,localhost"
        env["no_proxy"] = "127.0.0.1,localhost"
        env["UV_OFFLINE"] = "1"
        if extra_env:
            env.update(extra_env)

        argv = [
            "uv", "run", "--script", str(airship_path),
            str(ipa_path), "--stay", "--https-port", str(https_port),
        ]
        if extra_args:
            argv += extra_args

        with open(stdout_path, "w") as out:
            proc = subprocess.Popen(
                argv,
                stdout=out,
                stderr=subprocess.STDOUT,
                cwd=str(work_dir),
                env=env,
                start_new_session=True,  # own process group; see stop()
            )
        inst = AirshipInstance(
            proc, https_port, instance_dir, cert_path, stdout_path,
            forbidden_marker, child_tmp_dir,
        )
        instances.append(inst)
        return inst

    yield _spawn

    for inst in instances:
        inst.stop()


@pytest.fixture
def airship_copy_with_dummy_env(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway COPY of airship.py in its own directory, next to a dummy
    (never real) `.env.local`. notify_ready() resolves its env dir from
    `Path(__file__).resolve().parent` — i.e. wherever the running airship.py
    file actually lives — so this is how the pushover-isolation test proves
    the fake varlock/curl protection holds even when a real-looking
    `.env.local` sits right next to the script, without ever going near the
    real checkout's real `.env.local` (which this suite must never read)."""
    d = tmp_path_factory.mktemp("e2e-airship-copy")
    copied = d / "airship.py"
    shutil.copy(AIRSHIP_PY, copied)
    copied.chmod(0o755)
    (d / ".env.local").write_text(
        "PUSHOVER_TOKEN=dummy-not-a-real-token\nPUSHOVER_USER=dummy-not-a-real-user\n"
    )
    return copied
