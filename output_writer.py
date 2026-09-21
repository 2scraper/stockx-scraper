#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT, described in the family
CLAUDE.md — diverging from it needs a written reason, not a per-site tweak.
"""
from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper. A caller (CI, a cron job, another program) must be able
# to tell these apart without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
# The same code, under the name the rest of this family uses for it as of
# 2026-09-21. 5 means "the content was never obtained" — a remote API error
# is one way for that to happen and a dead proxy or a load timeout is
# another, and a caller driving several of these scrapers should not need a
# per-repo table to learn that. See CLAUDE.md §25.
EXIT_FETCH_FAILED = EXIT_REMOTE_API_ERROR
EXIT_PARTIAL = 6

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first (same order in JSON and CSV across every
    2scraper repo); stockx.com-specific fields at the end.

    `price` / `currency` / `price_source` are the columns diff_runs.py and
    every cross-site tool key off. `price` is StockX's *lowest ask* — the
    number a buyer could act on right now, and the closest StockX concept
    to "price" on a listing tile. `highest_bid` / `last_sale` /
    `annual_avg_price` are StockX-specific market-depth fields that most
    family sites don't have, so they live after the common block, not
    instead of `price`.
    """

    # --- family-common ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- stockx.com-specific ---
    style_id: Optional[str] = None
    colorway: Optional[str] = None
    lowest_ask: Optional[float] = None
    highest_bid: Optional[float] = None
    last_sale: Optional[float] = None
    retail_price_usd: Optional[float] = None
    release_date: Optional[str] = None
    annual_avg_price: Optional[float] = None
    annual_sales_count: Optional[int] = None
    gender: Optional[str] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    pagination loop should track to decide "did this page add anything
    NEW" — never just "is this page non-empty" (a page repeating an
    already-seen listing, e.g. past the real last page, isn't empty but
    isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_json(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            writer.writerow(asdict(p))


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> None:
    if fmt == "json":
        write_json(products, out_path)
    elif fmt == "csv":
        write_csv(products, out_path)
    else:
        raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    Path(meta_path_for(out_path)).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty."""
    failed_pages = failed_pages or []
    partial = bool(failed_pages) and pages_completed > 0
    zero_products = len(products) == 0
    # Pages were attempted and NONE completed: the content was never
    # obtained, which is a different fact from "we read the listing and it
    # held nothing". `partial` deliberately excludes this case (it requires
    # pages_completed > 0), so without this it fell through to
    # EXIT_ZERO_PRODUCTS and told a pipeline the catalogue was empty on a
    # run that never reached the site.
    never_obtained = bool(failed_pages) and pages_completed == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME pages did return products while the run was, in fact,
    # blocked partway through. A run audit (2026-09-15) found exactly
    # this: with products present, or with --allow-empty set, `blocked`/
    # `remote_api_error` were silently ignored and the run reported
    # "complete", exit 0 — a monitoring/cron consumer would trust a run
    # that never should have been trusted.
    if remote_api_error:
        status, exit_code = "remote_api_error", EXIT_FETCH_FAILED
    elif blocked:
        status, exit_code = "blocked", EXIT_BLOCKED
    elif never_obtained:
        # Ahead of zero_products on purpose: exit 4 is a claim about the
        # CATALOGUE and this run has no standing to make it.
        status, exit_code = "fetch_failed", EXIT_FETCH_FAILED
    elif zero_products:
        status, exit_code = "empty", EXIT_ZERO_PRODUCTS
    elif partial:
        status, exit_code = "partial", EXIT_PARTIAL
    else:
        status, exit_code = "complete", EXIT_OK

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — this branch's whole point is that a
    # PREVIOUS good <out>.json is left in place untouched, and a stale
    # failure sidecar sitting right beside it would contradict that good
    # data rather than describe it (this function's own docstring above,
    # README's exit-code table, and CLAUDE.md §9 all promise this). The
    # engine's own logs carry the diagnostic detail — that's what a failed
    # run's output is FOR, not this file.
    if zero_products and not allow_empty:
        return exit_code

    write_output(products, out_path, fmt)
    write_meta(
        out_path, status=status, stop_reason=status, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra_meta,
    )
    return exit_code
