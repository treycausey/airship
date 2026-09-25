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
./airship.py --https-port 8445     # serve on exactly this HTTPS port (see below)
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

To stop a **backgrounded** airship (no TTY), use `pkill -INT -f airship.py`.
Not `pkill -TERM`: that also signals uv's wrapper process, and uv kills
airship before its cleanup finishes, leaking the staging dir under `$TMPDIR`.

> Use Safari specifically — `itms-services://` install links do not work in
> Chrome or other iOS browsers.

### Push notification (optional)

When the install page is live, airship can send a [Pushover](https://pushover.net)
notification whose link opens that page, so you do not need the terminal or
the QR code. Set it up once:

1. On pushover.net, create an application for airship and copy its API token.
   Copy your user key from the dashboard too.
2. Put both values in `.env.local` next to `airship.py` (git-ignored,
   declared in `.env.schema`):

   ```sh
   cd ~/dev/airship && umask 077 \
     && read -rs "t?Pushover app token: " && echo \
     && read -rs "u?Pushover user key: " && echo \
     && printf 'PUSHOVER_TOKEN=%s\nPUSHOVER_USER=%s\n' "$t" "$u" >| .env.local \
     && unset t u && varlock load
   ```

airship reads these values with `varlock run --path <airship dir>`, so the
notification works whichever directory you call airship from. With no
`.env.local`, airship prints `Push notification: off` and continues. If a
send fails, airship prints a warning and keeps serving. Install links work
only in Safari: if the link opens in another browser view and **Install**
does nothing, open the same page in Safari.

### If the phone shows a different app at that URL

443 is a **shared origin**. A PWA served from your Mac's bare `ts.net` address
even once registers a service worker there, and that worker then controls the
whole origin — it will serve its own cached shell in place of airship's install
page, on a device where that app is not running and its dev server is not even
up. curl the URL from the Mac to tell the two apart: if the Mac gets the install
page and the phone does not, it is the phone's service worker, not airship.

airship heals this on its own: it answers every `*.js` request (the install
page has none of its own) with a tiny service worker that clears the stray
worker's caches, unregisters it, and reloads the page. A worker checks for
updates on each navigation by fetching its own script URL, so the first visit
to the install page swaps the stray worker for the kill switch and the second
load is the real page — usually within the same visit, since the kill switch
reloads for you. This works for any PWA toolchain and for any browser's own
data store (Chrome on iOS keeps one separate from Safari's, with no per-site
deletion, which is why clearing site data by hand was never a good answer).

If it still shows the wrong app, cheapest first:

- Open the URL in a **Private / Incognito tab** — no service workers run there.
- `./airship.py --https-port 8445 …` — a different port is a different origin, so
  a worker registered on 443 cannot intercept it. This is also the answer when
  something legitimately owns `/` on 443 and you would rather not disturb it.
- Fix the offending app to only register its worker on an origin it owns
  (flusso does this: `src/lib/sw-origin-guard.ts`).

airship never overwrites another service's Serve mapping: it refuses with
instructions instead. A mapping on any port *other* than the one it is using is
not a conflict and is left alone.

## How it works

1. Reads `CFBundleIdentifier` / `CFBundleVersion` / display name from the IPA's
   `Info.plist`.
2. Stages the IPA (APFS-cloned — instant, immutable snapshot; plain-copied on
   non-APFS filesystems), a `manifest.plist`, a one-button `index.html`, and
   the page's `icon.svg` favicon in a temp dir under stable, URL-safe names.
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
can prove ownership: every run writes an instance file for its HTTPS port (its
pid and its `tailscale serve` child's pid), and an orphaned serve child from a
crashed run is cleaned up.

Several sessions can ship at once. With no `--https-port`, airship tries 443,
then 4443 through 4449, and takes the first port whose `/` is free. It skips a
port where another airship is still alive and does not stop that run. It prints
which ports it skipped and why, and the URL it prints carries the chosen port.
An explicit `--https-port N` is strict: a previous airship on that port is
killed and taken over, and anything else on it makes airship refuse.
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

### End-to-end suite

`tests/e2e/` spawns a real `airship.py` subprocess and drives it over real
HTTPS with `httpx` — no browser, no real Tailscale, no real Pushover network
access. Fakes for `tailscale`, `varlock`, `curl`, `xcrun`, and `security`
(`tests/e2e/fake_*.py`) stand in for the real ones: `tailscale` terminates
TLS with a throwaway self-signed cert and proxies to airship's local HTTP
server exactly like `tailscale serve` does; `varlock` and `curl` block
anything that isn't a loopback request (so a real `.env.local` sitting next
to the script under test can never result in a real Pushover push); `xcrun`
and `security` are stubbed as belt-and-suspenders. The child process also
gets a dead proxy (`HTTP(S)_PROXY=http://127.0.0.1:9`) and `UV_OFFLINE=1`, so
nothing it does can reach the real network even if a fake were bypassed.
These tests are slower (they start a real process and do a real TLS
handshake) and excluded from the default run (`-m "not e2e"` in
`pytest.ini`). Run them explicitly:

```sh
uv run --with pytest --with segno --with httpx pytest -m e2e tests/
```

Two env vars exist solely so this suite can isolate a real airship.py run
from the machine it's running on; their defaults reproduce today's hardcoded
behavior exactly, an empty value is treated as unset (falls back to the
default, never becomes the current directory or an unhandled crash), and
`AIRSHIP_PREFERRED_PORT` is range-checked (0–65535) with a readable error
naming the variable on bad input:

- `AIRSHIP_INSTANCE_DIR` — where `airship-instance-<port>.json` lives.
  Default: the system temp dir (unchanged). The suite points this at a
  throwaway directory so it never reads or writes a real instance record.
- `AIRSHIP_PREFERRED_PORT` — the local HTTP server's preferred port before
  falling back to an OS-assigned one. Default: `4190` (unchanged). The suite
  always sets this to a port it chose itself (never touching the real,
  currently-live ships on 4190/4443/4444), and uses it to force the fallback
  path deterministically in the free-port test.

`.githooks/pre-commit` runs this suite automatically for a commit that
touches `airship.py`, `tests/`, or a dependency file. Enable it with
`git config core.hooksPath .githooks`.

## License

MIT — see [LICENSE](LICENSE).
