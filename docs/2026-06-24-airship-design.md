# airship — over-the-air iPhone installer

**Date:** 2026-06-24
**Status:** Approved, codex-reviewed, building

## Problem

Install finished iOS app builds (`.ipa`) onto my iPhone without a cable and
without being on the same WiFi network. Both my Mac Studio and iPhone are on the
same Tailscale tailnet, so the tool should route the install over Tailscale.

## Goals

- Point the tool at any `.ipa` and get it installed on the iPhone over the air.
- Works from anywhere both devices are on the tailnet (no shared LAN, no USB).
- Build-type agnostic: consumes a finished `.ipa`, never builds one.
- Local and private: no third-party upload services, no accounts beyond what I
  already have (Apple Developer + Tailscale).

## Non-goals (v1, YAGNI)

- Building IPAs (Tauri/Xcode/Expo produce the build; airship only ships it).
- A persistent multi-build drop UI / web dashboard.
- The zero-tap `xcrun devicectl` network-install path (Bonjour/RSD discovery
  does not traverse Tailscale reliably off-LAN).
- Public `tailscale funnel` exposure (the iPhone is on the tailnet; Serve is
  private and sufficient).
- App icon extraction (crushed CgBI PNGs / `Assets.car` parsing) — not worth the
  risk surface; the generic install placeholder is fine for v1.

## Preconditions (verified)

- Tailscale running on Mac Studio; iPhone on the same tailnet.
- HTTPS certs enabled on the tailnet. MagicDNS name:
  `example-mac.tail1234.ts.net` (real Let's Encrypt cert, trusted by iOS).
- Builds are signed with a paid Apple Developer account and the iPhone's UDID is
  in the development/ad-hoc provisioning profile, so Apple's OTA install path
  (`itms-services`) is valid for these builds.

## Mechanism

Apple's official over-the-air install: Safari opens an
`itms-services://?action=download-manifest&url=<https manifest url>` link, iOS
fetches a `manifest.plist` describing the app, downloads the `.ipa` over HTTPS,
and installs it. Both the manifest and the `.ipa` must be served over HTTPS with
a cert iOS trusts. Tailscale Serve provides that cert on the `ts.net` hostname.

## Design

A single-file Python CLI run with `uv` (PEP 723 inline deps).

Command: `ship <app.ipa>` (also runnable as `uv run airship.py <app.ipa>`).

### Steps performed

1. **Validate and read metadata.** Confirm the path exists and is a zip
   containing exactly one `Payload/*.app/`. Parse its `Info.plist` (binary plist
   via stdlib `plistlib`) for:
   - `CFBundleIdentifier` (required)
   - `CFBundleVersion` (build number; fallback `CFBundleShortVersionString`) —
     this is the manifest's `bundle-version`
   - `CFBundleDisplayName` (fallback `CFBundleName`, then the `.app` stem) — the
     manifest's `title`

   No icon extraction in v1 (see non-goals). OTA install works fine without
   icons; iOS shows a generic placeholder during install.

2. **Stage artifacts** in a fresh temp dir (`tempfile.mkdtemp`) under **stable,
   URL-safe filenames** (never the original IPA basename, which may contain
   spaces / `#` / `%` / non-ASCII):
   - the `.ipa` staged as `/app.ipa`
   - `manifest.plist` (generated with `plistlib.dump`, `FMT_XML` — never a string
     template) pointing at `/app.ipa` by absolute HTTPS URL
   - `index.html` landing page with the `itms-services` install link, app name,
     version, and bundle id

3. **Serve locally.** A stdlib `http.server.ThreadingHTTPServer` with a handler
   rooted at the temp dir, bound to **`127.0.0.1`**, with explicit content types:
   - `.ipa` → `application/octet-stream`
   - `.plist` → `text/xml`
   - `.html` → `text/html; charset=utf-8`

   Binding `127.0.0.1` (not `0.0.0.0`) is intentional: Tailscale Serve proxies
   from the same machine to localhost, so there is no reason to expose the
   unauthenticated IPA server on the LAN. This is a deliberate, justified
   exception to the global "bind dev servers to 0.0.0.0" rule — that rule exists
   so *other devices* can reach dev servers, but nothing reaches this port
   directly; only local Serve does.

   Port selection: try `4190` (airship's allocated block); if taken, bind to an
   OS-assigned free port (`bind(("127.0.0.1", 0))`) and use whatever port
   results. Tailscale Serve is pointed at the actual bound port, so a shifted
   port is transparent to the phone.

4. **Expose over Tailscale.** First check `tailscale serve status --json`; if `/`
   on port 443 is already mapped to something else, refuse and tell the user
   rather than clobbering it. Otherwise spawn `tailscale serve <port>` as a
   **foreground child process** so `https://example-mac.<tailnet>.ts.net/`
   proxies to `127.0.0.1:<port>`. The base URL is derived from
   `tailscale status --json` `Self.DNSName` with the **trailing dot stripped**
   (`rstrip(".")`), never hardcoded — a trailing-dot host risks a TLS/SNI cert
   mismatch.

5. **Hand off to the phone.** Print:
   - the HTTPS landing-page URL
   - the exact `itms-services://...` URL too (for debugging)
   - a scannable QR code of the landing-page URL rendered in the terminal
     (`segno`)
   - `tailscale ip -4` (per workflow convention)

   On the phone: scan or open the URL in Safari, tap Install. The single Safari
   tap is the only manual step and is inherent to Apple's OTA mechanism.

6. **Run until stopped, then clean up.** The process stays in the foreground
   serving requests. OTA fetches (manifest + IPA) happen *after* the Safari tap,
   so the process must stay alive until install completes. On `SIGINT`/`SIGTERM`
   (Ctrl-C) or normal exit: terminate the `tailscale serve` child (which removes
   **only this port's** Serve mapping — we never run the node-wide
   `tailscale serve reset`), stop the local HTTP server, and remove the temp dir.

### itms-services link construction

The nested manifest URL must be percent-encoded, and `&` must be HTML-escaped in
the `href`:

```
itms-services://?action=download-manifest&url=https%3A%2F%2Fhost.ts.net%2Fmanifest.plist
```

In `index.html` the href is written as
`itms-services://?action=download-manifest&amp;url=<percent-encoded manifest url>`.

### manifest.plist shape

```xml
<plist version="1.0"><dict><key>items</key><array><dict>
  <key>assets</key><array>
    <dict>
      <key>kind</key><string>software-package</string>
      <key>url</key><string>https://<host>/app.ipa</string>
    </dict>
  </array>
  <key>metadata</key><dict>
    <key>bundle-identifier</key><string><CFBundleIdentifier></string>
    <key>bundle-version</key><string><CFBundleVersion></string>
    <key>kind</key><string>software</string>
    <key>title</key><string><display name></string>
  </dict>
</dict></array></dict></plist>
```

## Preflight checks (clear errors, no stack traces)

- IPA path exists and contains a valid `Payload/*.app/Info.plist`; else explain.
- **Signing (advisory — the #1 cause of silent "Unable to Install").** Decode the
  IPA's `embedded.mobileprovision` with `security cms -D -i <profile>` and parse:
  - warn if `ExpirationDate` is in the past;
  - warn if there is no `ProvisionedDevices` array (means an App Store /
    enterprise profile, which cannot install ad-hoc OTA on a registered device);
  - best-effort: fetch the iPhone's UDID (via `iinfo`) and warn if it is not in
    `ProvisionedDevices`.

  These are warnings, not hard failures — proceed but make the likely problem
  visible before the user walks to the phone.
- `tailscale` binary present and node logged in (`tailscale status` succeeds).
- HTTPS certs available for the tailnet; if `tailscale serve` fails because
  HTTPS/MagicDNS is off, surface the one-line admin-console fix rather than the
  raw error.
- After Serve is up, `curl -I` the landing page, manifest, and IPA through the
  `ts.net` hostname to confirm all three are reachable with correct content
  types before handing off. (Final proof is still on-device.)

## Error handling

- Missing/invalid IPA → message naming what was wrong (not found / no Payload /
  no Info.plist / missing CFBundleIdentifier).
- `tailscale serve` nonzero exit → print captured stderr plus the likely cause
  (HTTPS not enabled, not logged in).
- Always run cleanup (terminate the Serve child + temp dir removal — never the
  node-wide `tailscale serve reset`) even on error paths.

## Stack

- Python single-file script, executed via `uv run` with PEP 723 inline metadata.
- Stdlib: `zipfile`, `plistlib`, `http.server`, `tempfile`, `subprocess`,
  `signal`, `socket`, `json`, `urllib.parse`.
- One dependency: `segno` (pure-Python terminal QR).

## Project setup

- Repo: `~/dev/airship`, local git + private `treycausey/airship` remote via `gh`.
- Port block 4190 added to the global dev-server port table.
- `README.md`: prerequisites and the one-command usage, verified by running it
  against a real `.ipa` before commit. Prereqs to call out explicitly:
  - Tailscale running with HTTPS certs enabled; iPhone on the same tailnet.
  - Tailscale ACLs must permit the iPhone (your user) to reach the Mac on 443.
  - The iPhone needs internet during install — iOS contacts Apple to validate the
    signing certificate; tailnet-only reachability is not enough.
  - The `.ipa` must be ad-hoc/development signed with the iPhone's UDID in the
    provisioning profile.

## Testing

- Unit: metadata extraction from a fixture `.ipa` (real build, not synthetic);
  manifest.plist generation matches expected; content-type mapping; free-port
  fallback when 4190 is occupied; base-URL derivation from `tailscale status`
  JSON; cleanup runs on SIGINT.
- Integration / acceptance: run `ship <real.ipa>`, confirm the HTTPS URL serves
  the landing page and manifest with correct content types (curl over the
  tailnet), then install on the iPhone and confirm the app launches. Done is not
  claimed until the app is installed and opens on-device.

## Addendum — 2026-07-01 hardening revision

The following behaviors changed from the design above (the code is the source
of truth; this addendum keeps the doc honest):

- **CLI.** The `.ipa` argument is now optional: with no argument, airship
  serves the newest `.ipa` under the current directory (dot-dirs and
  `node_modules` pruned; refuses to search from `$HOME` or `/`). New `--stay`
  flag keeps the old serve-until-Ctrl-C behavior.
- **Landing page** moved from `/index.html` to `/` (shorter URL, smaller QR).
- **Staging** uses an APFS clone (`cp -c`, falling back to `shutil.copy2`)
  instead of a plain copy: instant like a hardlink, but an immutable snapshot,
  so rebuilding the source `.ipa` mid-serve cannot corrupt an in-flight
  download.
- **Ownership marker.** Every run writes `$TMPDIR/airship-instance.json`
  (airship pid + `tailscale serve` child pid). On startup, a previous airship
  found alive via that file is SIGTERMed and taken over; an orphaned serve
  child from a crashed run is killed (its foreground Serve session dies with
  it). Ownership is proven by the instance file plus a `ps` command-line
  check — never guessed from port probing. Step 4's rule is otherwise
  unchanged: anything else mapping `/` on :443 is refused, never clobbered,
  and mappings on other HTTPS ports are ignored entirely.
- **Startup verification.** `tailscale serve` output is captured; airship
  waits until the mapping actually appears in `serve status --json` (failing
  with the CLI's own output if the child dies), then probes the HTTPS landing
  URL via curl and reports reachability before handoff.
- **Auto-exit.** After the phone — identified by Tailscale Serve's
  `X-Forwarded-For` being another tailnet device (200-response, full body
  streamed; local verification curls and 304s do not count) — finishes
  downloading the IPA, airship exits on its own after 45 idle seconds and
  cleans up. It never exits while a transfer is in flight. `--stay` disables
  this. This kills the "forgotten airship blocks the next run" failure mode at
  the source.
- **Shutdown.** SIGINT is handled via KeyboardInterrupt (no double-cleanup
  signal handlers); a second Ctrl-C during cleanup is ignored so teardown
  always finishes; the instance file is removed on exit.
