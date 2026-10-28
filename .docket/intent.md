# airship

airship is a single-file Python CLI (`airship.py`, run with uv; its one dependency is `segno`) that installs a finished iOS `.ipa` onto an iPhone over the air through Tailscale. No cable or shared WiFi is needed. It reads the app's metadata from the IPA's Info.plist and generates Apple's OTA `manifest.plist` and a one-button install page. It serves them from 127.0.0.1 behind `tailscale serve` (HTTPS, never Funnel), then prints a URL and QR code. It can also send a Pushover notification, with credentials resolved through Varlock. It checks signing and provisioning profiles, manages Serve mappings and instance files per HTTPS port so several sessions can run at once without touching mappings it does not own, and exits by itself once the phone has downloaded the IPA. It does not build apps. Tests are pytest unit tests plus an end-to-end suite under `tests/e2e/` that uses fake `tailscale`/`varlock`/`curl` binaries. A design doc is in `docs/`.

## Paths agents must not touch

- `.env`: Local secrets file (gitignored); may hold real credentials
- `.env.local`: Holds real Pushover token/user key that override .env.schema
- `.env.*.local`: Local secret override files (gitignored)

nogo: .env
nogo: .env.local
nogo: .env.*.local
