#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/real-fixture
checks. No pytest, no conftest, no fixtures directory beyond the real HTML
captures under tests/fixtures/. `tests/test_smoke.py` wraps this as a
single pytest entry point so `pytest` also works, without a second copy of
the checks.

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import ast
import csv
import inspect
import json
import os
import re
import sys
from pathlib import Path

import captcha_solver
import diff_runs
import env_config
import output_writer
import product_parser as pp
import proxy_pool
import puppeteer_scraper
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent
FIXTURES = ROOT / "tests" / "fixtures"

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome. Every check function is conventionally named `_`
    since nothing ever looks it up by name — only RESULTS matters — so the
    decorator must execute eagerly rather than return a callable to invoke
    later, or every `_` would just clobber the previous one."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return fn
    return decorator


# --------------------------------------------------------------------------- #
# Engines must import cleanly with NO engine library installed, and each
# must import its driver at MODULE level (not lazily inside the launch
# path) — otherwise a missing-library skip never actually exercises the
# guard, and CI's "fail on unexpected skip" job can't catch a broken import.
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        assert hasattr(mod, "main"), f"{mod.__name__} has no main()"


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError")
def _():
    for mod, driver_name in (
        (playwright_scraper, "playwright"),
        (selenium_scraper, "selenium"),
        (puppeteer_scraper, "pyppeteer"),
    ):
        source = inspect.getsource(mod)
        tree = ast.parse(source)
        top_level_try_import = False
        for node in tree.body:  # module level only — not inside a function
            if isinstance(node, ast.Try):
                imports_in_try = [n for n in ast.walk(node) if isinstance(n, (ast.Import, ast.ImportFrom))]
                names = " ".join(
                    (n.module or "") + " " + " ".join(a.name for a in n.names)
                    for n in imports_in_try
                )
                if driver_name in names:
                    has_import_error_handler = any(
                        isinstance(h.type, ast.Name) and h.type.id == "ImportError"
                        for h in node.handlers
                    )
                    if has_import_error_handler:
                        top_level_try_import = True
        assert top_level_try_import, (
            f"{mod.__name__} must import {driver_name!r} inside a MODULE-LEVEL "
            f"try/except ImportError — a lazy import inside the launch path "
            f"would let the module import cleanly with the driver missing "
            f"WITHOUT ever exercising the guard, so CI's engine-smoke group "
            f"could never catch a broken import."
        )


# --------------------------------------------------------------------------- #
# Banned wording (CLAUDE.md §12) — the original 2scraper.md brief predates
# this naming and uses some of these; the shipped repo must not.
# --------------------------------------------------------------------------- #
BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "2scraper antidetect browser",
    "gate.2prx.com",
    "--antidetect",
    "antidetect_local_api",
)
SHIPPED_TEXT_GLOBS = ("*.py", "*.md", "*.html", "*.toml", "*.cfg", "*.yml", "*.yaml")
_SKIP_DIRS = {".git", "tests", "__pycache__", ".venv", "venv", "node_modules"}


@check("no banned wording in any shipped file (CLAUDE.md §12)")
def _():
    offenders = []
    for pattern in SHIPPED_TEXT_GLOBS:
        for path in ROOT.glob(pattern):
            if path.name == "smoke_test.py" or path.name == "CLAUDE.md" or path.name.startswith("2scraper"):
                continue  # this file and the reference docs are allowed to NAME the banned phrases
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for phrase in BANNED_PHRASES:
                if phrase in text:
                    offenders.append(f"{path.name}: {phrase!r}")
    assert not offenders, f"banned wording found: {offenders}"


@check("--antidetect flag stays removed from every engine's CLI")
def _():
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        parser = mod.build_arg_parser()
        flags = {opt for action in parser._actions for opt in action.option_strings}
        assert "--antidetect" not in flags, f"{mod.__name__} reintroduced --antidetect"


# --------------------------------------------------------------------------- #
# product_parser against REAL captures (stockx.com, 2026-09-10) — values
# pinned, not just "some rows populated". See tests/fixtures/*.html and
# README "What was actually measured" for provenance.
# --------------------------------------------------------------------------- #
@check("search listing: real values from a live capture")
def _():
    html = (FIXTURES / "search_p1.html").read_text(encoding="utf-8")
    lp = pp.parse_listing_page(html, requested_url="https://stockx.com/search?s=jordan+4")
    assert lp.flow == "SEARCH_RESULTS"
    assert lp.currency == "EUR"
    assert lp.page == 1 and lp.page_count == 25 and lp.total == 1000
    assert len(lp.products) == 6, f"expected 6 trimmed rows, got {len(lp.products)}"
    first = lp.products[0]
    assert first.sku == "air-jordan-4-retro-toro-bravo-2026"
    assert first.title == "Jordan 4 Retro Toro Bravo (2026)"
    assert first.brand == "Jordan"
    assert first.price == 123 and first.lowest_ask == 123
    assert first.highest_bid == 214
    assert first.last_sale == 147
    assert first.price_source == "embedded_json"
    assert first.product_url == "https://stockx.com/air-jordan-4-retro-toro-bravo-2026"
    assert first.category == "search:jordan 4"
    assert first.style_id is None, "styleId is NOT on listing nodes — measured, not a bug (see README)"


@check("category listing: real values from a live capture")
def _():
    html = (FIXTURES / "category_p1.html").read_text(encoding="utf-8")
    lp = pp.parse_listing_page(html, requested_url="https://stockx.com/category/sneakers")
    assert lp.flow == "CATEGORY"
    assert len(lp.products) == 6
    assert lp.products[0].category == "sneakers"


@check("listing: Variant-typed edges (mixed with Product-typed) still get a real title/brand/sku (regression, live 2026-09-14)")
def _():
    # Real find: a live category page's getDiscoveryData mixed TWO node
    # shapes in the same edges list (confirmed live: 8 of 48 edges in one
    # real capture). A "Product" node carries title/brand/urlKey/media/
    # traits flat on the node. A "Variant" node — StockX's own term, e.g.
    # a specific size/colorway callout tile — carries the exact same
    # catalog fields one level down under node["product"], while
    # node["market"] stays at the same top-level spot either way. Before
    # the fix, parse_listing_node read those fields straight off the
    # node, so every Variant row came back with title/brand/sku/
    # product_url/image_url all null even though its price parsed fine —
    # exactly the "always-null column" shape CLAUDE.md warns about, and
    # it was ~1 in 6 rows on a real page, not a rare edge case. Trimmed,
    # scrubbed shape of the two real node types (values changed, brand
    # names are real StockX catalog data, not anyone's secret).
    product_node = {
        "__typename": "Product",
        "title": "Jordan 4 Retro Toro Bravo (2026)",
        "brand": "Jordan",
        "urlKey": "air-jordan-4-retro-toro-bravo-2026",
        "productCategory": "sneakers",
        "gender": "men",
        "media": {"thumbUrl": "https://images.stockx.com/a.jpg"},
        "traits": [{"name": "Release Date", "value": "2026-01-01", "visible": True}],
        "market": {
            "state": {"lowestAsk": {"amount": 150}, "highestBid": {"amount": 216}},
            "statistics": {"lastSale": {"amount": 147}, "annual": {"averagePrice": 140, "salesCount": 10}},
        },
    }
    variant_node = {
        "__typename": "Variant",
        "id": "cd011172-6979-4ff9-91b8-df69ff925251",
        "product": {
            "title": "Nike SB Dunk Low Bronx Girls Skate",
            "brand": "Nike",
            "urlKey": "nike-sb-dunk-low-bronx-girls-skate",
            "productCategory": "sneakers",
            "gender": "men",
            "media": {"thumbUrl": "https://images.stockx.com/b.jpg"},
            "traits": [{"name": "Release Date", "value": "2026-01-27", "visible": True}],
        },
        "market": {
            "state": {"lowestAsk": {"amount": 158}, "highestBid": {"amount": 92}},
            "statistics": {"lastSale": {"amount": 123}, "annual": {"averagePrice": 141, "salesCount": 148}},
        },
    }
    discovery_query = {
        "queryKey": ["getDiscoveryData", {"variables": {"currency": "EUR", "flow": "CATEGORY"}}],
        "state": {"data": {"browse": {"results": {
            "pageInfo": {"page": 1, "pageCount": 1, "total": 2},
            "edges": [{"node": product_node}, {"node": variant_node}],
        }}}},
    }
    next_data = {"props": {"pageProps": {"req": {"appContext": {"states": {"query": {"value": {
        "queries": [discovery_query]
    }}}}}}}}
    html = f'<!DOCTYPE html><html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script></body></html>'
    lp = pp.parse_listing_page(html, requested_url="https://stockx.com/category/sneakers")
    assert len(lp.products) == 2
    product_row, variant_row = lp.products

    assert product_row.title == "Jordan 4 Retro Toro Bravo (2026)"
    assert product_row.brand == "Jordan"
    assert product_row.sku == "air-jordan-4-retro-toro-bravo-2026"

    assert variant_row.title == "Nike SB Dunk Low Bronx Girls Skate", (
        "a Variant-typed edge must read title/brand/sku from node['product'], "
        "not come back null just because the node itself has no flat 'title'"
    )
    assert variant_row.brand == "Nike"
    assert variant_row.sku == "nike-sb-dunk-low-bronx-girls-skate"
    assert variant_row.product_url == "https://stockx.com/nike-sb-dunk-low-bronx-girls-skate"
    assert variant_row.image_url == "https://images.stockx.com/b.jpg"
    assert variant_row.release_date == "2026-01-27"
    # market data was already read correctly for Variant nodes even before
    # the fix — node["market"] sits at the same top-level spot either way —
    # so this must keep working exactly as before.
    assert variant_row.price == 158 and variant_row.lowest_ask == 158
    assert variant_row.highest_bid == 92
    assert variant_row.last_sale == 123


@check("product detail: real values from a live capture (JSON-LD + GetProduct traits)")
def _():
    html = (FIXTURES / "product_flight_club.html").read_text(encoding="utf-8")
    product = pp.parse_product_detail(html, requested_url="https://stockx.com/air-jordan-4-retro-og-flight-club")
    assert product is not None
    assert product.style_id == "IM4002-100"
    assert product.colorway == "Sail/Black/University Red"
    assert product.brand == "Jordan"
    assert product.price == 123 and product.price_source == "jsonld"
    assert product.retail_price_usd == 220.0
    assert product.release_date == "2026-01-17"
    assert product.gender == "men"
    # Never scrape the invisible catalog-ops traits (Requester/Uploaded By).
    assert "Requester" not in pp._ALLOWED_TRAIT_NAMES
    assert "Uploaded By" not in pp._ALLOWED_TRAIT_NAMES


@check("count_product_links matches real edge/tile counts")
def _():
    search_html = (FIXTURES / "search_p1.html").read_text(encoding="utf-8")
    assert pp.count_product_links(search_html) == 6


@check("ambient Cloudflare script tag alone does NOT read as blocked (regression)")
def _():
    # Real, measured trap: every captured page (including fully successful
    # ones) ships a cdn-cgi/challenge-platform "precursor" script. If that
    # string were a marker, EVERY normal page would flag as blocked.
    search_html = (FIXTURES / "search_p1.html").read_text(encoding="utf-8")
    assert "cdn-cgi/challenge-platform" not in " ".join(pp.BOT_CHALLENGE_MARKERS).lower()
    # this fixture is a normal, successful capture with real product data —
    # it must not be detected as a captcha/interstitial page.
    assert captcha_solver.detect_from_html(search_html, pp.BOT_CHALLENGE_MARKERS) is False


@check("StockX's own Next.js error boundary reads as blocked, not empty (regression, live 2026-09-13)")
def _():
    # A live run against stockx.com from a Russian residential IP got a 403
    # at the edge and Next.js rendered ITS OWN app-level error boundary
    # instead of a Cloudflare "Just a moment" interstitial — none of
    # BOT_CHALLENGE_MARKERS matched, so `blocked` stayed False and the run
    # reported exit 4 ("this category is empty") for what was actually a
    # hard block. Fixture below is a trimmed, scrubbed shape of that real
    # response's error-boundary root (the part that matters: the framework
    # id, not the page copy — see product_parser.APP_ERROR_MARKERS).
    app_error_html = (
        '<!DOCTYPE html><html id="__next_error__"><body>'
        '<h2>Uh-oh! Something went wrong.</h2><p style="...">ERROR abcd1234</p>'
        '</body></html>'
    )
    assert captcha_solver.detect_from_html(app_error_html, pp.APP_ERROR_MARKERS) is True
    # And the normal-page regression above must still hold for this marker
    # set too — a real successful capture must never trip it.
    search_html = (FIXTURES / "search_p1.html").read_text(encoding="utf-8")
    assert captcha_solver.detect_from_html(search_html, pp.APP_ERROR_MARKERS) is False


@check("captcha widget identification on synthetic markup (no real captcha was ever observed live)")
def _():
    turnstile_html = '<div class="cf-turnstile" data-sitekey="0x4AAAAAAA_test_only"></div>'
    signal = captcha_solver.identify_widget(turnstile_html)
    assert signal is not None and signal.captcha_type.value == "cloudflare_turnstile"
    assert signal.sitekey == "0x4AAAAAAA_test_only"

    v3_html = '<script src="https://www.google.com/recaptcha/api.js?render=6Lc_test_key"></script>'
    signal_v3 = captcha_solver.identify_widget(v3_html)
    assert signal_v3.captcha_type.value == "recaptcha_v3"

    v2_invisible_html = '<script src="https://www.google.com/recaptcha/api.js?render=explicit"></script><div class="g-recaptcha" data-sitekey="6Lc_v2_key"></div>'
    signal_v2 = captcha_solver.identify_widget(v2_invisible_html)
    assert signal_v2.captcha_type.value == "recaptcha_v2", "loader's render=explicit must win over any v3-looking wrapper"


@check("2Captcha's OWN Scraping Browser extension noise never counts as the page's captcha (live, 2026-09-14)")
def _():
    # Real find: a live 403 fetched over --cdp-endpoint turned out to be a
    # plain, static Cloudflare WAF page ("Sorry, you have been blocked",
    # no data-sitekey anywhere) — but the Scraping Browser API's managed
    # Chromium has 2Captcha's OWN captcha-solving extension pre-installed,
    # and page.content() captures ITS injected <script> tags too. Trimmed,
    # scrubbed shape of the real one (extension id kept — it's the
    # extension's own public id, not a secret):
    extension_noise = (
        '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
        'content/captcha/turnstile/interceptor.js"></script>'
        '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
        'content/captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-response">'
        '</script>'
    )
    # A genuinely clean page (real products, no challenge at all) must not
    # be flagged just because this extension's own code is always present
    # when fetched via --cdp-endpoint.
    clean_page = extension_noise + "<a href='/some-product'>p</a>" * 5
    assert captcha_solver.detect_from_html(clean_page) is False
    assert captcha_solver.identify_widget(clean_page) is None
    # And a REAL block (the "attention required" wording itself, not the
    # extension) must still be detected — stripping extension noise must
    # not blind detection to a real signal sitting right next to it.
    real_block_with_noise = extension_noise + "Attention Required! | Cloudflare"
    assert captcha_solver.detect_from_html(real_block_with_noise) is True
    assert captcha_solver.identify_widget(real_block_with_noise) is None  # no real widget on a static WAF page


@check("solve_when_blocked skips solving when products are already rendered")
def _():
    html = '<div class="cf-turnstile" data-sitekey="x"></div>' + "<a href='/some-product'>p</a>" * 5
    result = captcha_solver.solve_when_blocked(
        client=None, page_url="https://stockx.com/search", html=html,
        count_product_links=lambda h: 5,
    )
    assert result["action"] == "skipped_products_present"


# --------------------------------------------------------------------------- #
# output_writer contract
# --------------------------------------------------------------------------- #
def _mk_product(sku, price=100.0, **kw):
    base = dict(
        sku=sku, source="stockx.com", category="sneakers", title=f"Product {sku}", brand="Jordan",
        price=price, currency="EUR", price_source="embedded_json",
        product_url=f"https://stockx.com/{sku}", image_url=None, scraped_at="2026-09-10T00:00:00Z",
    )
    base.update(kw)
    return output_writer.Product(**base)


@check("merge_pages dedupes by sku, keeps PAGE order, last-write-wins per sku")
def _():
    page1 = [_mk_product("a", price=100), _mk_product("b", price=200)]
    page2 = [_mk_product("b", price=210), _mk_product("c", price=300)]  # b repeated with a new price
    merged = output_writer.merge_pages([page1, page2])
    assert [p.sku for p in merged] == ["a", "b", "c"]
    assert merged[1].price == 210, "later page's value should win for a repeated sku"


@check("a zero-product run writes NOTHING — not the data file, and not the sidecar either — unless --allow-empty")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        Path(out).write_text('[{"sku": "previous-good-run"}]', encoding="utf-8")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=1, pages_completed=0, failed_pages=[], blocked=False,
            remote_api_error=False, allow_empty=False, started_at=0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert json.loads(Path(out).read_text()) == [{"sku": "previous-good-run"}], \
            "an empty run must NEVER overwrite a previous good output"
        # Real bug, found 2026-09-14 by testing the sidecar too (the check
        # above only ever looked at the DATA file): finish_run's own
        # docstring says "NEVER writes a sidecar for a failed run", README
        # promises the same in the exit-code table, and CLAUDE.md §9 states
        # it as a family invariant — "a 'failed' sidecar beside good data
        # would contradict it" — but the code wrote one anyway, so a
        # consumer reading <out>.json.meta.json to decide whether to trust
        # <out>.json would see THIS run's "empty_not_written" status sitting
        # right next to a PREVIOUS run's perfectly good data.
        assert not Path(out + ".meta.json").exists(), (
            "an empty run must NOT write a sidecar — one sitting beside a "
            "previous good <out>.json contradicts that data instead of "
            "describing it"
        )


@check("a BLOCKED run with zero products also writes nothing — data OR sidecar (not just the generic empty case)")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        Path(out).write_text('[{"sku": "previous-good-run"}]', encoding="utf-8")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=1, pages_completed=0, failed_pages=[], blocked=True,
            remote_api_error=False, allow_empty=False, started_at=0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert json.loads(Path(out).read_text()) == [{"sku": "previous-good-run"}], \
            "regression: a blocked run must not overwrite good output either"
        assert not Path(out + ".meta.json").exists(), \
            "regression: a blocked run must not write a sidecar either"


@check("a remote-API-error run with zero products also writes nothing — data OR sidecar")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.json")
        Path(out).write_text('[{"sku": "previous-good-run"}]', encoding="utf-8")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="https://x",
            pages_requested=1, pages_completed=0, failed_pages=[], blocked=False,
            remote_api_error=True, allow_empty=False, started_at=0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR
        assert json.loads(Path(out).read_text()) == [{"sku": "previous-good-run"}]
        assert not Path(out + ".meta.json").exists(), \
            "a remote-API-error run must not write a sidecar either"


@check("empty CSV still carries its header")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = str(Path(d) / "out.csv")
        output_writer.write_csv([], out)
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [output_writer.PRODUCT_FIELD_NAMES]


@check("exit codes are distinct integers 0-6")
def _():
    codes = [
        output_writer.EXIT_OK, output_writer.EXIT_CRASH, output_writer.EXIT_BAD_USAGE,
        output_writer.EXIT_BLOCKED, output_writer.EXIT_ZERO_PRODUCTS,
        output_writer.EXIT_REMOTE_API_ERROR, output_writer.EXIT_PARTIAL,
    ]
    assert codes == sorted(set(codes)) == list(range(7))


# --------------------------------------------------------------------------- #
# proxy_pool
# --------------------------------------------------------------------------- #
@check("proxy formats parse; credentials never appear in the masked/server-only strings")
def _():
    p1 = proxy_pool.parse_proxy_line("http://alice:s3cr3t@1.2.3.4:8080")
    assert p1.host == "1.2.3.4" and p1.port == 8080 and p1.login == "alice" and p1.password == "s3cr3t"
    p2 = proxy_pool.parse_proxy_line("1.2.3.4:8080:bob:hunter2")
    assert p2.login == "bob" and p2.password == "hunter2"
    p3 = proxy_pool.parse_proxy_line("1.2.3.4:8080")
    assert p3.login is None

    assert "s3cr3t" not in p1.masked()
    assert "s3cr3t" not in p1.server_only()
    assert "hunter2" not in p2.server_only()
    assert "***" in p1.masked()


@check("proxy_pool: dead-error classification, block after N failures, success resets it")
def _():
    p = proxy_pool.parse_proxy_line("1.2.3.4:8080")
    pool = proxy_pool.ProxyPool([p], block_retries=2)
    assert proxy_pool.is_proxy_dead_error("net::ERR_PROXY_CONNECTION_FAILED at https://x") is True
    assert proxy_pool.is_proxy_dead_error("Timeout 30000ms exceeded") is False
    pool.report_failure(p, dead=True)
    assert pool.next() is not None, "one failure must not kill the only proxy in the pool"
    pool.report_failure(p, dead=True)
    assert pool.next() is None, "block_retries=2 consecutive dead-errors should mark it dead"


@check("worker_view rotates each worker to a different starting offset (no shared cursor)")
def _():
    proxies = [proxy_pool.parse_proxy_line(f"{i}.0.0.1:80") for i in range(3)]
    pool = proxy_pool.ProxyPool(proxies)
    v0 = pool.worker_view(0, 3).next().host
    v1 = pool.worker_view(1, 3).next().host
    v2 = pool.worker_view(2, 3).next().host
    assert len({v0, v1, v2}) == 3, "each worker should start on a different exit"


@check("redact_credentials scrubs URL userinfo AND key/token params, globally, from a real driver error shape (CLAUDE.md §8, live 2026-09-14)")
def _():
    # Reproduced live: a failed connect_over_cdp repeats the ws:// endpoint
    # — login:password included — FOUR times in Playwright's own "Call
    # log" text, not once. A masker that only handles the first occurrence
    # prints the password the other three times and looks like it worked.
    # Built by CONCATENATION rather than written out. No line of this file
    # then holds a complete `scheme://user:pass@host` literal, which keeps
    # `.github/ci_checks.py --secret-check` fully LIVE on this file instead
    # of exempting it. This is the file most likely to acquire a real
    # credential by paste while debugging, so it is the last one that should
    # be excluded from the scan -- which is what the inline grep in
    # tests.yml used to do.
    _user = "realLogin" + "123"
    _pw = "realSECRET" + "pass"
    _endpoint = "ws://" + _user + ":" + _pw + "@cb.2captcha.com:9222/"
    cdp_error = (
        "BrowserType.connect_over_cdp: WebSocket error: connect ECONNREFUSED 1.2.3.4:9222\n"
        "Call log:\n"
        f"  - <ws connecting> {_endpoint}\n"
        f"  - <ws error> {_endpoint} error connect ECONNREFUSED\n"
        f"  - <ws connect error> {_endpoint} connect ECONNREFUSED\n"
        f"  - <ws disconnected> {_endpoint} code=1006 reason=\n"
    )
    redacted = proxy_pool.redact_credentials(cdp_error)
    assert _user not in redacted and _pw not in redacted
    assert redacted.count("***:***@") == 4, "must redact EVERY occurrence, not just the first"

    # requests puts the full URL, query string included, in an HTTPError —
    # the fingerprint/random endpoint takes its key that way (CLAUDE.md §8
    # names it explicitly).
    fp_error = "404 Client Error: Not Found for url: https://api.2captcha.com/fingerprint/random?key=abcdef0123456789&format=chromium"
    redacted_fp = proxy_pool.redact_credentials(fp_error)
    assert "abcdef0123456789" not in redacted_fp
    assert "key=***" in redacted_fp


@check("a failed --cdp-endpoint connection never leaks its credential into the raised error (playwright, puppeteer)")
def _():
    import asyncio

    # Concatenated for the same reason as the fixture above.
    FAKE_ENDPOINT = ("ws://" + "fakeLogin987" + ":" + "fakeSecretXYZ"
                     + "@cb.2captcha.com:9222")

    class _FakeChromium:
        async def connect_over_cdp(self, endpoint):
            raise RuntimeError(f"connect failed to {endpoint} (Call log: <ws connecting> {endpoint})")

    class _FakePW:
        chromium = _FakeChromium()

    try:
        asyncio.run(playwright_scraper._connect_over_cdp(_FakePW(), FAKE_ENDPOINT))
        raised = None
    except Exception as exc:
        raised = str(exc)
    assert raised is not None, "the fake connect must have raised"
    assert "fakeSecretXYZ" not in raised and "fakeLogin987" not in raised, (
        f"credential leaked into playwright_scraper's raised error: {raised!r}"
    )

    async def _fake_pyppeteer_connect(*, browserWSEndpoint, defaultViewport):
        raise RuntimeError(f"connect failed to {browserWSEndpoint}")

    original = puppeteer_scraper.pyppeteer_connect
    puppeteer_scraper.pyppeteer_connect = _fake_pyppeteer_connect
    try:
        try:
            asyncio.run(puppeteer_scraper._launch(headless=True, proxy=None, cdp_endpoint=FAKE_ENDPOINT))
            raised2 = None
        except Exception as exc:
            raised2 = str(exc)
    finally:
        puppeteer_scraper.pyppeteer_connect = original
    assert raised2 is not None, "the fake connect must have raised"
    assert "fakeSecretXYZ" not in raised2 and "fakeLogin987" not in raised2, (
        f"credential leaked into puppeteer_scraper's raised error: {raised2!r}"
    )


@check("a failed fingerprint/random request never leaks the key into the raised error (CLAUDE.md §8)")
def _():
    import requests as _requests
    from scraper_api_client import TwoCaptchaClient, TwoCaptchaError

    client = TwoCaptchaClient(api_key="realTOPSECRETkey123")

    def _fake_get(url, params=None, timeout=None):
        full_url = url + "?" + "&".join(f"{k}={v}" for k, v in (params or {}).items())
        raise _requests.exceptions.ConnectionError(f"Failed to establish a new connection: {full_url}")

    original_get = _requests.get
    _requests.get = _fake_get
    try:
        try:
            client.random_fingerprint()
            raised = None
        except TwoCaptchaError as exc:
            raised = str(exc)
    finally:
        _requests.get = original_get
    assert raised is not None, "the fake request must have raised"
    assert "realTOPSECRETkey123" not in raised, f"API key leaked into raised error: {raised!r}"


# --------------------------------------------------------------------------- #
# scrape_listing concurrency: a worker that raises must not lose its
# siblings' pages (CLAUDE.md §10's own testing checklist names this case)
# --------------------------------------------------------------------------- #
@check("playwright: an unexpected parse exception on a WORKER page degrades to a failed page, not a crash that discards page 1 (live 2026-09-14 regression)")
def _():
    import asyncio
    import types

    fixture_html = (FIXTURES / "category_p1.html").read_text(encoding="utf-8")
    real_parse = pp.parse_listing_page

    def _patched(html, *, requested_url):
        if html == "CRASHTRIGGER":
            raise ValueError("simulated unexpected parse crash on a WORKER page only")
        return real_parse(html, requested_url=requested_url)

    class _Page1:
        async def goto(self, url, wait_until=None, timeout=None):
            return types.SimpleNamespace(status=200)

        async def wait_for_timeout(self, ms):
            pass

        async def content(self):
            return fixture_html

    class _Ctx1:
        async def new_page(self):
            return _Page1()

        async def close(self):
            pass

    class _WorkerPage:
        async def goto(self, url, wait_until=None, timeout=None):
            return types.SimpleNamespace(status=200)

        async def wait_for_timeout(self, ms):
            pass

        async def content(self):
            return "CRASHTRIGGER"

    class _WorkerCtx:
        async def new_page(self):
            return _WorkerPage()

        async def close(self):
            pass

    class _FakeBrowser:
        def __init__(self):
            self._first = True

        async def new_context(self, **kwargs):
            if self._first:
                self._first = False
                return _Ctx1()
            return _WorkerCtx()

    class _Args:
        pages = 3
        concurrency = 2
        delay = 0
        retries = 0
        retry_delay = 0
        proxy_rotate = False
        dump_html = False
        solve_captcha = "off"
        out = "unused.json"

    pp.parse_listing_page = _patched
    try:
        pages_products, failed_pages, blocked, remote_api_error, _, _ = asyncio.run(
            playwright_scraper.scrape_listing(
                args=_Args(), start_url="https://stockx.com/category/sneakers",
                browser=_FakeBrowser(), proxy_pool=None, client=None,
            )
        )
    finally:
        pp.parse_listing_page = real_parse

    assert len(pages_products) == 1, f"page 1's already-good data must survive a worker's parse crash: {pages_products!r}"
    assert sum(len(p) for p in pages_products) > 0, "page 1 must still have real products"
    assert 2 in failed_pages or 3 in failed_pages, f"the crashing page must be recorded as failed, not silently dropped: {failed_pages!r}"


@check("puppeteer: an unexpected parse exception on a WORKER page degrades to a failed page, not a crash (mirrors the playwright fix, CLAUDE.md §6)")
def _():
    import asyncio

    fixture_html = (FIXTURES / "category_p1.html").read_text(encoding="utf-8")
    real_parse = pp.parse_listing_page
    call_count = {"n": 0}

    def _patched(html, *, requested_url):
        if html == "CRASHTRIGGER":
            raise ValueError("simulated unexpected parse crash on a WORKER page only")
        return real_parse(html, requested_url=requested_url)

    class _FakePage:
        def __init__(self, html):
            self._html = html

        async def goto(self, url, opts=None):
            import types
            return types.SimpleNamespace(status=200)

        async def content(self):
            return self._html

    class _FakeBrowser:
        def __init__(self, html):
            self._html = html

        async def newPage(self):
            return _FakePage(self._html)

        async def close(self):
            pass

    async def _fake_launch(*, headless, proxy, cdp_endpoint):
        call_count["n"] += 1
        return _FakeBrowser(fixture_html) if call_count["n"] == 1 else _FakeBrowser("CRASHTRIGGER")

    class _Args:
        pages = 3
        concurrency = 2
        delay = 0
        retries = 0
        retry_delay = 0
        proxy_rotate = False
        dump_html = False
        solve_captcha = "off"
        out = "unused.json"
        cdp_endpoint = None
        headless = True

    pp.parse_listing_page = _patched
    original_launch = puppeteer_scraper._launch
    puppeteer_scraper._launch = _fake_launch
    try:
        pages_products, failed_pages, blocked, remote_api_error = asyncio.run(
            puppeteer_scraper.scrape_listing(
                args=_Args(), start_url="https://stockx.com/category/sneakers",
                proxy_pool=None, client=None,
            )
        )
    finally:
        pp.parse_listing_page = real_parse
        puppeteer_scraper._launch = original_launch

    assert len(pages_products) == 1, f"page 1's already-good data must survive a worker's parse crash: {pages_products!r}"
    assert sum(len(p) for p in pages_products) > 0, "page 1 must still have real products"
    assert 2 in failed_pages or 3 in failed_pages, f"the crashing page must be recorded as failed, not silently dropped: {failed_pages!r}"


@check("selenium: an unexpected parse exception on a WORKER page degrades to a failed page, not a crash (mirrors the playwright fix, CLAUDE.md §6)")
def _():
    fixture_html = (FIXTURES / "category_p1.html").read_text(encoding="utf-8")
    real_parse = pp.parse_listing_page
    call_count = {"n": 0}

    def _patched(html, *, requested_url):
        if html == "CRASHTRIGGER":
            raise ValueError("simulated unexpected parse crash on a WORKER page only")
        return real_parse(html, requested_url=requested_url)

    class _FakeDriver:
        def __init__(self, html):
            self._html = html

        def set_page_load_timeout(self, s):
            pass

        def get(self, url):
            pass

        @property
        def page_source(self):
            return self._html

        def execute_script(self, script):
            return 200

        def quit(self):
            pass

    def _fake_build_driver(*, headless, proxy, cdp_endpoint):
        call_count["n"] += 1
        return _FakeDriver(fixture_html) if call_count["n"] == 1 else _FakeDriver("CRASHTRIGGER")

    class _Args:
        pages = 3
        concurrency = 2
        delay = 0
        retries = 0
        retry_delay = 0
        dump_html = False
        solve_captcha = "off"
        out = "unused.json"
        headless = True

    pp.parse_listing_page = _patched
    original_build_driver = selenium_scraper._build_driver
    selenium_scraper._build_driver = _fake_build_driver
    try:
        pages_products, failed_pages, blocked, remote_api_error = selenium_scraper.scrape_listing(
            args=_Args(), start_url="https://stockx.com/category/sneakers",
            proxy_pool=None, client=None,
        )
    finally:
        pp.parse_listing_page = real_parse
        selenium_scraper._build_driver = original_build_driver

    assert len(pages_products) == 1, f"page 1's already-good data must survive a worker's parse crash: {pages_products!r}"
    assert sum(len(p) for p in pages_products) > 0, "page 1 must still have real products"
    assert 2 in failed_pages or 3 in failed_pages, f"the crashing page must be recorded as failed, not silently dropped: {failed_pages!r}"


@check("selenium: concurrent workers fetch the REAL page numbers, not the ThreadPoolExecutor loop index (regression, live 2026-09-14)")
def _():
    """`pool.map(lambda item: worker(*item), enumerate(remaining, start=0))`
    used to feed `worker(n, worker_index)` BACKWARDS — enumerate yields
    (index, page_number), so `n` (meant to be the real page number used in
    the fetched URL) received the loop index instead. With `remaining ==
    [2, 3]` this fetched page_url(start_url, 0) and page_url(start_url, 1)
    — page 0 doesn't exist and page 1 was already fetched — instead of the
    real pages 2 and 3. Confirmed live by recording every URL `_fetch` was
    actually called with."""
    fixture_html = (FIXTURES / "category_p1.html").read_text(encoding="utf-8")
    requested_urls = []

    class _FakeDriver:
        def __init__(self, html):
            self._html = html

        def set_page_load_timeout(self, s):
            pass

        def get(self, url):
            pass

        @property
        def page_source(self):
            return self._html

        def execute_script(self, script):
            return 200

        def quit(self):
            pass

    def _fake_build_driver(*, headless, proxy, cdp_endpoint):
        return _FakeDriver(fixture_html)

    real_fetch = selenium_scraper._fetch

    def _spying_fetch(driver, url, **kwargs):
        requested_urls.append(url)
        return real_fetch(driver, url, **kwargs)

    class _Args:
        pages = 3
        concurrency = 2
        delay = 0
        retries = 0
        retry_delay = 0
        dump_html = False
        solve_captcha = "off"
        out = "unused.json"
        headless = True

    original_build_driver = selenium_scraper._build_driver
    original_fetch = selenium_scraper._fetch
    selenium_scraper._build_driver = _fake_build_driver
    selenium_scraper._fetch = _spying_fetch
    try:
        selenium_scraper.scrape_listing(
            args=_Args(), start_url="https://stockx.com/category/sneakers",
            proxy_pool=None, client=None,
        )
    finally:
        selenium_scraper._build_driver = original_build_driver
        selenium_scraper._fetch = original_fetch

    assert any("page=2" in u for u in requested_urls), f"page 2 was never requested: {requested_urls!r}"
    assert any("page=3" in u for u in requested_urls), f"page 3 was never requested: {requested_urls!r}"
    assert not any("page=0" in u or "page=1" in u for u in requested_urls), (
        f"a wrong (loop-index) page number leaked into a fetched URL: {requested_urls!r}"
    )


# --------------------------------------------------------------------------- #
# diff_runs
# --------------------------------------------------------------------------- #
@check("diff_runs refuses to compare a non-complete run")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "old.json"
        new = Path(d) / "new.json"
        old.write_text("[]", encoding="utf-8")
        new.write_text("[]", encoding="utf-8")
        (Path(d) / "old.json.meta.json").write_text(json.dumps({"status": "partial"}), encoding="utf-8")
        (Path(d) / "new.json.meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        try:
            diff_runs.diff(str(old), str(new))
            raised = False
        except SystemExit:
            raised = True
        assert raised, "diff() must refuse a non-'complete' run rather than produce a misleading diff"


@check("diff_runs separates a real price change from a price_source change")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        old = Path(d) / "old.json"
        new = Path(d) / "new.json"
        old.write_text(json.dumps([
            {"sku": "a", "price": 100, "price_source": "embedded_json", "title": "A"},
            {"sku": "b", "price": 200, "price_source": "embedded_json", "title": "B"},
        ]), encoding="utf-8")
        new.write_text(json.dumps([
            {"sku": "a", "price": 110, "price_source": "embedded_json", "title": "A"},   # real change, same source
            {"sku": "b", "price": 205, "price_source": "jsonld", "title": "B"},          # price AND source both differ
        ]), encoding="utf-8")
        for f in (old, new):
            Path(f"{f}.meta.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        result = diff_runs.diff(str(old), str(new))
        assert [c["sku"] for c in result["changed"]] == ["a"]
        assert [c["sku"] for c in result["source_changed"]] == ["b"]


# --------------------------------------------------------------------------- #
# env_config
# --------------------------------------------------------------------------- #
@check("ENV_KEYS matches .env.example in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
    assert documented == set(env_config.ENV_KEYS), (
        f"drift between .env.example and ENV_KEYS: "
        f"in .env.example only={documented - set(env_config.ENV_KEYS)}, "
        f"in ENV_KEYS only={set(env_config.ENV_KEYS) - documented}"
    )


@check("env_config never maps a variable onto a flag with a non-empty default")
def _():
    # --out has a non-empty effective default (derived from --format) in
    # every engine — STOCKX_URL/PROXY/CDP_ENDPOINT/TWOCAPTCHA_KEY all map
    # onto flags whose argparse default is None, which is the rule.
    for mod in (playwright_scraper, selenium_scraper, puppeteer_scraper):
        parser = mod.build_arg_parser()
        defaults = {a.dest: a.default for a in parser._actions}
        for dest in env_config.ENV_KEYS.values():
            if dest in defaults:
                assert defaults[dest] in (None, ""), (
                    f"{mod.__name__}: --{dest} has a non-empty default "
                    f"({defaults[dest]!r}) — env_config.apply_env() would "
                    f"never be able to fill it, making the env var silently inert"
                )


@check("apply_env treats a pasted {...} example fragment as unset, not as a real credential (CLAUDE.md §17)")
def _():
    # The exact family bug: .env.example documents STOCKX_CDP_ENDPOINT the
    # way the vendor documents it, `ws://{login}-zone-...-pid-{profileId}:
    # {password}@cb.2captcha.com:9222`, and on OTHER repos in this family a
    # copied-verbatim placeholder check (an exact-string match on
    # "your_key_here" and friends) never matched that shape — pasting the
    # DOCUMENTED example past the `=` read as configured, and the run
    # authenticated to the real host as the literal string "{login}-zone-…"
    # and failed with a confusing 401 far from its actual cause. This
    # repo's own .env.example ships blank `KEY=` lines (checked above), so
    # the incident hasn't happened here — this pins the defensive fix.
    import argparse

    braced_value = "ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222"
    prev = os.environ.pop("STOCKX_CDP_ENDPOINT", None)
    os.environ["STOCKX_CDP_ENDPOINT"] = braced_value
    try:
        args = argparse.Namespace(cdp_endpoint=None)
        env_config.apply_env(args, dotenv_path=str(ROOT / "_no_such_file.env"))
        assert args.cdp_endpoint is None, (
            f"a {{...}} placeholder fragment must read as unset, not be applied: {args.cdp_endpoint!r}"
        )
    finally:
        del os.environ["STOCKX_CDP_ENDPOINT"]
        if prev is not None:
            os.environ["STOCKX_CDP_ENDPOINT"] = prev

    # A real, fully-filled value with no braces must still apply normally —
    # the generic {...} check must not over-match.
    prev2 = os.environ.pop("STOCKX_URL", None)
    os.environ["STOCKX_URL"] = "https://stockx.com/category/sneakers"
    try:
        args2 = argparse.Namespace(url=None)
        env_config.apply_env(args2, dotenv_path=str(ROOT / "_no_such_file.env"))
        assert args2.url == "https://stockx.com/category/sneakers"
    finally:
        del os.environ["STOCKX_URL"]
        if prev2 is not None:
            os.environ["STOCKX_URL"] = prev2


# --------------------------------------------------------------------------- #
# sample_output.{json,csv} — must be real, must match the Product schema
# --------------------------------------------------------------------------- #
@check("sample_output.json/csv columns match the Product dataclass, and are non-fabricated")
def _():
    sample_json = json.loads((ROOT / "sample_output.json").read_text(encoding="utf-8"))
    assert sample_json, "sample_output.json must not be empty"
    assert set(sample_json[0].keys()) == set(output_writer.PRODUCT_FIELD_NAMES)

    with (ROOT / "sample_output.csv").open(newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == output_writer.PRODUCT_FIELD_NAMES

    fabrication_markers = ("lorem ipsum", "example.com", "foo bar", "test product", "placeholder")
    blob = json.dumps(sample_json).lower()
    for marker in fabrication_markers:
        assert marker not in blob, f"sample_output.json looks fabricated (contains {marker!r})"


_ENGINE_NAMES = ("playwright_scraper", "puppeteer_scraper", "selenium_scraper")


@check("the credential scan has ONE implementation, and the workflow calls it (CLAUDE.md §17)")
def _():
    """Two implementations of one check is the shape §17 warns about: in
    three sibling repos the shipped script was invoked by nothing while
    tests.yml carried a narrower grep, and the two disagreed in the
    direction that matters -- the inline one matched only `ws://`/`wss://`,
    so an `http://user:pass@` credential would have sailed past CI.

    This repo had only the inline version, and it excluded smoke_test.py
    wholesale -- the file most likely to acquire a real credential by paste
    while debugging was the one file nobody scanned.
    """
    # This suite also runs INSIDE the Docker image, where `RUN python3
    # smoke_test.py` executes it at build time and `.github/` is
    # deliberately not COPYed -- the image carries no CI material, which is
    # itself one of the things CI asserts about it. So the subject of this
    # check (the repo's wiring) does not exist there.
    #
    # The escape is narrow ON PURPOSE: it triggers only when the whole
    # `.github` directory is absent, never when a file inside it is missing.
    # A check that quietly passes because its input disappeared is the
    # failure mode CLAUDE.md §21 describes -- a guard is only as good as the
    # fixture it runs against.
    github = ROOT / ".github"
    if not github.is_dir():
        return          # inside the image: nothing to assert about CI here

    script = github / "ci_checks.py"
    assert script.is_file(), ".github/ci_checks.py is missing"

    workflow = (github / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "ci_checks.py --secret-check" in workflow, (
        "tests.yml does not invoke the shipped credential scan; a script no "
        "workflow runs is dead code that looks load-bearing")
    # And it must not have grown a second implementation alongside it.
    assert "TWOCAPTCHA_KEY|" not in workflow, (
        "tests.yml has an inline credential grep again -- one implementation, "
        "invoked from both places")
    # The exclusion that made the inline version weak must not come back:
    # the fixtures are built by concatenation precisely so it is unnecessary.
    assert "smoke_test\\.py" not in workflow, (
        "tests.yml excludes smoke_test.py from a scan again")


@check("no shipped module holds a statement the control flow can never reach")
def _():
    """A statement after a return/raise/break/continue in the SAME block.

    Narrow on purpose: it claims nothing about reachability in general, only
    about a block whose control flow has already left. Measured across the
    eighteen repos of this family on 2026-09-16 it reported six problems and
    zero false positives -- six repos carrying the same fifteen lines, a
    function whose `def` line had been lost and whose body was absorbed into
    the end of the function above it. Byte-compiling cannot see this,
    because unreachable code is still valid code.
    """
    problems = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                body = getattr(node, field, None)
                if not isinstance(body, list):
                    continue
                for i, stmt in enumerate(body[:-1]):
                    if isinstance(stmt, (ast.Return, ast.Raise,
                                         ast.Continue, ast.Break)):
                        problems.append(f"{path.name}:{body[i + 1].lineno}")
                        break
    assert not problems, "unreachable statement(s): " + ", ".join(problems)


@check("every call into a shared module binds against the callee's real signature (CLAUDE.md §17)")
def _():
    """§17's check #1, and the one that earns its keep.

    A sibling repo shipped `classify(html, url=...)` in two of three engines
    against a callee whose second parameter is `status`, and BOTH crashed on
    their FIRST fetch -- invisible to import, `--help`, byte-compiling and
    every other check here, because none of those calls a function the way a
    live run does.

    Two failure modes, and the second is the one a weaker version of this
    swallows: a call whose arguments do not fit, and a call to a name the
    shared module DOES NOT DEFINE. A sibling resolved the callee with
    `getattr(..., None)` and skipped whatever came back not-callable, so
    three calls into an API that did not exist sat under a green run of its
    own binding check.

    Conservative by construction: `*args`/`**kwargs` calls are skipped
    rather than guessed at, and a name bound anywhere in the calling file
    shadows a same-named module -- an engine takes `proxy_pool` as a
    PARAMETER, and `proxy_pool.next()` on it is a method call.
    """
    shared = {"product_parser": pp, "output_writer": output_writer,
              "proxy_pool": proxy_pool, "captcha_solver": captcha_solver,
              "env_config": env_config, "diff_runs": diff_runs}
    problems, bound_count = [], 0
    for name in _ENGINE_NAMES + ("product_parser", "scraper_api_client"):
        path = ROOT / f"{name}.py"
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        local = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                local.add(node.id)
            elif isinstance(node, ast.arg):
                local.add(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.add(node.name)

        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in shared and not node.level:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (node.module, alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = None
            if isinstance(node.func, ast.Name) and node.func.id in direct:
                target = direct[node.func.id]
            elif (isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id in shared
                  and node.func.value.id not in local):
                target = (node.func.value.id, node.func.attr)
            if target is None:
                continue
            module_name, attr = target
            module = shared[module_name]
            if not hasattr(module, attr):
                problems.append(f"{path.name}:{node.lineno} calls "
                                f"{module_name}.{attr}, which does not exist "
                                f"— a live run reaches this as AttributeError")
                continue
            callee = getattr(module, attr)
            if not (inspect.isfunction(callee) or inspect.isclass(callee)):
                continue
            if (any(isinstance(a, ast.Starred) for a in node.args)
                    or any(k.arg is None for k in node.keywords)):
                continue
            try:
                signature = inspect.signature(callee)
            except (TypeError, ValueError):
                continue
            try:
                signature.bind(*([inspect.Parameter.empty] * len(node.args)),
                               **{k.arg: inspect.Parameter.empty for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{path.name}:{node.lineno} "
                                f"{module_name}.{attr}{signature} — {exc}")
            else:
                bound_count += 1
    assert not problems, "\n        ".join(problems)
    # A check that binds nothing passes for the wrong reason.
    assert bound_count > 10, f"only {bound_count} shared call(s) bound"


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run())
