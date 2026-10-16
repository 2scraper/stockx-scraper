# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) as closely as a CLI toolkit can manage. A patch
release means "fixes", not that every flag and default is frozen — a
behaviour-changing default gets called out explicitly in its entry below
rather than being a silent violation of that.

## [Unreleased]

> **Behaviour change.** `--captcha-api`, `--min-score` and
> `--solve-captcha always` are refused with exit 2 in every engine
> (Playwright, Selenium, pyppeteer). No engine injects a solved captcha token
> into the page, so these flags changed nothing while appearing to configure
> a solve. The error names the flag and says that this repository does not
> implement captcha-token injection. `--solve-captcha` now accepts
> `when-blocked` (the default) and `off`; their behaviour is unchanged.

### Changed
- The captcha flags are defined once, in `captcha_solver.add_captcha_arguments`,
  so the three engines cannot drift apart; `--help` marks the refused flags.
- An offline check runs each engine's parser against each refused flag and
  confirms that `off` and `when-blocked` still parse and run.
- CI only: the canary bills a Scraping Browser session (`STOCKX_CDP_ENDPOINT`),
  so it now runs weekly (Mondays 06:17 UTC) instead of daily; a manual dispatch
  still runs it.

## [1.1.0] — 2026-10-15

> **Behaviour changes.**
> - **Exit codes.** A served page whose rows cannot be read (the data block
>   is missing, or the page reports results and none were parsed) now exits
>   **5** with `status: parser_found_nothing`; it used to exit 4, "empty
>   catalogue". A 404/410 for the requested address exits **5** with
>   `status: not_found`; it used to exit 3, "blocked". Since #3, a run in
>   which every page failed to load exits **5** (`fetch_failed`) instead of
>   4; that change was not recorded here before.
> - **A page blocked mid-run is a failed page.** It goes into
>   `failed_pages` and is no longer counted in `pages_completed`.
>   `stop_reason` now says why a complete run stopped (`site_last_page`,
>   `no_new_rows`, `requested_pages_done`, `single_product`) or what went
>   wrong, instead of repeating `status`.
> - **No paid captcha solves are bought.** None of the engines injects a
>   solver token into a locally launched browser, so the solve they used to
>   buy could not change the outcome. A captcha page that gates the content
>   now reads as blocked (exit 3) whether or not a key is set. Over
>   `--cdp-endpoint` the Scraping Browser API still solves in-session.
> - **Selenium and pyppeteer reject the values Playwright rejects**:
>   `--retries -1` (it ran no navigation at all), `--pages 0`,
>   `--concurrency 0` or negative, a negative `--delay`/`--retry-delay`, and
>   (in all three engines) a negative `--proxy-block-retries` are now exit 2.
> - **`--url` with `--category` is exit 2**, and a typed `--category` now
>   wins over `STOCKX_URL` from the environment or `.env` (the environment
>   URL used to replace it silently).
> - **The canary fails without `STOCKX_CDP_ENDPOINT`** instead of skipping
>   green, and fails on any exit other than 0.

### Fixed

- Output files and the sidecar are written atomically: a temporary file in
  the same directory, fsynced and renamed into place. An interrupted write
  used to truncate the previous good file. A new file gets
  `0o666 & ~umask`; an existing file keeps its mode.
- CSV cells starting with `=`, `+`, `-`, `@`, a tab or a line break get a
  leading `'`, so a spreadsheet does not run them as formulas. Numbers and
  the JSON output are unchanged; the sidecar records `csv_cells_escaped`.
- A network error talking to the captcha solver is a warning, not a crash
  (it used to end the run with exit 1).
- The Selenium engine now honours `--proxy-rotate` (a fresh browser on the
  next exit for each page); it was accepted and ignored.
- Selenium and pyppeteer now recognise StockX's bot-guard page as blocked,
  as Playwright already did.
- The credential scan matches its skipped directories against the path
  inside the repository, so a clone under a directory named `venv` is
  scanned instead of reporting "ok, 0 files"; a scan of 0 files now fails.
- `python3 env_config.py` no longer prints the first and last characters
  of a secret, only its length.

### Removed

- `TwoCaptchaClient.scraping_browser_connection_url()`: nothing called it,
  and the endpoint shape it built was never verified.

### Added

- **`.github/ci_checks.py`** — one implementation of the credential, `--help`
  and sample-schema checks, invoked from **both** CI and the offline suite,
  plus a suite check asserting that the workflow calls it rather than
  reimplementing it. Two implementations of one check cost something: in
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
  pinning rather than leaving to drift. It is written before it is needed
  because a sibling's README promised "same CLI" while its
  primary engine had twelve flags its twins did not, nine of them predating
  the claim. The extractor counts only the argparse parser's own
  `add_argument` calls — `options.add_argument("--no-sandbox")` is a Chrome
  switch handed to chromedriver, not a flag of this tool, and counting those
  made Selenium look like it had three extra flags.
- **A check that binds every call into a shared module against the callee's
  real signature**. It catches a call whose arguments do not
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
