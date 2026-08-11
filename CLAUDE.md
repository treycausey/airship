# airship — shared OTA install tool

airship is a standalone, single-file Python CLI (`airship.py`, run via
`uv run`) that installs a finished iOS `.ipa` onto an iPhone over the air via
Tailscale. It is not owned by one app — other projects depend on it as their
device-install path. mise uses it today; flusso is adopting it.

Read `README.md` for full usage and `docs/2026-06-24-airship-design.md` for
the original design rationale before changing behavior. This file states the
contract other projects rely on — do not break it silently.

## Contract for consuming projects

- **Input is a signed `.ipa`, never a bare `.app`.** airship reads
  `CFBundleIdentifier` / `CFBundleVersion` / display name out of the IPA's
  `Info.plist` and validates it is ad-hoc/development signed (or enterprise
  in-house with `ProvisionsAllDevices`) for the target iPhone's UDID. Package
  a `.xcarchive`'s `Products/Applications/*.app` into a `Payload/` dir and zip
  it — do not hand airship the `.app` directly.
- **Serves from `127.0.0.1`, published via `tailscale serve`.** The local HTTP
  server intentionally binds loopback only; Tailscale Serve is what makes it
  reachable tailnet-wide with a real HTTPS cert. Never point a caller at the
  local port directly — the phone must use the `https://<node>.ts.net/` URL
  airship prints.
- **Auto-exits 45s after the phone finishes downloading.** No cleanup step is
  needed by the caller; airship tears down its own Serve mapping and temp
  staging dir on exit (and never runs a node-wide `tailscale serve reset`).
  Pass `--stay` to keep it serving indefinitely instead (e.g. for scripted or
  unattended flows where the caller controls the Ctrl-C).
- **`--https-port N`** picks a different Tailscale Serve HTTPS port. Use this
  when a caller's own project already owns `/` on 443, or when a service
  worker from another app has claimed that origin and would intercept the
  install page — a different port is a different origin. Default is defined
  by `DEFAULT_HTTPS_PORT` in `airship.py`.
- **CLI surface is exactly three args**: positional `ipa` (optional — defaults
  to the newest `.ipa` under the current directory), `--stay`, `--https-port`.
  Treat this as the stable interface; anything else is an implementation
  detail of `airship.py`.

## Cross-project usage

The `ios-ship` and `ios-deploy-policy` skills (in `~/.claude/skills/`) encode
how other projects are expected to invoke airship end to end — keychain
preflight, packaging the `.ipa` from an `.xcarchive`, XcodeGen regeneration,
Tailscale-up checks, and the airship handoff itself. Consult those skills
before wiring a new project's device-install flow instead of re-deriving the
sequence here.

## Development

- Single-file `uv` inline-script (`# /// script` header declares
  `requires-python` and the one dependency, `segno`). Run directly as
  `./airship.py`; no separate install step.
- Tests: `uv run --with pytest --with segno pytest tests/` (see README's
  Development section). Fixtures live under `tests/fixtures/`, including a
  synthetic unsigned `.ipa` built from a real iOS binary's `Info.plist`.
- This file (`file-length: accept` marker at the top of `airship.py`) is
  deliberately kept as one file — do not split it into a package without
  discussing the tradeoff first.
