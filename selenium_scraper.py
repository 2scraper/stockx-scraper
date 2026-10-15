#!/usr/bin/env python3
"""selenium_scraper.py — Selenium engine, parity copy of playwright_scraper.py
(same flags, same exit codes, same status semantics — see
output_writer.finish_run). Selenium is not the primary engine (Playwright
is); it exists for parity, not because it is preferred.

Two hard engine limits, stated here rather than left to be discovered:

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
from captcha_solver import SolveBudget, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, RunFlags, finish_run, merge_pages, sku_key as _sku_key
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


def _positive_int(value: str) -> int:
    """argparse type: rejects 0 and negative values (same as the Playwright
    engine — the three engines accept the same values)."""
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
    """`--retries` counts EXTRA attempts, so 0 is legal (one navigation);
    -1 used to mean zero navigations and a run that never fetched."""
    ivalue = int(value)
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return ivalue


def _nonnegative_float(value: str) -> float:
    fvalue = float(value)
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0 (got {value!r})")
    return fvalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="stockx.com listing/product scraper — Selenium engine",
        epilog="Credentials belong in .env / STOCKX_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None)
    p.add_argument("--category", default=None)
    p.add_argument("--pages", type=_positive_int, default=1)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--delay", type=_nonnegative_float, default=1.5)
    p.add_argument("--retries", type=_nonnegative_int, default=2)
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--concurrency", type=_positive_int, default=1)
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-rotate", action="store_true")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=_nonnegative_int, default=3)
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


# Solver outcomes that mean "a captcha gates this page and nothing cleared
# it". Combined with zero product links, the page is blocked.
_UNCLEARED_CAPTCHA = ("warning_no_key", "warning_solver_error", "detected_unidentified_widget",
                      "detected_not_solved", "budget_exhausted")


def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str,
                         budget: SolveBudget) -> Optional[dict]:
    """Detection always runs (it is free); a paid solve never does here.
    Selenium cannot open the authenticated CDP session the Scraping Browser
    API's own solver needs, and this engine does not inject a token into
    the page (that step is widget-specific and unverified), so
    `can_use_token=False` and no solve is bought."""
    if policy == "off":
        return None
    result = solve_when_blocked(client=client, page_url=url, html=html, count_product_links=pp.count_product_links,
                                budget=budget, can_use_token=False)
    action = result.get("action")
    if action == "detected_not_solved":
        log.warning("A %s captcha gates this page. No solve was bought: the Selenium engine does "
                    "not inject a solver token — use playwright_scraper.py or puppeteer_scraper.py "
                    "with --cdp-endpoint for the Scraping Browser API's in-session solve.",
                    result.get("captcha_type"))
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    return result


def _captcha_blocks(result: Optional[dict], html: str) -> bool:
    return bool(result) and result.get("action") in _UNCLEARED_CAPTCHA and pp.count_product_links(html) == 0


def scrape_listing(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
) -> Tuple[List[List[Product]], List[int], RunFlags]:
    """Mirrors playwright_scraper.scrape_listing: (pages_products,
    failed_pages, flags); a page counts as completed only when it was
    served AND parsed."""
    pages_products: List[List[Product]] = []
    failed_pages: List[int] = []
    flags = RunFlags()

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=None)

    def fetch_page(n: int, drv, cur_proxy: Optional[Proxy]):
        """Returns the parsed listing, or None after recording the page as
        failed and its cause in `flags`. Thread-safe enough for the worker
        pool: list.append and attribute stores are atomic under the GIL."""
        url = pp.page_url(start_url, n)
        budget = SolveBudget()  # one per page, created before any retry
        html, status, error = _fetch(drv, url, retries=args.retries, retry_delay=args.retry_delay,
                                     proxy_pool=proxy_pool, current_proxy=cur_proxy)
        if html is None:
            log.error("Page %d permanently failed: %s", n, error)
            failed_pages.append(n)
            return None
        if args.dump_html:
            Path(_dump_path(args.out, n)).write_text(html, encoding="utf-8")
        state = pp.classify_served_page(html, status)
        if state == "ok":
            captcha_result = _maybe_solve_captcha(html=html, url=url, client=client,
                                                  policy=args.solve_captcha, budget=budget)
            if _captcha_blocks(captcha_result, html):
                state = "blocked"
        if state == "blocked":
            log.warning("Page %d is a block page (HTTP %s) — recorded as failed, not empty.", n, status)
            flags.blocked = True
            failed_pages.append(n)
            return None
        if state == "not_found":
            log.warning("Page %d answered HTTP %s — the address does not exist.", n, status)
            flags.not_found = True
            failed_pages.append(n)
            return None
        try:
            listing = pp.parse_listing_page(html, requested_url=start_url)
        except Exception as exc:  # noqa: BLE001
            # An unexpected exception INSIDE parsing must not crash the run
            # and discard every page already collected (in a worker thread
            # it would re-raise in the main thread when pool.map's result is
            # consumed). One bad page degrades the run to partial.
            log.error("Page %d fetched OK but failed to parse — treating as failed, not crashing: %s", n, exc)
            flags.parser_failed = True
            failed_pages.append(n)
            return None
        if listing.parser_failed:
            if pp.has_challenge_marker(html):
                log.warning("Page %d is a Cloudflare interstitial — recorded as blocked.", n)
                flags.blocked = True
            else:
                log.error("Page %d was served but no rows could be read (data block found: %s, "
                          "rows the page reports: %s) — a parser failure, not an empty listing.",
                          n, listing.data_found, listing.total)
                flags.parser_failed = True
            failed_pages.append(n)
            return None
        return listing

    listing = fetch_page(1, driver, proxy)
    reported_page_count = None
    seen_skus: set = set()
    if listing is not None:
        pages_products.append(listing.products)
        seen_skus.update(_sku_key(p) for p in listing.products)
        reported_page_count = listing.page_count

    total_pages = args.pages
    if reported_page_count is not None and reported_page_count < total_pages:
        total_pages = reported_page_count
        flags.stop_reason = "site_last_page"
    else:
        flags.stop_reason = "requested_pages_done"
    remaining = list(range(2, total_pages + 1)) if listing is not None else []

    if remaining:
        if args.concurrency <= 1:
            for n in remaining:
                if getattr(args, "proxy_rotate", False) and proxy_pool is not None:
                    # A rotation is a fresh browser: chromedriver's
                    # --proxy-server is fixed at launch.
                    driver.quit()
                    proxy = proxy_pool.next()
                    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=None)
                time.sleep(args.delay)
                listing = fetch_page(n, driver, proxy)
                if listing is None:
                    continue
                pages_products.append(listing.products)
                page_skus = {_sku_key(p) for p in listing.products}
                if page_skus <= seen_skus:
                    flags.stop_reason = "no_new_rows"
                    break  # data-based termination: this page added no NEW sku
                seen_skus |= page_skus
        else:
            # Selenium has no async runtime: "a worker owns its browser" is
            # literal here — one full webdriver.Chrome per thread.
            results: dict = {}

            def worker(n: int, worker_index: int):
                worker_proxy = proxy_pool.worker_view(worker_index, args.concurrency).next() if proxy_pool else None
                worker_driver = _build_driver(headless=args.headless, proxy=worker_proxy, cdp_endpoint=None)
                try:
                    worker_listing = fetch_page(n, worker_driver, worker_proxy)
                    if worker_listing is not None:
                        results[n] = worker_listing.products
                finally:
                    worker_driver.quit()

            # `worker(n, worker_index)` wants (real page number, loop index)
            # in that order — the SAME order the other two engines pass.
            # `enumerate(remaining)` yields (index, page_number); passing that
            # tuple straight in once fed it BACKWARDS and fetched page 0 and
            # page 1 instead of 2 and 3 (live-reproduced 2026-09-14).
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                list(pool.map(lambda item: worker(*item), ((n, i) for i, n in enumerate(remaining))))
            for n in sorted(results):
                pages_products.append(results[n])

    driver.quit()
    return pages_products, failed_pages, flags


def scrape_single_product(
    *, args: argparse.Namespace, url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient],
) -> Tuple[List[Product], List[int], RunFlags]:
    flags = RunFlags(stop_reason="single_product")
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    driver = _build_driver(headless=args.headless, proxy=proxy, cdp_endpoint=None)
    budget = SolveBudget()
    try:
        html, status, error = _fetch(driver, url, retries=args.retries, retry_delay=args.retry_delay,
                                     proxy_pool=proxy_pool, current_proxy=proxy)
        if html is None:
            log.error("Single-product fetch failed: %s", error)
            return [], [1], flags
        if args.dump_html:
            Path(_dump_path(args.out, 1)).write_text(html, encoding="utf-8")
        state = pp.classify_served_page(html, status)
        if state == "ok":
            captcha_result = _maybe_solve_captcha(html=html, url=url, client=client,
                                                  policy=args.solve_captcha, budget=budget)
            if _captcha_blocks(captcha_result, html):
                state = "blocked"
    finally:
        driver.quit()
    if state == "blocked":
        log.warning("Product page is a block page (HTTP %s) — treating as blocked, not empty.", status)
        flags.blocked = True
        return [], [1], flags
    if state == "not_found":
        log.warning("Product page answered HTTP %s — no such product at %s.", status, url)
        flags.not_found = True
        return [], [1], flags
    product = pp.parse_product_detail(html, requested_url=url)
    if product is None:
        if pp.has_challenge_marker(html) or pp.count_product_links(html) == 0:
            flags.blocked = True
        else:
            log.error("Product page was served but neither JSON-LD nor GetProduct could be read — "
                      "a parser failure, not a missing product.")
            flags.parser_failed = True
        return [], [1], flags
    return [product], [], flags


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
            merged, failed_pages, flags = scrape_single_product(
                args=args, url=start_url, proxy_pool=proxy_pool, client=client,
            )
            pages_completed = 0 if failed_pages else 1
        else:
            pages_products, failed_pages, flags = scrape_listing(
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
        blocked=flags.blocked, remote_api_error=flags.remote_api_error, not_found=flags.not_found,
        parser_failed=flags.parser_failed, stop_reason=flags.stop_reason, allow_empty=args.allow_empty,
        started_at=started_at, price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    if args.url and args.category:
        parser.error("--url and --category both name the start page; pass one of them")
    args = env_config.apply_env(args)
    try:
        return run(args)
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
