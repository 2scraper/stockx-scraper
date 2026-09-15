# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [1.0.0] — 2026-09-15

Initial release.

### Added
- **Three engines, one contract.** `playwright_scraper.py` (primary),
  `selenium_scraper.py`, and `puppeteer_scraper.py` (via pyppeteer) share
  the same CLI flags, the same `Product` output schema, and the same exit
  codes (`0` complete · `1` crash · `2` bad usage · `3` blocked · `4` zero
  products · `5` remote API error · `6` partial), via a shared
  `output_writer.finish_run()`.
- **Search, category/browse listings, and single product pages.** Reads
  stockx.com's own embedded data payload first — the `__NEXT_DATA__`
  react-query cache on listing pages, JSON-LD on product pages — with DOM
  parsing as a fallback, not the primary path.
- **JSON and CSV export**, a documented `Product` schema, and a
  `.meta.json` run sidecar (status, pages completed, failed pages,
  price-confirmation rate) written for every completed or partial run —
  and deliberately never written for a blocked/empty/remote-API-error run,
  so a sidecar can never sit next to a good previous output with a
  misleading status.
- **Optional 2Captcha integration**: captcha solving (detects a real
  challenge, not just a widget's presence, before spending a solve),
  the Scraping Browser API over `--cdp-endpoint` (a remote browser session
  with its own proxy, fingerprint, and captcha auto-solve bundled), the
  Fingerprint API, and residential proxies (`--proxy` / `STOCKX_PROXY`,
  rotated with per-exit failure tracking) — all wired in, none required to
  get started.
- **Concurrent listing-page fetches** (`--concurrency`), with per-page
  failures (network or parsing) isolated to that page rather than failing
  the whole run.
- Credential handling designed to never reach a log or an exception
  message in full — proxy and CDP endpoint credentials are masked/redacted
  everywhere they could otherwise leak.
- `smoke_test.py`: an offline suite asserting real, pinned values against
  real captured fixtures — not just "some rows populated" — runnable with
  zero engines installed, one engine installed, or all three.
- CI: an offline test matrix across two Python versions, one
  `engine-smoke` job per engine in its own virtualenv, a Docker build
  check, and a `canary` workflow for a real periodic live run (skips
  cleanly, not red, when its secret isn't configured).
- `landing.md` / `landing.html` and a full `README.md` documenting the
  CLI, the `Product` schema, the exit-code contract, and what was actually
  measured against the live site.
