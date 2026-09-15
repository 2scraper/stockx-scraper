#!/usr/bin/env python3
"""playwright_scraper.py — Playwright engine for the stockx-scraper family
member. Playwright is the primary engine (see selenium_scraper.py /
puppeteer_scraper.py for parity copies — all three must agree on exit
codes, run status and whether a run crashes or spends money).

Example:
    python3 playwright_scraper.py --category sneakers --pages 3 --format json --out stockx_sneakers.json
    python3 playwright_scraper.py --url "https://stockx.com/search?s=jordan+4" --pages 2
    python3 playwright_scraper.py --url "https://stockx.com/air-jordan-4-retro-og-flight-club"   # single product

Measured live, 2026-09-10 (see README "What was actually measured"): an
ordinary local headless Chromium, no proxy, no CAPTCHA key, cleared
stockx.com's Cloudflare bot-management fingerprinting scripts and returned
full listing/product data on every page requested. The paid products (a
`--twocaptcha-key`, `--proxy`, a `--cdp-endpoint` Scraping Browser session)
are for volume, a specific exit country, or a consistent device identity —
not because the free path is blocked. Say so before recommending them.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from playwright.async_api import Browser, BrowserContext, Page, async_playwright
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    Browser = BrowserContext = Page = None
    async_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PLAYWRIGHT_IMPORT_ERROR = None

import env_config
import product_parser as pp
from captcha_solver import detect_from_html, solve_when_blocked
from fingerprint_client import refuse_if_cdp
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "playwright"
BASE_URL = pp.BASE_URL

# --- the handful of engine constants that vary per site (CLAUDE.md §5) ---
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_MS = 2_500
MIN_CARD_MATCHES = pp.MIN_CARD_MATCHES

log = logging.getLogger("playwright_scraper")


def _positive_int(value: str) -> int:
    """argparse type: rejects 0 and negative values. A run audit
    (2026-09-15) found --pages 0 and negative --pages/--concurrency were
    silently accepted and produced a misleading "complete" 0-page run —
    see output_writer.finish_run()'s outcome-precedence fix in the same
    change for the other half of that bug."""
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def _nonnegative_int(value: str) -> int:
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
        description="stockx.com listing/product scraper — Playwright engine",
        epilog="Credentials belong in .env / STOCKX_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    p.add_argument("--url", default=None, help="Full stockx.com URL (search, /category/*, /browse/*, or a single product page)")
    p.add_argument("--category", default=None, help="Shortcut: a known category/demographic slug, e.g. 'sneakers' or 'men'")
    p.add_argument("--pages", type=_positive_int, default=1, help="Listing pages to fetch (ignored in single-product mode)")
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None, help="Output path (default: stockx_products.<format>)")
    p.add_argument("--delay", type=_nonnegative_float, default=1.5, help="Base delay between requests, seconds")
    p.add_argument("--retries", type=_nonnegative_int, default=2, help="Retries per page on failure")
    p.add_argument("--retry-delay", type=_nonnegative_float, default=3.0)
    p.add_argument("--concurrency", type=_positive_int, default=1, help="Concurrent listing-page workers (page 1 is always fetched alone first)")
    p.add_argument("--proxy", default=None, help="A single proxy, e.g. http://login:pass@host:port (or set STOCKX_PROXY)")
    p.add_argument("--proxy-file", default=None, help="One proxy per line, same formats as --proxy")
    p.add_argument("--proxy-rotate", action="store_true", help="Use a fresh proxy (and a fresh browser context) for every page")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None, help="(or set TWOCAPTCHA_KEY)")
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score")
    p.add_argument("--cdp-endpoint", default=None, help="Connect to a remote CDP session (e.g. the 2Captcha Scraping Browser API) instead of launching locally (or set STOCKX_CDP_ENDPOINT)")
    p.add_argument("--allow-empty", action="store_true", help="Write output even if zero products were found")
    p.add_argument("--dump-html", action="store_true", help="Save each fetched page's HTML next to --out, on success too")
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


async def _fetch(
    page: Page, url: str, *, retries: int, retry_delay: float,
    proxy_pool: Optional[ProxyPool], current_proxy: Optional[Proxy],
) -> Tuple[Optional[str], Optional[Proxy], Optional[int], Optional[str]]:
    """Returns (html, proxy_used, http_status, error). A dead-proxy error is
    reported to the pool and swaps to the next exit; a plain timeout retries
    the SAME exit — they are different failures wanting different responses.

    `http_status` is the main-frame navigation response's status, when
    Playwright gives us one — a 403/429/5xx here is a hard fact from the
    server, independent of what the response BODY says, and catches a block
    page whose wording BOT_CHALLENGE_MARKERS/APP_ERROR_MARKERS does not (yet)
    know about. See product_parser.APP_ERROR_MARKERS' docstring for the live
    case (2026-09-13) that was missed without it: a 403 rendering StockX's
    own Next.js error boundary, not a Cloudflare challenge page."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(READINESS_WAIT_MS)
            html = await page.content()
            status = response.status if response is not None else None
            return html, current_proxy, status, None
        except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and current_proxy is not None and dead:
                proxy_pool.report_failure(current_proxy, dead=True)
                current_proxy = proxy_pool.next()
                log.warning("Proxy reported dead, rotating to %s: %s", current_proxy.masked() if current_proxy else "(pool exhausted)", message)
            else:
                log.warning("Fetch attempt %d/%d failed for %s: %s", attempt + 1, retries + 1, url, message)
            if attempt < retries:
                await asyncio.sleep(retry_delay)
    return None, current_proxy, None, last_error


async def _new_context(browser: Browser, proxy: Optional[Proxy]) -> BrowserContext:
    kwargs = {}
    if proxy is not None:
        kwargs["proxy"] = proxy.playwright_proxy_dict()
    return await browser.new_context(**kwargs)


async def _enable_scraping_browser_auto_solve(context: BrowserContext, page: Page) -> None:
    """Only meaningful over --cdp-endpoint: the Scraping Browser API's own
    `Captcha` CDP domain detects and solves a challenge INSIDE 2Captcha's
    infrastructure and injects the token itself — this is the well-founded
    mechanism (2Captcha's own integration contract), unlike a locally-
    launched browser where this codebase would have to guess each widget's
    own callback/injection point (see captcha_solver.solve_when_blocked's
    module docstring on why that part stays unimplemented rather than
    guessed)."""
    try:
        session = await context.new_cdp_session(page)
        session.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        session.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        session.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await session.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(
    *, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str,
) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(
        client=client, page_url=url, html=html, count_product_links=pp.count_product_links,
    )
    action = result.get("action")
    if action == "no_captcha_detected":
        pass
    elif action == "skipped_products_present":
        log.info("Captcha widget present but products already rendered — not solving.")
    elif action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s).", result.get("captcha_type"))
    elif action == "detected_unidentified_widget":
        log.warning("A captcha-like marker was detected but no known widget/sitekey could be extracted.")
    return result


async def _connect_over_cdp(pw, cdp_endpoint: str):
    """Wraps `chromium.connect_over_cdp` so a connection failure never
    reaches a log with the real credential in it. Playwright's own error
    embeds the endpoint URL — login:password included — up to five times
    (the message plus a four-line "Call log"), confirmed live 2026-09-14
    against a bad endpoint. `run()`'s outer `except Exception:
    log.exception(...)` would otherwise print that straight to the log —
    exactly the family bug CLAUDE.md §8 names by name. `from None` drops the
    original exception from the chain so its own unredacted text can never
    surface via "the above exception was the direct cause of…"."""
    try:
        return await pw.chromium.connect_over_cdp(cdp_endpoint)
    except Exception as exc:
        raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None


async def scrape_listing(
    *, args: argparse.Namespace, start_url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
) -> Tuple[List[List[Product]], List[int], bool, bool, Optional[int], Optional[str]]:
    """Returns (pages_products_in_order, failed_page_numbers, blocked,
    remote_api_error, page_count_reported_by_site, dump_out_prefix)."""
    pages_products: List[List[Product]] = []
    failed_pages: List[int] = []
    blocked = False
    remote_api_error = False
    reported_page_count: Optional[int] = None

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    context = await _new_context(browser, proxy)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)

    async def fetch_page(n: int, ctx_page: Page, cur_proxy: Optional[Proxy]) -> Tuple[Optional[str], Optional[Proxy]]:
        url = pp.page_url(start_url, n)
        html, used_proxy, status, error = await _fetch(
            ctx_page, url, retries=args.retries, retry_delay=args.retry_delay,
            proxy_pool=proxy_pool, current_proxy=cur_proxy,
        )
        nonlocal blocked
        if html is None:
            log.error("Page %d permanently failed: %s", n, error)
            failed_pages.append(n)
            return None, used_proxy
        if status is not None and status >= 400:
            # A hard fact from the server, independent of body wording —
            # see _fetch()'s docstring. Report it and stop treating this
            # page as real content, but still let it flow through to
            # dump-html / diagnostics below.
            log.warning("Page %d returned HTTP %d — treating as blocked, not empty.", n, status)
            blocked = True
            if proxy_pool is not None and used_proxy is not None and status in (403, 429):
                proxy_pool.report_failure(used_proxy, dead=True)
        elif status is not None and proxy_pool is not None and used_proxy is not None:
            proxy_pool.report_success(used_proxy)
        if args.dump_html:
            Path(_dump_path(args.out, n)).write_text(html, encoding="utf-8")
        if detect_from_html(html, pp.APP_ERROR_MARKERS) or detect_from_html(html, pp.BOT_SITE_GUARD_MARKERS):
            blocked = True
        captcha_result = await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
        if captcha_result and captcha_result.get("action") in ("warning_no_key", "warning_solver_error", "detected_unidentified_widget"):
            if pp.count_product_links(html) == 0:
                blocked = True
        return html, used_proxy

    def safe_parse(n: int, html_text: str):
        """`_fetch()` already contains every NETWORK-level failure (it
        catches internally and returns `(None, ..., error)` — see its own
        docstring). An unexpected exception INSIDE parsing (a page that
        fetched fine but has a shape product_parser doesn't expect) had no
        equivalent containment: it would propagate out of `scrape_listing`
        through `run()`'s outer `except Exception` and crash the whole run,
        discarding every already-collected page's data — including page 1's,
        fetched synchronously before any concurrent worker even starts.
        Live-reproduced 2026-09-14 via a monkeypatched `parse_listing_page`
        raising on a worker page. CLAUDE.md §10's testing checklist names
        this exact case: "a worker that raises neither hangs the run nor
        loses its siblings' pages." One bad page degrades the run to
        EXIT_PARTIAL, same as a page that failed to fetch — it must never
        turn a partially-successful run into EXIT_CRASH."""
        try:
            return pp.parse_listing_page(html_text, requested_url=start_url)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            log.error("Page %d fetched OK but failed to parse — treating as failed, not crashing: %s", n, exc)
            failed_pages.append(n)
            return None

    # Page 1 is always fetched alone — its content decides whether later
    # pages can be addressed independently (their own ?page=N convention).
    html, proxy = await fetch_page(1, page, proxy)
    seen_skus: set = set()
    if html is not None:
        listing = safe_parse(1, html)
        if listing is not None:
            pages_products.append(listing.products)
            seen_skus.update(_sku_key(p) for p in listing.products)
            reported_page_count = listing.page_count
            if not listing.products and listing.page is None:
                # __NEXT_DATA__ / getDiscoveryData was entirely absent — treat
                # as a possible block rather than silently reporting "empty",
                # but only if a real challenge marker is present (see
                # product_parser.BOT_CHALLENGE_MARKERS on why the ambient
                # Cloudflare script tag alone does NOT count).
                if detect_from_html(html, pp.BOT_CHALLENGE_MARKERS) or detect_from_html(html, pp.BOT_SITE_GUARD_MARKERS):
                    blocked = True

    total_pages = args.pages
    if reported_page_count is not None:
        total_pages = min(total_pages, reported_page_count)

    remaining = list(range(2, total_pages + 1))
    if remaining and args.concurrency <= 1:
        for n in remaining:
            if args.proxy_rotate and proxy_pool is not None:
                await context.close()
                proxy = proxy_pool.next()
                context = await _new_context(browser, proxy)
                page = await context.new_page()
            await asyncio.sleep(args.delay)
            html, proxy = await fetch_page(n, page, proxy)
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
    elif remaining:
        sem = asyncio.Semaphore(max(1, args.concurrency))
        results: dict = {}

        async def worker(n: int, worker_index: int):
            async with sem:
                worker_proxy = proxy_pool.worker_view(worker_index, args.concurrency).next() if proxy_pool else None
                worker_ctx = await _new_context(browser, worker_proxy)
                worker_page = await worker_ctx.new_page()
                await asyncio.sleep(args.delay * (worker_index % max(1, args.concurrency)))
                html, _ = await fetch_page(n, worker_page, worker_proxy)
                await worker_ctx.close()
                if html is not None:
                    listing = safe_parse(n, html)
                    if listing is not None:
                        results[n] = listing.products

        await asyncio.gather(*(worker(n, i) for i, n in enumerate(remaining)))
        for n in sorted(results):  # merge in PAGE order, not arrival order
            pages_products.append(results[n])

    await context.close()
    return pages_products, failed_pages, blocked, remote_api_error, reported_page_count, None


async def scrape_single_product(
    *, args: argparse.Namespace, url: str, browser: Browser,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
) -> Tuple[List[Product], bool, bool]:
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    context = await _new_context(browser, proxy)
    page = await context.new_page()
    if autosolve:
        await _enable_scraping_browser_auto_solve(context, page)
    html, _, status, error = await _fetch(
        page, url, retries=args.retries, retry_delay=args.retry_delay,
        proxy_pool=proxy_pool, current_proxy=proxy,
    )
    blocked = False
    if html is None:
        await context.close()
        log.error("Single-product fetch failed: %s", error)
        return [], False, True
    if status is not None and status >= 400:
        log.warning("Product page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
        if proxy_pool is not None and proxy is not None and status in (403, 429):
            proxy_pool.report_failure(proxy, dead=True)
    elif status is not None and proxy_pool is not None and proxy is not None:
        proxy_pool.report_success(proxy)
    if args.dump_html:
        Path(_dump_path(args.out, 1)).write_text(html, encoding="utf-8")
    if detect_from_html(html, pp.APP_ERROR_MARKERS) or detect_from_html(html, pp.BOT_SITE_GUARD_MARKERS):
        blocked = True
    await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
    product = pp.parse_product_detail(html, requested_url=url)
    await context.close()
    if product is None:
        if pp.count_product_links(html) == 0:
            blocked = True
        return [], blocked, False
    return [product], blocked, False


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    if async_playwright is None:
        print(f"Error: playwright is not installed ({_PLAYWRIGHT_IMPORT_ERROR}). "
              f"pip install -r requirements-playwright.txt && playwright install chromium", file=sys.stderr)
        return EXIT_CRASH
    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url or --category", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.concurrency > 1 and args.cdp_endpoint:
        # The Scraping Browser API allows exactly one live connection per
        # profile — concurrent workers would collide on it (profile_locked)
        # rather than usefully parallelize. Refuse outright rather than
        # attempting a connection the family already knows fails; use
        # several `pid-` profiles (one run each) for real parallelism.
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

    client = None
    if args.twocaptcha_key and args.solve_captcha != "off":
        client = TwoCaptchaClient(args.twocaptcha_key)

    cdp_refused_extras = refuse_if_cdp(args.cdp_endpoint)  # just logs; nothing to refuse here (no --fingerprint flag wired up)
    del cdp_refused_extras

    try:
        async with async_playwright() as pw:
            if args.cdp_endpoint:
                if args.proxy or args.proxy_file:
                    log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
                    proxy_pool = None
                browser = await _connect_over_cdp(pw, args.cdp_endpoint)
            else:
                browser = await pw.chromium.launch(headless=args.headless)

            autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"
            single_mode = pp.is_single_product_url(start_url) and not pp.is_listing_url(start_url)
            if single_mode:
                products, blocked, remote_api_error = await scrape_single_product(
                    args=args, url=start_url, browser=browser, proxy_pool=proxy_pool, client=client,
                    autosolve=autosolve,
                )
                pages_completed = 1 if products or not blocked else 0
                failed_pages = [] if products or blocked else [1]
                merged = products
                price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None
            else:
                pages_products, failed_pages, blocked, remote_api_error, reported_page_count, _ = await scrape_listing(
                    args=args, start_url=start_url, browser=browser, proxy_pool=proxy_pool, client=client,
                    autosolve=autosolve,
                )
                merged = merge_pages(pages_products)
                pages_completed = len(pages_products)
                price_confirmed_pct = (sum(1 for p in merged if p.price is not None) / len(merged)) if merged else None

            await browser.close()
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH

    return finish_run(
        products=merged,
        out_path=args.out,
        fmt=args.format,
        engine=ENGINE_NAME,
        url=start_url,
        pages_requested=args.pages,
        pages_completed=pages_completed,
        failed_pages=failed_pages,
        blocked=blocked,
        remote_api_error=remote_api_error,
        allow_empty=args.allow_empty,
        started_at=started_at,
        price_confirmed_pct=price_confirmed_pct,
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args()
    args = env_config.apply_env(args)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
