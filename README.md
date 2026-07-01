# airship

Install a finished iOS `.ipa` onto your iPhone **over the air** — no cable, no
shared WiFi. Both devices just need to be on the same Tailscale tailnet.

airship reads the app's metadata out of the `.ipa`, generates Apple's
over-the-air install manifest, serves it over Tailscale (which provides a
real HTTPS cert that iOS trusts), and prints a URL + QR code. You open the link
in Safari on the phone and tap **Install**. That single tap is the only manual
step — it is inherent to Apple's OTA mechanism.

airship is build-agnostic: point it at any `.ipa` (Tauri, Xcode, Expo, …). It
does not build the app.

## Prerequisites

- **Tailscale** running on this Mac and on the iPhone, both on the same tailnet.
- **HTTPS certs enabled** for your tailnet (Tailscale admin console → DNS →
  *Enable HTTPS*). Without this, Tailscale Serve cannot get a cert and install
  will fail.
- **Tailscale ACLs** must allow the iPhone (your user) to reach this Mac on
  port 443. The default "allow all" policy already does.
- The iPhone needs **internet access during install** — iOS contacts Apple to
  validate the app's signing certificate. Being on the tailnet alone is not
  enough.
- The `.ipa` must be **ad-hoc or development signed** with the iPhone's UDID in
  its provisioning profile. (App Store / enterprise builds cannot install this
  way.) airship warns you before you walk to the phone if the embedded
  provisioning profile is expired, has no provisioned devices, or does not list
  this iPhone.
- [`uv`](https://docs.astral.sh/uv/) installed (runs the single-file script and
  its one dependency, `segno`, with no setup).

## Usage

```sh
./airship.py path/to/YourApp.ipa   # explicit path
./airship.py                       # newest .ipa under the current directory
./airship.py --stay                # keep serving until Ctrl-C (no auto-exit)
```

You'll see something like:

```
  YourApp  com.you.yourapp  v1.4.2

  Open this on your iPhone (Safari), then tap Install:
  https://your-mac.tailXXXXXX.ts.net/

  [ QR code ]

  Tailscale IP: 100.x.y.z  (local server on 127.0.0.1:4190)
  Serving — exits by itself once the phone has downloaded the app and 45s pass (or press Ctrl-C).
```

On the iPhone: scan the QR (or open the URL) in **Safari**, tap **Install**, and
confirm. Once the phone has downloaded the IPA, airship prints a confirmation
and exits on its own after 45 quiet seconds — no need to come back to the
terminal. Ctrl-C works anytime; `--stay` keeps it serving indefinitely.

> Use Safari specifically — `itms-services://` install links do not work in
> Chrome or other iOS browsers.

## How it works

1. Reads `CFBundleIdentifier` / `CFBundleVersion` / display name from the IPA's
   `Info.plist`.
2. Stages the IPA (hardlinked, not copied), a `manifest.plist`, and a
   one-button `index.html` in a temp dir under stable, URL-safe names.
3. Serves them from `127.0.0.1` (an arbitrary free port, preferring 4190).
4. Runs `tailscale serve <port>` so `https://<your-node>.ts.net/` proxies to
   it, verifies the mapping actually appears in `tailscale serve status`, and
   probes the HTTPS URL before telling you to pick up the phone.
5. Prints the install URL and a QR code.
6. Watches the request log; once the phone has fetched the whole IPA and 45
   quiet seconds pass, it exits and cleans up by itself (Ctrl-C also works).
   It tears down only its own Serve mapping and temp dir — it never runs the
   node-wide `tailscale serve reset`.

If `/` on your node is already claimed, airship recovers on its own where it
can prove ownership: every run writes an instance file (its pid and its
`tailscale serve` child's pid), so a previous airship left running is killed
and taken over, and an orphaned serve child from a crashed run is cleaned up.
Anything airship cannot prove is its own — including stale-looking mappings —
is never touched; it refuses and tells you the exact command to clear it.

## Troubleshooting

- **"Unable to Install" on the phone** — almost always signing. The IPA must be
  ad-hoc/development signed with this iPhone's UDID. Heed airship's signing
  warnings.
- **Connection refused / cert errors** — HTTPS certs aren't enabled for your
  tailnet, or the iPhone isn't on the tailnet. Check the Tailscale app on the
  phone.
- **Install link does nothing** — you opened it in a browser other than Safari.

## Development

```sh
uv run --with pytest --with segno pytest tests/
```

Tests use `tests/fixtures/Gambatte-fixture.ipa`, built from a real iOS build's
binary `Info.plist` and real `embedded.mobileprovision`.
