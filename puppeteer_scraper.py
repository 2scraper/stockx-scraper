#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same flags, same exit codes and status semantics —
see output_writer.finish_run). Not the primary engine (Playwright is); kept
for parity, and because it — like Playwright, unlike Selenium — CAN open an
authenticated `ws://login:pass@host:port` CDP session, so it is the second
engine (after Playwright) able to use the Scraping Browser API's own
`Captcha.setAutoSolve`.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

One real architectural difference from playwright_scraper.py, not a
shortcut: Chromium's `--proxy-server` is a LAUNCH-time flag for the whole
browser PROCESS, unlike Playwright's per-context `proxy=`. So here, unlike
the Playwright engine, "a rotation is a fresh browser" is literal for every
single proxy change, and concurrent workers with different exits each need
their own `launch()`, not just their own page.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download,
which many restricted CI/sandbox networks cannot reach), otherwise
pyppeteer's own default.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlparse

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import product_parser as pp
from captcha_solver import detect_from_html, solve_when_blocked
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH, Product, finish_run, merge_pages, sku_key as _sku_key
from proxy_pool import Proxy, ProxyPool, is_proxy_dead_error, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "puppeteer"
NAV_TIMEOUT_MS = 30_000
READINESS_WAIT_S = 2.5

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="stockx.com listing/product scraper — pyppeteer (Puppeteer) engine",
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
    p.add_argument("--cdp-endpoint", default=None)
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


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        # Same leak as Playwright's connect_over_cdp (CLAUDE.md §8): a
        # failed connection's exception can carry the endpoint URL —
        # login:password included — straight into whatever catches it.
        # Redact before it ever reaches a log, and drop the original from
        # the chain (`from None`) so its own unredacted text can't surface
        # via "the above exception was the direct cause of…".
        try:
            return await pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None)
        except Exception as exc:
            raise RuntimeError(f"CDP connection failed: {redact_credentials(str(exc))}") from None
    args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if proxy is not None:
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    if proxy is not None:
        auth = proxy.pyppeteer_auth_dict()
        if auth:
            await page.authenticate(auth)


async def _enable_scraping_browser_auto_solve(page) -> None:
    """Same rationale as playwright_scraper.py's version: only meaningful
    over --cdp-endpoint, where 2Captcha's own Captcha CDP domain detects,
    solves and injects the token inside their infrastructure."""
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _fetch(page, url: str, *, retries: int, retry_delay: float,
                  proxy_pool: Optional[ProxyPool], current_proxy: Optional[Proxy]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Returns (html, http_status, error) — see playwright_scraper._fetch's
    docstring for why http_status matters: a 403/429/5xx is a hard fact from
    the server, independent of what BOT_CHALLENGE_MARKERS/APP_ERROR_MARKERS
    know to look for in the body."""
    last_error = None
    for attempt in range(retries + 1):
        try:
            response = await page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
            await asyncio.sleep(READINESS_WAIT_S)
            html = await page.content()
            if proxy_pool is not None and current_proxy is not None:
                proxy_pool.report_success(current_proxy)
            status = response.status if response is not None else None
            return html, status, None
        except (NetworkError, PageError, PyppeteerTimeoutError, Exception) as exc:  # noqa: BLE001
            message = str(exc)
            last_error = message
            dead = is_proxy_dead_error(message)
            if proxy_pool is not None and current_proxy is not None and dead:
                proxy_pool.report_failure(current_proxy, dead=True)
                log.warning("Proxy reported dead: %s", message)
            else:
                log.warning("Fetch attempt %d/%d failed for %s: %s", attempt + 1, retries + 1, url, message)
            if attempt < retries:
                await asyncio.sleep(retry_delay)
    return None, None, last_error


async def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    result = solve_when_blocked(client=client, page_url=url, html=html, count_product_links=pp.count_product_links)
    action = result.get("action")
    if action == "warning_no_key":
        log.warning("Captcha solving skipped: %s", result.get("detail"))
    elif action == "warning_solver_error":
        log.warning("Captcha solve failed: %s", result.get("detail"))
    elif action == "solved":
        log.info("Captcha solved via 2Captcha (%s). Token obtained but only "
                  "auto-injected when running via --cdp-endpoint (Scraping "
                  "Browser API's own Captcha domain) — see module docstring.",
                  result.get("captcha_type"))
    return result


async def scrape_listing(
    *, args: argparse.Namespace, start_url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
) -> Tuple[List[List[Product]], List[int], bool, bool]:
    pages_products: List[List[Product]] = []
    failed_pages: List[int] = []
    blocked = False

    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    page = await browser.newPage()
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)

    async def fetch_page(n: int, pg) -> Optional[str]:
        url = pp.page_url(start_url, n)
        html, status, error = await _fetch(pg, url, retries=args.retries, retry_delay=args.retry_delay,
                                            proxy_pool=proxy_pool, current_proxy=proxy)
        nonlocal blocked
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
        await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
        return html

    def safe_parse(n: int, html_text: str):
        """Mirrors playwright_scraper.py's safe_parse: `_fetch()` already
        contains every NETWORK-level failure, but an unexpected exception
        INSIDE parsing had no equivalent containment and would crash the
        whole run via run()'s outer `except Exception`, discarding every
        already-collected page — see playwright_scraper.py's docstring for
        the live reproduction and the CLAUDE.md §10 checklist item this
        closes."""
        try:
            return pp.parse_listing_page(html_text, requested_url=start_url)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            log.error("Page %d fetched OK but failed to parse — treating as failed, not crashing: %s", n, exc)
            failed_pages.append(n)
            return None

    html = await fetch_page(1, page)
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

    if remaining and args.concurrency <= 1:
        for n in remaining:
            if args.proxy_rotate and proxy_pool is not None:
                # --proxy-server is launch-time here: a real rotation means
                # a whole new browser process, not just a new page.
                await browser.close()
                proxy = proxy_pool.next()
                browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
                page = await browser.newPage()
                await _authenticate_if_needed(page, proxy)
            await asyncio.sleep(args.delay)
            html = await fetch_page(n, page)
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
            nonlocal blocked
            async with sem:
                worker_proxy = proxy_pool.worker_view(worker_index, args.concurrency).next() if proxy_pool else None
                worker_browser = await _launch(headless=args.headless, proxy=worker_proxy, cdp_endpoint=None)
                worker_page = await worker_browser.newPage()
                await _authenticate_if_needed(worker_page, worker_proxy)
                await asyncio.sleep(args.delay * (worker_index % max(1, args.concurrency)))
                html, status, _ = await _fetch(worker_page, pp.page_url(start_url, n), retries=args.retries,
                                                retry_delay=args.retry_delay, proxy_pool=proxy_pool, current_proxy=worker_proxy)
                if status is not None and status >= 400:
                    log.warning("Page %d returned HTTP %d — treating as blocked, not empty.", n, status)
                    blocked = True
                if html is not None:
                    if args.dump_html:
                        Path(_dump_path(args.out, n)).write_text(html, encoding="utf-8")
                    if detect_from_html(html, pp.APP_ERROR_MARKERS):
                        blocked = True
                    await _maybe_solve_captcha(html=html, url=pp.page_url(start_url, n), client=client, policy=args.solve_captcha)
                    listing = safe_parse(n, html)
                    if listing is not None:
                        results[n] = listing.products
                else:
                    failed_pages.append(n)
                await worker_browser.close()

        await asyncio.gather(*(worker(n, i) for i, n in enumerate(remaining)))
        for n in sorted(results):
            pages_products.append(results[n])

    await browser.close()
    return pages_products, failed_pages, blocked, False


async def scrape_single_product(
    *, args: argparse.Namespace, url: str,
    proxy_pool: Optional[ProxyPool], client: Optional[TwoCaptchaClient], autosolve: bool = False,
) -> Tuple[List[Product], bool, bool]:
    proxy = proxy_pool.next() if proxy_pool else None
    log.info("Using proxy %s", proxy.masked() if proxy else "(no local proxy pool — direct connection, or a --cdp-endpoint session providing its own exit)")
    browser = await _launch(headless=args.headless, proxy=proxy, cdp_endpoint=args.cdp_endpoint)
    page = await browser.newPage()
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)
    html, status, error = await _fetch(page, url, retries=args.retries, retry_delay=args.retry_delay,
                                        proxy_pool=proxy_pool, current_proxy=proxy)
    blocked = False
    if html is None:
        await browser.close()
        log.error("Single-product fetch failed: %s", error)
        return [], False, True
    if status is not None and status >= 400:
        log.warning("Product page returned HTTP %d — treating as blocked, not empty.", status)
        blocked = True
    if args.dump_html:
        Path(_dump_path(args.out, 1)).write_text(html, encoding="utf-8")
    if detect_from_html(html, pp.APP_ERROR_MARKERS):
        blocked = True
    await _maybe_solve_captcha(html=html, url=url, client=client, policy=args.solve_captcha)
    product = pp.parse_product_detail(html, requested_url=url)
    await browser.close()
    if product is None:
        if pp.count_product_links(html) == 0:
            blocked = True
        return [], blocked, False
    return [product], blocked, False


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
        return EXIT_CRASH

    start_url = _resolve_start_url(args)
    if not start_url:
        print("Error: provide --url or --category", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print(f"Error: unsupported --format {args.format!r}", file=sys.stderr)
        return EXIT_BAD_USAGE
    if args.concurrency > 1 and args.cdp_endpoint:
        # Same limit as playwright_scraper.py: the Scraping Browser API
        # allows one live connection per profile, so concurrent workers
        # collide (profile_locked) rather than parallelizing. Refuse
        # outright instead of silently falling back to unprotected local
        # browsers for the concurrent pages.
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
    autosolve = bool(args.cdp_endpoint) and args.solve_captcha != "off"

    try:
        single_mode = pp.is_single_product_url(start_url) and not pp.is_listing_url(start_url)
        if single_mode:
            products, blocked, remote_api_error = await scrape_single_product(
                args=args, url=start_url, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
            )
            pages_completed = 1 if products or not blocked else 0
            failed_pages = [] if products or blocked else [1]
            merged = products
        else:
            pages_products, failed_pages, blocked, remote_api_error = await scrape_listing(
                args=args, start_url=start_url, proxy_pool=proxy_pool, client=client, autosolve=autosolve,
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
        return asyncio.get_event_loop().run_until_complete(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
