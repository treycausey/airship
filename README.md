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

- **Tailscale** running on this Mac and on the iPhone, both on the same
  tailnet, with the `tailscale` CLI reachable on `PATH` (the macOS app ships
  it — enable the CLI in the app's settings if `tailscale` is not found).
- **HTTPS certs enabled** for your tailnet (Tailscale admin console → DNS →
  *Enable HTTPS*). Without this, Tailscale Serve cannot get a cert and install
  will fail.
- **Tailscale ACLs** must allow the iPhone (your user) to reach this Mac on
  the HTTPS port airship uses — 443 by default, or whatever you pass with
  `--https-port`. The default "allow all" policy already does.
- The iPhone needs **internet access during install** — iOS contacts Apple to
  validate the app's signing certificate. Being on the tailnet alone is not
  enough.
- The `.ipa` must be **ad-hoc or development signed** with the iPhone's UDID in
  its provisioning profile — or enterprise in-house signed with a
  `ProvisionsAllDevices` profile, which Apple also supports for OTA install.
  (App Store builds cannot install this way.) Development-signed apps
  additionally need **Developer Mode** enabled on the iPhone.
  airship warns you before you walk to the phone if the embedded
  provisioning profile is expired, has no provisioned devices, or lists none of
  the iPhones/iPads this Mac has paired with. (That last check reads Xcode's
  device registry via `xcrun devicectl` / `xcrun xctrace` when available and is
  skipped otherwise — the phone does not need to be plugged in.)
- [`uv`](https://docs.astral.sh/uv/) installed (runs the single-file script and
  its one dependency, `segno`, with no setup).

## Usage

```sh
./airship.py path/to/YourApp.ipa   # explicit path
./airship.py                       # newest .ipa under the current directory
./airship.py --stay                # keep serving until Ctrl-C (no auto-exit)
./airship.py --https-port 8445     # serve on a different HTTPS port (see below)
```

You'll see something like:

```
  YourApp  com.you.yourapp  v1.4.2

  Open this on your iPhone (Safari), then tap Install:
  https://your-mac.tailXXXXXX.ts.net/

  [ QR code ]

  Tailscale IP: 100.x.y.z  (local server on 127.0.0.1:4190)
  itms link (debug): itms-services://?action=download-manifest&url=https://your-mac.tailXXXXXX.ts.net/manifest.plist

  Reachability: ✓ https://your-mac.tailXXXXXX.ts.net/ answers from this Mac.
  Serving — exits by itself once the phone has downloaded the app and 45s pass (or press Ctrl-C).
```

On the iPhone: scan the QR (or open the URL) in **Safari**, tap **Install**, and
confirm. Once the phone has downloaded the IPA, airship prints a confirmation
and exits on its own after 45 quiet seconds — no need to come back to the
terminal. Ctrl-C works anytime; `--stay` keeps it serving indefinitely.

> Use Safari specifically — `itms-services://` install links do not work in
> Chrome or other iOS browsers.

### If the phone shows a different app at that URL

443 is a **shared origin**. A PWA served from your Mac's bare `ts.net` address
even once registers a service worker there, and that worker then controls the
whole origin — it will serve its own cached shell in place of airship's install
page, on a device where that app is not running and its dev server is not even
up. curl the URL from the Mac to tell the two apart: if the Mac gets the install
page and the phone does not, it is the phone's service worker, not airship.

Fixes, cheapest first:

- Open the URL in a **Private tab** — iOS Safari does not run service workers there.
- `./airship.py --https-port 8445 …` — a different port is a different origin, so
  a worker registered on 443 cannot intercept it. This is also the answer when
  something legitimately owns `/` on 443 and you would rather not disturb it.
- Fix the offending app to disown origins it does not serve.

airship never overwrites another service's Serve mapping: it refuses with
instructions instead. A mapping on any port *other* than the one it is using is
not a conflict and is left alone.

## How it works

1. Reads `CFBundleIdentifier` / `CFBundleVersion` / display name from the IPA's
   `Info.plist`.
2. Stages the IPA (APFS-cloned — instant, immutable snapshot; plain-copied on
   non-APFS filesystems), a `manifest.plist`, and a one-button `index.html`
   in a temp dir under stable, URL-safe names.
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

## Security model

The install page and the IPA are served without authentication to your
tailnet: while airship is running, any device (and any user) on the tailnet
can download the app. That is the intended trust model for a personal
tailnet; if yours is shared, scope access with Tailscale ACLs. Nothing is
ever exposed to the public internet (airship uses Tailscale Serve, never
Funnel), and the local HTTP server binds `127.0.0.1` only.

## Troubleshooting

- **"Unable to Install" on the phone** — almost always signing. The IPA must be
  ad-hoc/development signed with this iPhone's UDID (or enterprise in-house
  signed). For development-signed apps, check Developer Mode is on (Settings →
  Privacy & Security → Developer Mode). Heed airship's signing warnings.
- **Connection refused / cert errors** — HTTPS certs aren't enabled for your
  tailnet, or the iPhone isn't on the tailnet. Check the Tailscale app on the
  phone.
- **Install link does nothing** — you opened it in a browser other than Safari.

## Development

```sh
uv run --with pytest --with segno pytest tests/
```

Tests use `tests/fixtures/Gambatte-fixture.ipa`, built from a real iOS build's
binary `Info.plist` plus a synthetic, unsigned provisioning profile (a real
profile embeds the developer's identity and device UDIDs).

## License

MIT — see [LICENSE](LICENSE).
