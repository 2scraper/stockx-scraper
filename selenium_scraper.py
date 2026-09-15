#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of playwright_scraper.py
(same flags, same exit codes, same status semantics — see
output_writer.finish_run). Selenium is not the primary engine (Playwright
is); it exists for parity, not because it is preferred.

Two hard engine limits, stated here rather than left to be discovered (see
also the family CLAUDE.md §6):

  - Selenium CANNOT use an authenticated remote CDP endpoint.
    chromedriver's `debuggerAddress` takes a bare `host:port`; Playwright's
    `connect_over_cdp` and pyppeteer's `connect` take a full
    `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
    A `--cdp-endpoint` carrying credentials (the Scraping Browser API
    shape) is refused outright here with EXIT_BAD_USAGE — no half-working
    attempt — with a pointer to playwright_scraper.py / puppeteer_scraper.py.
  - Selenium's `--proxy-server` CANNOT authenticate at all. A `--proxy`
    with a login/password has its credentials stripped before being handed
    to Chrome, and this engine WARNS rather than silently dropping them —
    never let a user believe a user:pass URL did something it didn't.

Chrome binary: normally auto-detected by Selenium/Selenium Manager from a
regular Chrome/Chromium install. If you'd rather point it at a specific
binary (e.g. reusing Playwright's own downloaded Chromium, so you don't
need a second browser install just to run this engine), set `CHROME_BIN`
or `SELENIUM_CHROME_BIN` — same idea as `PYPPETEER_EXECUTABLE_PATH` in
puppeteer_scraper.py. chromedriver itself still needs to be resolvable
(on `PATH`, or via Selenium Manager if your network reaches Google's
download hosts) and its major version needs to match the Chrome binary's.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    webdriver = None
    WebDriverException = Exception
    Options = None
    Service = None
    _SELENIUM_IMPORT_ERROR = _IMPORT_ERROR
else:
    _SELENIUM_IMPORT_ERROR = None

import env_config
import product_parser as pp
from captcha_solver import detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, is_proxy_dead_error, load_proxies
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "selenium"
NAV_TIMEOUT_S = 30
READINESS_WAIT_S = 2.5
_CHROME_BINARY = os.environ.get("SELENIUM_CHROME_BIN") or os.environ.get("CHROME_BIN")

# Selenium Manager (invoked transparently by `webdriver.Chrome()` below,
# whenever `Service()` isn't handed an explicit chromedriver path) phones
# home to plausible.io with usage stats by default — confirmed live
# 2026-09-14 ("Error sending stats to Plausible: ..." in stderr on every
# run). That directly contradicts SECURITY.md's "nothing here phones
# home" — the only calls this project's OWN code makes are to the target
# site and, when configured, 2Captcha, but Selenium Manager is third-party
# code this engine invokes, not something this codebase controls the
# network behaviour of by omission. `SE_AVOID_STATS` is Selenium Manager's
# own documented opt-out (its CLI's `avoid_stats` flag, exposed as an
# `SE_`-prefixed env var) — set as a default (never overriding a value the
# caller already set) so this engine's own promise holds without the
# caller having to know Selenium Manager exists. This does not stop the
# separate driver-VERSION-CHECK network call Selenium Manager makes when no
# matching chromedriver is already resolvable on PATH / SELENIUM_CHROME_BIN
# — see SECURITY.md for that caveat.
os.environ.setdefault("SE_AVOID_STATS", "true")

log = logging.getLogger("selenium_scraper")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="stockx.com listing/product scraper — Selenium engine",
        epilog="Credentials belong in .env / STOCKX_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--delay", type=float, default=1.5)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", action="store_true")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None)
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3)
    p.add_argument("--cdp-endpoint", default=None,
                    help="NOTE: refused if it carries credentials — Selenium cannot authenticate a remote CDP session")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"stockx_products.{fmt}"


def _resolve_start_url(args: argparse.Namespace) -> Optional[str]:
    if args.url:
        return args.url
    if args.category:
        if args.category in pp.CATEGORY_SLUGS:
            return pp.category_page_url(args.category)
        if args.category in pp.DEMOGRAPHIC_SLUGS:
            return pp.demographic_page_url(args.category)
        log.warning("Unrecognised --category %r — trying it as a /category/ slug anyway", args.category)
        return pp.category_page_url(args.category)
    return None


def _dump_path(out_path: str, page_num: int) -> str:
    stem = Path(out_path).with_suffix("")
    return f"{stem}_p{page_num}_debug.html"


def _cdp_endpoint_has_credentials(cdp_endpoint: str) -> bool:
    parts = urlparse(cdp_endpoint)
    return bool(parts.username or parts.password)


def _build_driver(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    if _CHROME_BINARY:
        options.binary_location = _CHROME_BINARY

    if cdp_endpoint:
        # Bare host:port only — no credentials possible here (checked by
        # the caller before we get this far; see run()'s upfront refusal).
        options.debugger_address = urlparse(cdp_endpoint).netloc.split("@")[-1]
        return webdriver.Chrome(options=options)

    if proxy is not None:
        if proxy.has_auth:
            log.warning(
                "Selenium's --proxy-server cannot authenticate — using %s:%s "
                "with credentials STRIPPED, not silently dropped.",
                proxy.host, proxy.port,
            )
        options.add_argument(f"--proxy-server={proxy.server_only()}")

    service = Service() if Service else None
    return webdriver.Chrome(service=service, options=options) if service else webdriver.Chrome(options=options)


# Selenium/chromedriver has no equivalent of Playwright's/pyppeteer's
# navigation Response object — the WebDriver protocol simply does not
# expose the main-frame HTTP status. Chrome's own Navigation Timing Level 2
# API does, though (`responseStatus`, Chrome 109+), so we read it back via
# a script instead of leaving this engine blind where its two siblings are
# not. Returns 0 (falsy) rather than raising on an older Chrome or a
# cross-origin redirect chain the API won't reveal — 0 is treated as
# "unknown", never as "known to be fine".
_STATUS_JS = (
    "try { return performance.getEntriesByType('navigation')[0].responseStatus || 0; } "
    "catch (e) { return 0; }"
)


def _fetch(driver, url: str, *, retries: int, retry_delay: float,
           proxy_pool: Optional[ProxyPool], current_proxy: Optional[Proxy]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Returns (html, http_status, error) — see playwright_scraper._fetch's
    docstring for why http_status matters; here it comes from the
    Navigation Timing API rather than a driver-native Response object (see
    _STATUS_JS above), and is None when the browser could not report one at
    all (nothing to act on, distinct from a confirmed-good 2xx)."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            driver.set_page_load_timeout(NAV_TIMEOUT_S)
            driver.get(url)
            time.sleep(READINESS_WAIT_S)
            html = driver.page_source
            status = None
            try:
                reported = driver.execute_script(_STATUS_JS)
                status = int(reported) if reported else None
            except WebDriverException:
                pass  # older Chrome / API unsupported — status stays unknown, not a fetch failure
            if proxy_pool is not None and current_proxy is not None:
                proxy_pool.report_success(current_proxy)
            return html, status, None
        except WebDriverException as exc:
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and current_proxy is not None and dead:
                proxy_pool.report_failure(current_proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Fetch attempt %d/%d failed for %s: %s", attempt + 1, retries + 1, url, message)
            if attempt < retries:
                time.sleep(retry_delay)
    return None, None, last_error


def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(client=client, page_url=url, html=html, count_product_links=pp.count_product_links)
    action = result.get("action")
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        # Selenium has no equivalent to the Scraping Browser API's own
        # Captcha.setAutoSolve CDP domain (that requires an authenticated
        # CDP session, which Selenium cannot open at all — see module
        # docstring). A token IS obtained here but is NOT injected into
        # the page: doing that would mean guessing this widget's own
        # callback, which this codebase declines to do without a real
        # captcha instance to verify against (see captcha_solver.py).
        log.warning(
            "Captcha token obtained but NOT auto-injected on the Selenium "
            "engine (unverified widget-specific step) — use "
            "playwright_scraper.py or puppeteer_scraper.py with "
            "--cdp-endpoint for the Scraping Browser API's built-in solve."
        )
    return result


def scrape_listing(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
) -> Tuple[List[List[Product]], List[int], bool, bool]:
    pages_products: List[List[Product]] = []
    failed_pages: List[int] = []
    blocked = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=None)

    def fetch_page(n: int) -> Optional[str]:
        nonlocal blocked
        url = pp.page_url(start_url, n)
        html, status, error = _fetch(driver, url, retries=args.retries, retry_delay=args.retry_delay,
                                      proxy_pool=proxy_pool, current_proxy=proxy)
        if html is None:
            log.error("Page %d permanently failed: %s", n, error)
            failed_pages.append(n)
            return None
        if status is not None and status >= 400:
            log.warning("Page %d returned HTTP %d — treating as blocked, not empty.", n, status)
            blocked = True
        if args.dump_html:
            Path(_dump_path(args.out, n)).write_text(html, encoding="utf-8")
        if detect_from_html(html, pp.APP_ERROR_MARKERS):
            blocked = True
        _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
        return html

    def safe_parse(n: int, html_text: str):
        """Mirrors playwright_scraper.py's safe_parse: `_fetch()` already
        contains every NETWORK-level failure, but an unexpected exception
        INSIDE parsing had no equivalent containment and would crash the
        whole run via run()'s outer `except Exception`, discarding every
        already-collected page — see playwright_scraper.py's docstring for
        the live reproduction and the CLAUDE.md §10 checklist item this
        closes. Here the concurrent path is a ThreadPoolExecutor, not
        asyncio, but the same gap applied identically: an unhandled
        exception in a worker thread's `pool.map()` call re-raises in the
        main thread the moment its result is consumed, which is exactly
        this function's caller."""
        try:
            return pp.parse_listing_page(html_text, requested_url=start_url)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            log.error("Page %d fetched OK but failed to parse — treating as failed, not crashing: %s", n, exc)
            failed_pages.append(n)
            return None

    html = fetch_page(1)
    reported_page_count = None
    seen_skus: set = set()
    if html is not None:
        listing = safe_parse(1, html)
        if listing is not None:
            pages_products.append(listing.products)
            seen_skus.update(_sku_key(p) for p in listing.products)
            reported_page_count = listing.page_count
            if not listing.products and listing.page is None and detect_from_html(html, pp.BOT_CHALLENGE_MARKERS):
                blocked = True

    total_pages = args.pages
    if reported_page_count is not None:
        total_pages = min(total_pages, reported_page_count)
    remaining = list(range(2, total_pages + 1))

    if remaining:
        if args.concurrency <= 1:
            for n in remaining:
                time.sleep(args.delay)
                html = fetch_page(n)
                if html is None:
                    continue
                listing = safe_parse(n, html)
                if listing is None:
                    continue
                pages_products.append(listing.products)
                page_skus = {_sku_key(p) for p in listing.products}
                if page_skus <= seen_skus:
                    break  # data-based termination: this page added no NEW sku
                seen_skus |= page_skus
        else:
            # Selenium has no async runtime: "a worker owns its browser" is
            # literal here — one full webdriver.Chrome per thread.
            results: dict = {}

            def worker(n: int, worker_index: int):
                nonlocal blocked
                worker_proxy = proxy_pool.worker_view(worker_index, args.concurrency).next() if proxy_pool else None
                worker_driver = _build_driver(headless=args.headless, proxy=worker_proxy, cdp_endpoint=None)
                try:
                    html, status, _ = _fetch(worker_driver, pp.page_url(start_url, n), retries=args.retries,
                                              retry_delay=args.retry_delay, proxy_pool=proxy_pool, current_proxy=worker_proxy)
                    if status is not None and status >= 400:
                        log.warning("Page %d returned HTTP %d — treating as blocked, not empty.", n, status)
                        blocked = True
                    if html is not None:
                        if args.dump_html:
                            Path(_dump_path(args.out, n)).write_text(html, encoding="utf-8")
                        if detect_from_html(html, pp.APP_ERROR_MARKERS):
                            blocked = True
                        _maybe_solve_captcha(html=html, url=pp.page_url(start_url, n), client=client, policy=args.solve_captcha)
                        listing = safe_parse(n, html)
                        if listing is not None:
                            results[n] = listing.products
                    else:
                        failed_pages.append(n)
                finally:
                    worker_driver.quit()

            # `worker(n, worker_index)` wants (real page number, loop index)
            # in that order — the SAME order playwright_scraper.py and
            # puppeteer_scraper.py pass to their own `worker(n, i)`.
            # `enumerate(remaining)` yields (index, page_number), so passing
            # that tuple straight into `worker(*item)` fed it BACKWARDS:
            # `n` received the loop index (0, 1, 2…) instead of the real
            # page number, and `worker_index` received the real page number
            # instead of the index. Live-reproduced 2026-09-14: with
            # `remaining == [2, 3]` this fetched `page_url(start_url, 0)`
            # and `page_url(start_url, 1)` — page 0 doesn't exist and page 1
            # was already fetched — instead of the intended pages 2 and 3,
            # and threw off proxy_pool.worker_view's rotation the same way.
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(lambda item: worker(*item), ((n, i) for i, n in enumerate(remaining))))
            for n in sorted(results):
                pages_products.append(results[n])

    driver.quit()
    return pages_products, failed_pages, blocked, False


def scrape_single_product(
    *, args: argparse.Namespace, url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
) -> Tuple[List[Product], bool, bool]:
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=None)
    html, status, error = _fetch(driver, url, retries=args.retries, retry_delay=args.retry_delay,
                                  proxy_pool=proxy_pool, current_proxy=proxy)
    blocked = False
    if html is None:
        driver.quit()
        log.error("Single-product fetch failed: %s", error)
        return [], False, True
    if status is not None and status >= 400:
        log.warning("Product page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
    if args.dump_html:
        Path(_dump_path(args.out, 1)).write_text(html, encoding="utf-8")
    if detect_from_html(html, pp.APP_ERROR_MARKERS):
        blocked = True
    _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
    product = pp.parse_product_detail(html, requested_url=url)
    driver.quit()
    if product is None:
        if pp.count_product_links(html) == 0:
            blocked = True
        return [], blocked, False
    return [product], blocked, False


def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    if webdriver is None:
        print(f"Error: selenium is not installed ({_SELENIUM_IMPORT_ERROR}). "
              f"pip install -r requirements-selenium.txt", file=sys.stderr)
        return EXIT_CRASH

    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url or --category", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.cdp_endpoint and _cdp_endpoint_has_credentials(args.cdp_endpoint):
        print(
            "Error: --cdp-endpoint carries credentials — Selenium/chromedriver's "
            "debuggerAddress takes a bare host:port and cannot authenticate a "
            "remote session. Use playwright_scraper.py or puppeteer_scraper.py "
            "for the Scraping Browser API.", file=sys.stderr,
        )
        return EXIT_BAD_USAGE
    if args.concurrency > 1 and args.cdp_endpoint:
        # Same limit as the other two engines: the Scraping Browser API
        # allows one live connection per profile, so concurrent workers
        # collide (profile_locked). A bare (uncredentialed) --cdp-endpoint
        # can reach this check even though a credentialed one is already
        # refused above.
        print("Error: --concurrency > 1 is refused with --cdp-endpoint — the Scraping "
              "Browser API allows one live connection per profile, so concurrent "
              "workers would collide. Run multiple single-worker invocations against "
              "different pid- profiles instead.", file=sys.stderr)
        return EXIT_BAD_USAGE
    args.out = args.out or _default_out(args.format)

    proxies = load_proxies(args.proxy, args.proxy_file)
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.concurrency > 1 and proxy_pool is None:
        log.warning(
            "--concurrency %d with no proxy pool sends that many workers' traffic "
            "from ONE address — a faster way to get it scored than to gather data. "
            "Continuing anyway (this is a warning, not a refusal).", args.concurrency,
        )
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

    client = TwoCaptchaClient(args.twocaptcha_key) if (args.twocaptcha_key and args.solve_captcha != "off") else None

    try:
        single_mode = pp.is_single_product_url(start_url) and not pp.is_listing_url(start_url)
        if single_mode:
            products, blocked, remote_api_error = scrape_single_product(
                args=args, url=start_url, proxy_pool=proxy_pool, client=client,
            )
            pages_completed = 1 if products or not blocked else 0
            failed_pages = [] if products or blocked else [1]
            merged = products
        else:
            pages_products, failed_pages, blocked, remote_api_error = scrape_listing(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client,
            )
            merged = merge_pages(pages_products)
            pages_completed = len(pages_products)
        price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    return finish_run(
        products=merged, out_path=args.out, fmt=args.format, engine=ENGINE_NAME, url=start_url,
        pages_requested=args.pages, pages_completed=pages_completed, failed_pages=failed_pages,
        blocked=blocked, remote_api_error=remote_api_error, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
