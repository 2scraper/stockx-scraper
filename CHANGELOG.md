# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [1.0.1] — 2026-09-16

### Added

- **`.github/ci_checks.py`** — one implementation of the credential, `--help`
  and sample-schema checks, invoked from **both** CI and the offline suite,
  plus a suite check asserting that the workflow calls it rather than
  reimplementing it. CLAUDE.md §17 records what two implementations cost: in
  three sibling repos the shipped script was invoked by nothing while
  `tests.yml` carried a narrower grep, and they disagreed in the direction
  that matters. Verified in both directions on 2026-09-17 — it passes on this
  repo (31 files scanned) and it flags a planted CDP endpoint, a planted
  32-hex key and a planted `http://user:pass@` proxy URL.
- **A pre-publication history scan** (`--history-check`), run here for the
  first time: **35 blobs across 58 objects, nothing credential-shaped**. The
  two findings it did report are the masking fixtures as they were written up
  to v1.0.0; both are recorded in `HISTORY_DECIDED` with the reason and the
  date, because a commit on top cannot remove what history holds. That list
  is consulted by `--history-check` only, so a value reappearing in the
  working tree still fails.
- **A check that no module holds a statement the control flow can never
  reach** — anything after a `return`/`raise`/`break`/`continue` in the same
  block. Byte-compiling cannot see it, since unreachable code is still valid
  code. Six repos in this family carried the same fifteen unreachable lines
  from their first commit; this tree is clean, and the check keeps it clean.
- The workflow-wiring check tolerates being run **inside the Docker image**,
  where `RUN python3 smoke_test.py` executes this suite at build time and
  `.github/` is deliberately not COPYed — the image carries no CI material,
  which is itself something CI asserts about it. The escape is narrow on
  purpose: it triggers only when the whole `.github` directory is absent,
  never when a file inside it is missing, because a check that quietly
  passes once its input disappears is worse than no check. Verified in all
  three states — wired, unwired, and no `.github` at all.
- **A check that the three engines expose exactly the same CLI flags**, in
  both directions. Measured 2026-09-17: **23 flags, identical across all
  three, no exceptions** — a stronger position than the rest of this family,
  where per-engine differences are documented and carried, so it is worth
  pinning rather than leaving to drift. §20 records why this is written
  before it is needed: a sibling's README promised "same CLI" while its
  primary engine had twelve flags its twins did not, nine of them predating
  the claim. The extractor counts only the argparse parser's own
  `add_argument` calls — `options.add_argument("--no-sandbox")` is a Chrome
  switch handed to chromedriver, not a flag of this tool, and counting those
  made Selenium look like it had three extra flags.
- **A check that binds every call into a shared module against the callee's
  real signature** (§17's check #1). It catches a call whose arguments do not
  fit and a call to a name the shared module does not define at all — both of
  which reach a live run as a crash on the first fetch while everything
  offline stays green. All three new checks are verified by control.

### Changed

- **`tests.yml` no longer carries its own copies of three checks.** The
  inline credential grep excluded `smoke_test.py` **wholesale**, so the file
  most likely to acquire a real credential by paste while debugging was the
  one file nobody scanned. The masking fixtures that made that exclusion look
  necessary are now built by concatenation, so no line holds a complete
  `scheme://user:pass@host` literal and the scan is live on that file too.
  The inline sample check also compared column **sets**, which passes when
  two columns are swapped; the script compares the ordered list, and column
  order is part of this family's output contract.

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
