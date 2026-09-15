#!/usr/bin/env python3
"""product_parser.py — this IS the stockx.com site knowledge. Everything
here was verified against real, live captures of stockx.com taken on
2026-09-10 (search, category and product-detail pages) — see README.md
"What was actually measured" for the raw numbers. Nothing in this file is
guessed from a reverse-engineering write-up.

Primary data source, and why it's not JSON-LD-first like the rest of the
family:

  - SEARCH and CATEGORY listing pages carry exactly ONE JSON-LD block
    (`BreadcrumbList` — navigation only, zero products). Like
    amazon-scraper, the real primary source is the site's OWN embedded
    payload: a Next.js `__NEXT_DATA__` script tag holds a dehydrated
    react-query cache, and one cached query — `queryKey[0] ==
    "getDiscoveryData"` — carries the full listing: `browse.results.edges[
    ].node` (title, brand, urlKey, category, live market data) and
    `browse.results.pageInfo` (page/pageCount/total). This is even richer
    than JSON-LD would have been: no DOM read is needed at all for listing
    scraping.
  - PRODUCT DETAIL pages DO carry real JSON-LD (`@type: "Product"` with an
    `AggregateOffer`/`Offer[]` — one Offer per shoe size, `description` is
    the US size). That's used as the primary price source there, cross-
    referenced with the `__NEXT_DATA__` `GetProduct` query for fields
    JSON-LD doesn't carry (`styleId`, `traits`, size chart, breadcrumbs).

Both sources return plain numeric amounts (`"amount": 123`, `"lowPrice":
123`) — none of the locale-grouping/NBSP parsing the family's Farfetch
lineage needed. `parse_price_text()` below exists purely as a last-resort
DOM fallback if a future markup change ever removes both structured
sources; it is NOT on the primary path and untested against a real DOM
price string, deliberately, since none was needed.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("product_parser")

BASE_URL = "https://stockx.com"
SOURCE = "stockx.com"

# Real taxonomy pulled from the site's own `getMenuCollections` query,
# 2026-09-10 — not guessed. Two different route shapes: verticals under
# /category/*, demographics under /browse/*.
CATEGORY_SLUGS = (
    "sneakers", "shoes", "apparel", "accessories",
    "collectibles", "trading-cards", "electronics", "mystery-boxes",
)
DEMOGRAPHIC_SLUGS = ("men", "women", "kids")

# First path segments seen in real navigation/route structure that are
# NOT a single-product page, even though they are one path segment deep
# like a product slug is. Best-effort — extend if a live run reports a
# suspiciously "product" row for one of these.
_NOT_A_CATEGORY = {
    "search", "category", "browse", "dp", "about", "news", "sell", "sell-cash",
    "buying", "selling", "help", "account", "cart", "checkout", "login",
    "signup", "portfolio", "orders", "returns", "authentication", "verified",
    "sitemap", "contact", "legal", "privacy", "terms", "careers", "press",
    "gift-card", "api",
}

# Real interstitial-only signals. Deliberately does NOT include the bare
# string "cdn-cgi/challenge-platform" — that Cloudflare Bot Management
# "precursor" script tag is present on ORDINARY, fully-rendered pages too
# (verified live, 2026-09-10: search/category/product pages all loaded
# real content while still shipping it). Using it as a marker would flag
# every normal page as blocked.
BOT_CHALLENGE_MARKERS = (
    'id="challenge-running"',
    "Enable JavaScript and cookies to continue",
    "cf_chl_opt",
    "Just a moment...</title>",
    "Attention Required! | Cloudflare",
)

# StockX's OWN Next.js application error boundary — a different failure
# class from the Cloudflare interstitial above, and just as real. Found
# 2026-09-13 on a live run: a request that gets a 403/5xx at the edge (a
# Russian residential exit, in the case that surfaced this) renders this
# app-level "Error" page instead of a Cloudflare challenge page. Neither
# BOT_CHALLENGE_MARKERS string matched it, so `blocked` stayed False and
# the run reported EXIT_ZERO_PRODUCTS ("this category is empty") for what
# was actually a hard block — a wrong answer in the family's own terms
# (§8: "missing content is one of three things, and they want three
# different answers" — this is the fourth thing that was missing:
# rejected-at-the-edge, not unpainted/lazy/wrong-session).
# `id="__next_error__"` is the framework's own error-boundary root id, not
# page copy, so it does not depend on wording that a redesign could change
# as easily as the visible "Something went wrong" text would.
APP_ERROR_MARKERS = (
    'id="__next_error__"',
)

# A THIRD distinct block-page family, neither the Cloudflare interstitial
# above nor the Next.js app-error boundary — StockX's own dedicated
# bot-guard route, reported by a live audit (2026-09-15): a request that
# gets rejected serves this instead of either of the other two, on a
# route containing "error/bot-site-guard" with its own visible copy
# ("Please login to continue. Please log in to verify you are not a bot.").
# Same rationale as APP_ERROR_MARKERS above: match a stable string, not
# HTTP status alone, since a driver path that doesn't surface the status
# code (some CDP/Selenium configurations) would otherwise miss this and
# misreport it as EXIT_ZERO_PRODUCTS ("this category is empty") for what
# is actually a hard block.
BOT_SITE_GUARD_MARKERS = (
    "error/bot-site-guard",
    "Please log in to verify you are not a bot",
)

MIN_CARD_MATCHES = 2  # per family invariant: must be >1, or a single
                       # unrelated link resolves the "rendered" wait early.

_PRICE_TEXT_RE = re.compile(
    r"(?P<currency>[A-Z]{3}|[$€£¥]|HK\$|A\$|NT\$)?\s*"
    r"(?P<number>[\d.,    ]+)"
)
_ISO_CURRENCY_ALLOWLIST = {
    "USD", "EUR", "GBP", "JPY", "CNY", "HKD", "AUD", "CAD", "CHF", "SGD",
    "KRW", "MXN", "NZD", "SEK", "NOK", "DKK", "PLN", "AED", "THB", "TWD",
}


# --------------------------------------------------------------------------- #
# URL helpers
# --------------------------------------------------------------------------- #
def category_page_url(slug: str, page: int = 1) -> str:
    url = f"{BASE_URL}/category/{slug}"
    return page_url(url, page) if page > 1 else url


def demographic_page_url(slug: str, page: int = 1) -> str:
    url = f"{BASE_URL}/browse/{slug}"
    return page_url(url, page) if page > 1 else url


def search_url(query: str, page: int = 1) -> str:
    url = f"{BASE_URL}/search?" + urlencode({"s": query})
    return page_url(url, page) if page > 1 else url


def page_url(url: str, page: int) -> str:
    """Reconstruct `url` with its `page` query param set to `page`,
    preserving every other query param (`s=`, `sort=`, ...) — verified
    live that requesting `?page=N` directly server-renders that page's own
    `getDiscoveryData` result (no cursor/token in play), so this is safe
    to use standalone rather than only after following page 1's own link."""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    if page > 1:
        q["page"] = [str(page)]
    else:
        q.pop("page", None)
    new_query = urlencode(q, doseq=True)
    return urlunparse(parts._replace(query=new_query))


def is_listing_url(url: str) -> bool:
    path = urlparse(url).path.strip("/")
    first = path.split("/")[0] if path else ""
    return first in ("search", "category", "browse")


def is_single_product_url(url: str) -> bool:
    path = urlparse(url).path.strip("/")
    if not path or "/" in path:
        return False
    return path not in _NOT_A_CATEGORY


def category_from_url(url: str) -> Optional[str]:
    parts = urlparse(url)
    path = parts.path.strip("/")
    if not path:
        return None
    segments = path.split("/")
    if segments[0] in ("category", "browse") and len(segments) > 1:
        return segments[1]
    if segments[0] == "search":
        q = parse_qs(parts.query)
        s = q.get("s", [None])[0]
        return f"search:{s}" if s else "search"
    return None


# --------------------------------------------------------------------------- #
# __NEXT_DATA__ / react-query extraction
# --------------------------------------------------------------------------- #
def extract_next_data(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag or not tag.string:
        return None
    try:
        return json.loads(tag.string)
    except (ValueError, TypeError):
        log.debug("__NEXT_DATA__ present but not parseable as JSON")
        return None


def _known_path_queries(next_data: dict) -> Optional[List[dict]]:
    try:
        return (
            next_data["props"]["pageProps"]["req"]["appContext"]
            ["states"]["query"]["value"]["queries"]
        )
    except (KeyError, TypeError):
        return None


def _walk_for_queries(node: Any, depth: int = 0) -> Optional[List[dict]]:
    """Fallback if the known path ever shifts: find any list whose items
    each look like a dehydrated react-query entry (`queryKey` + `state`)."""
    if depth > 10:
        return None
    if isinstance(node, list):
        if node and all(isinstance(n, dict) and "queryKey" in n and "state" in n for n in node):
            return node
        for item in node:
            found = _walk_for_queries(item, depth + 1)
            if found:
                return found
    elif isinstance(node, dict):
        for value in node.values():
            found = _walk_for_queries(value, depth + 1)
            if found:
                return found
    return None


def find_query(next_data: dict, query_name: str) -> Optional[dict]:
    """Returns the matching dehydrated query entry (with `.queryKey` and
    `.state.data`), or None if absent/query failed."""
    queries = _known_path_queries(next_data)
    if queries is None:
        queries = _walk_for_queries(next_data)
    if not queries:
        return None
    for q in queries:
        key = q.get("queryKey")
        if isinstance(key, list) and key and key[0] == query_name:
            if q.get("state", {}).get("data") is not None:
                return q
    return None


# --------------------------------------------------------------------------- #
# Listing pages (search + category + browse) — getDiscoveryData
# --------------------------------------------------------------------------- #
@dataclass
class ListingPage:
    products: List[Product]
    page: Optional[int]
    page_count: Optional[int]
    total: Optional[int]
    currency: Optional[str]
    flow: Optional[str]


def _num(node: Optional[dict], *path: str) -> Optional[float]:
    cur: Any = node
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    if isinstance(cur, (int, float)):
        return cur
    return None


def parse_listing_node(node: dict, *, currency: Optional[str], category_label: Optional[str]) -> Product:
    market = node.get("market") or {}
    state = market.get("state") or {}
    stats = market.get("statistics") or {}

    # getDiscoveryData's edges mix two node shapes (confirmed live,
    # 2026-09-14, in a single real category page: 8 of 48 edges). A
    # "Product" node (__typename == "Product") carries title/brand/
    # urlKey/media/traits flat on the node itself. A "Variant" node
    # (__typename == "Variant" — a specific size/colorway tile, e.g. a
    # StockX-picked "also available" row) carries the exact same catalog
    # fields one level down, under node["product"], while node["market"]
    # stays at the same top-level spot either way. Reading node.get(...)
    # directly for the catalog fields left every Variant row with a null
    # title/brand/sku/product_url/image_url even though its price/
    # highest_bid/last_sale parsed fine — this picks the nested dict up
    # when present instead of silently leaving those fields null.
    catalog = node.get("product") if isinstance(node.get("product"), dict) else node

    media = catalog.get("media") or {}
    url_key = catalog.get("urlKey")

    lowest_ask = _num(state, "lowestAsk", "amount")
    return Product(
        sku=url_key,
        source=SOURCE,
        category=category_label or catalog.get("productCategory"),
        title=catalog.get("title") or catalog.get("name"),
        brand=catalog.get("brand"),
        price=lowest_ask,
        currency=currency,
        price_source="embedded_json" if lowest_ask is not None else None,
        product_url=f"{BASE_URL}/{url_key}" if url_key else None,
        image_url=media.get("thumbUrl") or media.get("smallImageUrl"),
        scraped_at=_now_iso(),
        style_id=None,  # not carried on listing nodes — see README measurement
        colorway=None,
        lowest_ask=lowest_ask,
        highest_bid=_num(state, "highestBid", "amount"),
        last_sale=_num(stats, "lastSale", "amount"),
        retail_price_usd=None,
        release_date=_trait_value(catalog.get("traits"), "Release Date"),
        annual_avg_price=_num(stats, "annual", "averagePrice"),
        annual_sales_count=(stats.get("annual") or {}).get("salesCount")
            if isinstance(stats.get("annual"), dict) else None,
        gender=catalog.get("gender"),
    )


def parse_listing_page(html: str, *, requested_url: str) -> ListingPage:
    next_data = extract_next_data(html)
    if next_data is None:
        return ListingPage([], None, None, None, None, None)

    query = find_query(next_data, "getDiscoveryData")
    if query is None:
        return ListingPage([], None, None, None, None, None)

    variables = {}
    key = query.get("queryKey")
    if isinstance(key, list) and len(key) > 1 and isinstance(key[1], dict):
        variables = key[1].get("variables") or {}
    currency = variables.get("currency")
    flow = variables.get("flow")

    browse = (query.get("state", {}).get("data") or {}).get("browse") or {}
    results = browse.get("results") or {}
    edges = results.get("edges") or []
    page_info = results.get("pageInfo") or {}

    category_label = category_from_url(requested_url)
    products = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else None
        if not isinstance(node, dict):
            continue
        products.append(parse_listing_node(node, currency=currency, category_label=category_label))

    return ListingPage(
        products=products,
        page=page_info.get("page"),
        page_count=page_info.get("pageCount"),
        total=page_info.get("total"),
        currency=currency,
        flow=flow,
    )


def count_product_links(html: str) -> int:
    """Used by captcha_solver.solve_when_blocked: cheap, no readiness wait."""
    next_data = extract_next_data(html)
    if next_data is not None:
        query = find_query(next_data, "getDiscoveryData")
        if query is not None:
            results = (query.get("state", {}).get("data") or {}).get("browse", {}).get("results") or {}
            edges = results.get("edges")
            if isinstance(edges, list):
                return len(edges)
    soup = BeautifulSoup(html, "html.parser")
    tiles = soup.select('[data-testid="ProductTile"], [data-testid="productTile"]')
    if tiles:
        return len(tiles)
    # last-resort URL-pattern count
    count = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = href.split("?")[0].strip("/")
        if path and "/" not in path and path not in _NOT_A_CATEGORY:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Product detail pages — JSON-LD (primary) + GetProduct (secondary)
# --------------------------------------------------------------------------- #
def extract_json_ld_product(html: str) -> Optional[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not tag.string:
            continue
        try:
            data = json.loads(tag.string)
        except (ValueError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if isinstance(candidate, dict) and candidate.get("@type") == "Product":
                return candidate
    return None


def _trait_value(traits: Any, name: str) -> Optional[str]:
    if not isinstance(traits, list):
        return None
    for t in traits:
        if isinstance(t, dict) and t.get("name") == name and t.get("visible", True):
            return t.get("value")
    return None


# Only these VISIBLE trait names are ever read. GetProduct.traits also
# carries internal-only entries (`visible: false`) like "Requester" /
# "Uploaded By" — StockX catalog-ops metadata, not customer data, but not
# meant to be user-facing either, so it is never read here regardless of
# the `visible` flag on it.
_ALLOWED_TRAIT_NAMES = ("Style", "Colorway", "Retail Price", "Release Date")


def parse_product_detail(html: str, *, requested_url: str) -> Optional[Product]:
    jsonld = extract_json_ld_product(html)
    next_data = extract_next_data(html)
    gp_product: Optional[dict] = None
    if next_data is not None:
        q = find_query(next_data, "GetProduct")
        if q is not None:
            gp_product = (q.get("state", {}).get("data") or {}).get("product")

    if jsonld is None and gp_product is None:
        return None

    # --- price: JSON-LD AggregateOffer is the structured, schema-legal
    # source. Defend against the legal-but-awkward shapes noted in the
    # family CLAUDE.md even though none were observed live on stockx.com,
    # since a different vertical (apparel/collectibles/mystery boxes) may
    # render a different Offer shape than the sneaker page this was
    # verified against.
    offers = jsonld.get("offers") if jsonld else None
    if isinstance(offers, list):
        offers = offers[0] if offers and isinstance(offers[0], dict) else None
    if not isinstance(offers, dict):
        offers = {}
    low_price = offers.get("lowPrice")
    currency = offers.get("priceCurrency")
    price = low_price if isinstance(low_price, (int, float)) else None

    url_key = (gp_product or {}).get("urlKey") or urlparse(requested_url).path.strip("/")
    title = (jsonld or {}).get("name") or (gp_product or {}).get("title")
    brand = None
    if jsonld and isinstance(jsonld.get("brand"), dict):
        brand = jsonld["brand"].get("name")
    brand = brand or (gp_product or {}).get("brand")

    image = (jsonld or {}).get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get("url") or image.get("contentUrl")

    traits = (gp_product or {}).get("traits")
    style_id = (gp_product or {}).get("styleId") or _trait_value(traits, "Style") or (jsonld or {}).get("sku")
    colorway = _trait_value(traits, "Colorway") or (jsonld or {}).get("color")
    release_date = _trait_value(traits, "Release Date") or (jsonld or {}).get("releaseDate")

    # Retail Price trait is USD-denominated regardless of the page's own
    # display currency (measured live: EUR-priced page still showed a
    # $-prefixed retail price sourced from this trait) — a real site
    # quirk, documented rather than silently normalised away.
    retail_raw = _trait_value(traits, "Retail Price")
    retail_price_usd = None
    if retail_raw is not None:
        try:
            retail_price_usd = float(str(retail_raw).replace(",", ""))
        except ValueError:
            retail_price_usd = None

    category_label = category_from_url(requested_url) or (
        (gp_product or {}).get("productCategory")
    )

    return Product(
        sku=url_key,
        source=SOURCE,
        category=category_label,
        title=title,
        brand=brand,
        price=price,
        currency=currency,
        price_source="jsonld" if price is not None else None,
        product_url=f"{BASE_URL}/{url_key}" if url_key else requested_url,
        image_url=image,
        scraped_at=_now_iso(),
        style_id=style_id,
        colorway=colorway.strip() if isinstance(colorway, str) else colorway,
        lowest_ask=price,
        highest_bid=None,   # not present on the SSR payload — see README
        last_sale=None,     # (both render client-side after mount)
        retail_price_usd=retail_price_usd,
        release_date=release_date,
        annual_avg_price=None,
        annual_sales_count=None,
        gender=(gp_product or {}).get("gender"),
    )


# --------------------------------------------------------------------------- #
# DOM price-text fallback — NOT on the primary path (see module docstring)
# --------------------------------------------------------------------------- #
def parse_price_text(text: str) -> Optional[float]:
    text = text.replace(" ", " ").replace(" ", " ").replace(" ", " ").strip()
    m = _PRICE_TEXT_RE.search(text)
    if not m:
        return None
    number = m.group("number").strip()
    if not number:
        return None
    has_dot, has_comma = "." in number, "," in number
    if has_dot and has_comma:
        decimal_sep = "." if number.rfind(".") > number.rfind(",") else ","
        thousands_sep = "," if decimal_sep == "." else "."
        number = number.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma and not has_dot:
        tail = number.split(",")[-1]
        number = number.replace(",", "") if len(tail) == 3 else number.replace(",", ".")
    elif " " in number:
        groups = number.split(" ")
        if all(len(g) == 3 for g in groups[1:]):
            number = number.replace(" ", "")
        else:
            return None
    try:
        return float(number)
    except ValueError:
        return None


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
