# stockx-scraper

![release](https://img.shields.io/github/v/release/2scraper/stockx-scraper?sort=semver)
![tests](https://github.com/2scraper/stockx-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/stockx-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![runs without an account](https://img.shields.io/badge/runs%20without%20an%20account-yes-success)

StockX listing-page scraper: search results, category/browse listings, and
single product pages. Three engines (Playwright primary, Selenium and
Puppeteer/pyppeteer for parity), JSON or CSV output, an open, documented
`Product` schema. Part of the [2scraper](https://github.com/2scraper) family
— see `CLAUDE.md` if this repo ships one, for the shared architecture this
follows.

## What was actually measured

Live captures against stockx.com, 2026-09-10:

- **An ordinary headless Chromium, no proxy, no 2Captcha key, no account,
  cleared stockx.com's Cloudflare bot-management scripts** and returned
  full search/category/product data on every page requested. If your
  traffic looks like this too, you may not need the paid products at all —
  they buy you volume from many addresses, a specific exit country, or a
  consistent device identity, not passage through a wall that wasn't there.
- **Search and category listing pages carry exactly one JSON-LD block**
  (`BreadcrumbList` — zero products in it). The real data source is
  stockx.com's own embedded Next.js payload: a `__NEXT_DATA__` script tag
  holds a dehydrated react-query cache, and the `getDiscoveryData` entry in
  it carries the full listing — title, brand, category, and live market
  data (lowest ask, highest bid, last sale, 90-day/annual stats) — richer
  than JSON-LD would have been, and requiring no DOM read at all.
- **Product-detail pages DO carry real JSON-LD** (`@type: "Product"`, an
  `AggregateOffer` with one `Offer` per shoe size). Style ID, colorway and
  retail price come from the same page's `GetProduct` query `traits` array
  instead — JSON-LD doesn't carry them here.
- **Style ID is not present on listing rows at all** — only on product-detail
  pages. `style_id` is `None` on every row a `--category`/`--url <listing>`
  run produces; that's the site's own data shape, not a parsing gap. Scrape
  a product's own URL directly if you need it.
- **Pagination is a real `?page=N` query param**, with actual `<a
  href="/search?...&page=2">` links in the DOM — and, verified live,
  requesting `?page=2` directly (without following page 1's own link)
  server-renders page 2's own data correctly. No cursor, no token.
- **Prices are geo/currency-localized** (observed: a Netherlands exit
  returned EUR throughout — search results, product offers, and the
  page's own displayed prices all agreed). The one exception: the
  **Retail Price trait is always USD-denominated**, even on an
  EUR-currency page — a real site quirk, not a scraper bug.
- **The bot-management vendor is Cloudflare**, but its `cdn-cgi/challenge-
  platform` "precursor" script tag is present on ordinary, fully-successful
  page loads too — it is NOT, by itself, evidence of being blocked. See
  `product_parser.BOT_CHALLENGE_MARKERS` and the regression test in
  `smoke_test.py` for why that string is deliberately excluded from the
  block-detection markers.
- No actual CAPTCHA challenge page was encountered during this testing —
  `captcha_solver.py`'s widget-identification logic is therefore verified
  only against synthetic markup (see its tests), not a real StockX
  challenge. Detection stays broad and safe (never solves when products
  are already rendered); token *injection* on a locally-launched browser is
  intentionally left unimplemented rather than guessed — see "Known
  limitations" below.
- **Network reachability is per-host, not all-or-nothing**, at least from
  this project's own dev environment: `stockx.com` and `2captcha.com`
  itself are refused outright, but `api.2captcha.com` (classic solving,
  Fingerprint API) answers normally, and `cb.2captcha.com:9222` (the
  Scraping Browser API's CDP endpoint) accepts a connection at the
  TCP/CONNECT level too — only the WebSocket auth, which needs a real
  key, was left unverified. Don't assume "can't reach the target site"
  means "can't reach any of 2Captcha's infrastructure either" — check
  each host.

## Install

Pick one engine (installing more than one into the same environment is not
supported — see "Engines" below):

```bash
pip install -r requirements-playwright.txt && playwright install chromium   # primary
pip install -r requirements-selenium.txt                                    # needs a matching chromedriver
pip install -r requirements-puppeteer.txt                                   # pyppeteer — see its own warning below
```

Copy `.env.example` to `.env` and fill in what you use — see `env_config.py`
for the precedence rules (CLI flag > exported env var > `.env` > default).
`python3 env_config.py` shows what was picked up without ever printing a
secret.

## Usage

```bash
# a category (see product_parser.CATEGORY_SLUGS / DEMOGRAPHIC_SLUGS for the real list)
python3 playwright_scraper.py --category sneakers --pages 3 --format json --out stockx_sneakers.json

# a search
python3 playwright_scraper.py --url "https://stockx.com/search?s=jordan+4" --pages 2

# a single product page (style ID, colorway, per-size JSON-LD offers)
python3 playwright_scraper.py --url "https://stockx.com/air-jordan-4-retro-og-flight-club"

# with 2Captcha's Scraping Browser API (proxies + fingerprint + captcha solving bundled server-side)
python3 playwright_scraper.py --category sneakers --cdp-endpoint "$STOCKX_CDP_ENDPOINT"
```

`selenium_scraper.py` and `puppeteer_scraper.py` accept the identical flag
set and produce the identical output contract — see "Engines" for the two
places they genuinely can't behave the same as Playwright.

### Flags

`--url --category --pages --format --out --delay --retries --retry-delay
--concurrency --proxy --proxy-file --proxy-rotate --proxy-shuffle
--proxy-block-retries --twocaptcha-key --captcha-api --solve-captcha
--min-score --cdp-endpoint --allow-empty --dump-html --headless/--headful`

Credentials belong in `.env` / `STOCKX_PROXY` / `TWOCAPTCHA_KEY` — never as
literal `--proxy`/`--twocaptcha-key` text on a shared or logged command
line if you can avoid it.

## Output contract

`Product` (`output_writer.py`) — family-common columns first, stockx.com-
specific columns after:

```
sku, source, category, title, brand, price, currency, price_source, product_url,
image_url, scraped_at,
style_id, colorway, lowest_ask, highest_bid, last_sale, retail_price_usd,
release_date, annual_avg_price, annual_sales_count, gender
```

`price` is StockX's **lowest ask** — the number a buyer could act on right
now. `price_source` is `embedded_json` (listing pages), `jsonld`
(product-detail pages), or `null` if genuinely unavailable — never a
defaulted guess. See `sample_output.json` / `sample_output.csv` for real
rows (cut from an actual capture, not fabricated — `smoke_test.py` checks
for fabrication markers).

**Exit codes**: `0` complete · `1` crash · `2` bad usage · `3` blocked ·
`4` zero products (and nothing was written) · `5` remote API error · `6`
partial (some pages failed, some data was still written). Every run writes
a `<out>.meta.json` sidecar with `status`, `pages_completed`,
`failed_pages` and `price_confirmed_pct` — **except** a failed/empty run,
which writes no sidecar and no output at all, so it can never overwrite a
previous good run (`--allow-empty` opts out of that guard).

## Engines

Playwright is primary; Selenium and pyppeteer are parity copies — all
three agree on exit codes and the `Product` schema via the shared
`output_writer.finish_run()`. Real, stated limits and quirks:

- **Selenium cannot use an authenticated remote CDP endpoint.**
  chromedriver's `debuggerAddress` takes a bare `host:port`; the Scraping
  Browser API's `ws://login:pass@host:port` shape needs an authenticated
  WebSocket upgrade, which only Playwright's `connect_over_cdp` and
  pyppeteer's `connect` support. `selenium_scraper.py` refuses a
  credentialed `--cdp-endpoint` outright (exit 2) rather than attempting a
  connection that would just fail on auth.
- **Selenium's `--proxy-server` cannot authenticate at all.** A `--proxy`
  with credentials has them stripped before reaching Chrome, with a loud
  warning — never a silent no-op.
- **pyppeteer is effectively unmaintained** (its own README points at
  Playwright) — shipped for parity, not as a recommendation.
- **If Selenium Manager can't reach `googlechromelabs.github.io` /
  `storage.googleapis.com`** (a restrictive corporate proxy, same failure
  mode this repo hit while being built — see CHANGELOG), get a matching
  `chromedriver` another way rather than giving up on the engine: the
  `chromedriver-py` PyPI package ships prebuilt binaries as regular wheels
  (`pip install chromedriver-py==<chrome-major>.<...>`, then point at
  `chromedriver_py.binary_path` or put it on `PATH`) — it comes down over
  the same `pypi.org`/`files.pythonhosted.org` hosts everything else in
  `requirements*.txt` already needs, so it gets through where the Google
  download hosts don't. `SELENIUM_CHROME_BIN` / `CHROME_BIN` point the
  engine at a specific Chrome/Chromium binary (e.g. reusing Playwright's
  own downloaded one) the same way `PYPPETEER_EXECUTABLE_PATH` does for
  the Puppeteer engine — useful for the same reason.
- Install **exactly one** engine per environment. Playwright and pyppeteer
  declare mutually unsatisfiable `pyee` pins, and pyppeteer collides with
  Selenium's `urllib3` pin. All three can end up installed side by side
  without erroring, but `pip check` will report the conflict and pip may
  silently resolve it by downgrading something you needed — this was
  reproduced live while building this repo (`pip install
  pyppeteer` after playwright pulled `pyee` down to 11.1.1 against
  playwright's own `<14,>=13` requirement). Use a venv per engine — see
  `.github/workflows/tests.yml`'s `engine-smoke` job for exactly that
  pattern.

## Known limitations

- **Captcha token injection on a locally-launched browser is not
  implemented.** `captcha_solver.solve_when_blocked()` detects a widget,
  decides whether solving is actually warranted (never when products are
  already rendered), and returns a solved token from 2Captcha — but
  injecting that token into the page is widget/site-specific, and no real
  StockX challenge was ever observed to verify an injector against. Over
  `--cdp-endpoint` (the Scraping Browser API), this doesn't matter:
  2Captcha's own `Captcha.setAutoSolve` CDP domain detects, solves and
  injects entirely inside their infrastructure (wired up in
  `playwright_scraper.py` / `puppeteer_scraper.py` — Selenium can't reach
  it either, for the CDP-auth reason above).
- **`highest_bid` / `last_sale` are only populated on listing rows**, not
  on rows from scraping a product page directly — StockX's product-page
  SSR payload doesn't carry them (they render client-side after mount);
  scrape the product's category/search listing instead if you need them
  alongside a specific product.
- **A live run against stockx.com from this project's own dev sandbox is
  still not possible** (outbound access to `stockx.com` is refused there),
  but a user's own machine has run the full sequence for real. Results
  vary by path and exit, so read all of the following as "this is what a
  clean run can look like" and "this is a real failure mode to expect",
  not "this always works":
  - A category listing via the Scraping Browser API (`--cdp-endpoint`, EU
    exit) came back `status: complete`, 41 products, 100%
    `price_confirmed_pct` — the log even shows the "detected != blocking"
    policy working correctly on a real page ("Captcha widget present but
    products already rendered — not solving").
  - Some listing rows come from a `Variant`-typed `getDiscoveryData` edge
    (e.g. a specific size/colorway callout) rather than a `Product`-typed
    one — StockX nests the catalog fields (`title`/`brand`/`sku`) one
    level down on those, instead of carrying them flat. `parse_listing_node()`
    handles both shapes; see the regression test in `smoke_test.py` for
    the real capture this was verified against.
  - Playwright, direct (residential IP, no proxy): fast HTTP 403, StockX's
    own Next.js error boundary instead of listing data, has been observed.
  - Playwright, through a 2Captcha residential proxy with a EU exit:
    **the same fast 403** has also been observed — a different exit IP
    and country did not help in that instance.
  - Playwright, through the Scraping Browser API (`--cdp-endpoint`): a
    consistent 30s navigation timeout (no response at all, not even a
    403) has also been observed — a third, distinct failure mode.
  - **The same machine's ordinary, non-automated Chrome loaded the exact
    same URL instantly, fully rendered, real data, no challenge**, at the
    same time as the 403s above. That rules out a network/ISP/country-level
    block (the human browser reached the site fine, from the same network,
    at the same time) and points at **Cloudflare/StockX distinguishing an
    automated Chromium session from an ordinary one** — a
    fingerprint/behaviour signal, not an IP-reputation one, since switching
    IP (the proxy test) made no difference.
  - A stable connection through the Scraping Browser API has also
    captured what StockX sends when it does block: a plain, static
    Cloudflare WAF block page — "Sorry, you have been blocked... This
    website is using a security service to protect itself from online
    attacks" — with **no captcha widget anywhere** (`data-sitekey` appears
    nowhere in the page). Cloudflare can make a hard block decision before
    any solvable challenge would be offered, over `--cdp-endpoint` exactly
    as over a direct connection or a residential proxy — there is no
    captcha here for 2Captcha's own solving to help with, on a request
    blocked this way. (The Scraping Browser API's managed browser also
    ships its own captcha-detection extension, whose injected code can
    look like a page's own captcha markers; `captcha_solver.py` strips
    that out before matching, so it isn't confused with a real widget.)
  This project does not patch the
  browser to spoof its own automation fingerprint (`navigator.webdriver`
  and similar) — it stays within Playwright/Selenium/pyppeteer's stock
  behaviour and the paid products' own legitimate anti-detection
  measures. All three engines' OWN control flow (pagination, retry,
  dedup, output/exit-code contract) has separately been exercised for
  real against a local server replaying genuine, trimmed stockx.com
  captures — see `tests/fixtures/` — so what's unverified specifically is
  getting a real automated session past this detection, not the
  parsing/engine logic once a real page is in hand.
- **2Captcha's REST surface (`api.2captcha.com`) has been exercised for
  real, with a real key** — not just the bogus-key error path
  (`createTask`/`getBalance`/Fingerprint API all correctly reject an
  invalid key with the genuine `ERROR_KEY_DOES_NOT_EXIST` /HTTP 401), but
  a live key too: `get_balance()` and `random_fingerprint()` both
  succeeded against production `api.2captcha.com` and returned real data.
  **The Scraping Browser API (`--cdp-endpoint`, `cb.2captcha.com:9222`)
  could not be completed from either development sandbox tried** — the
  TCP/CONNECT tunnel to it is reachable in both, but the environment's
  own egress security layer explicitly refused to forward the connection
  once it saw credentials going out over a non-TLS (plain WebSocket)
  connection ("refusing to forward injected credentials over non-TLS
  port"); a manual relay built to test around that was deliberately
  abandoned rather than pursued once that refusal was clearly a security
  policy, not a fluke. The residential proxy (`STOCKX_PROXY` /
  `eu.proxy.2captcha.com`) hit the same wall from a different angle: both
  sandboxes route ALL outbound traffic through their own controlling
  proxy and refuse any direct DNS/connection to a third-party proxy
  host, so it can't be used as an actual forward proxy from either shell
  either — `proxy_pool.parse_proxy_line()` was confirmed to parse a real
  `host:port:login:password` proxy credential correctly, but an actual
  request through it was never made. **Both need to be tried from a
  network without this restriction** — your own machine's normal
  terminal (not a sandboxed one), or GitHub Actions once this is pushed —
  to get a real answer.
- **Captcha solving itself was never exercised against a real challenge.**
  No CAPTCHA was ever shown during this project's own testing (plain
  headless Chromium, no key, cleared stockx.com's Cloudflare checks
  every time — see "What was actually measured"), so there was nothing
  real to solve, even with a working key in hand — the classic
  `createTask`/`getTaskResult` flow is only verified against its error
  paths and a valid-key no-op (`getBalance`), not an actual solve.
  `identify_widget()`'s sitekey extraction is therefore
  verified only against synthetic markup (see its tests in
  `smoke_test.py`), not a real StockX challenge page.

## Development

```bash
python3 smoke_test.py     # or: pytest tests/test_smoke.py
```

Passes with **no** engine library installed at all (each engine guards its
driver import behind a module-level `try/except ImportError`, and
`smoke_test.py` asserts that guard exists via an AST check — a lazily-done
import inside the launch path would let the module "pass" without ever
proving the guard works).

**Testing against the live site, with real credentials**: see
[`TESTING.md`](TESTING.md) — a step-by-step checklist for everything this
repo's own build environment could not verify (outbound access to
stockx.com was blocked there): the free path against the real site, each
engine for real, the 2Captcha REST API and Scraping Browser API with a
live key, and the residential proxy.

## License

MIT — see `LICENSE`.
