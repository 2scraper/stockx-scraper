#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
the output CONTRACT every engine shares (exit codes, sidecar fields, the
"never overwrite good output with an empty run" guard) — diverging from it
needs a written reason, not a per-site tweak.
"""
from __future__ import annotations

import contextlib
import csv
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, List, Optional, Sequence

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
# 2026-09-21. 5 means "the requested content was never obtained" — a remote
# API error is one way for that to happen; a dead proxy, a load timeout, a
# 404 for the requested address and a served page the parser could not read
# are others. A caller driving several of these scrapers should not need a
# per-repo table to learn that; the sidecar's `status`/`stop_reason` say
# which one it was.
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

# Every status finish_run() can report, with its exit code. STATUS_BY_EXIT
# above keeps one name per code for older callers; this is the full list,
# because exit 5 has several distinct causes and the sidecar names which.
EXIT_BY_STATUS = {
    "complete": EXIT_OK,
    "blocked": EXIT_BLOCKED,
    "empty": EXIT_ZERO_PRODUCTS,
    "remote_api_error": EXIT_FETCH_FAILED,
    "fetch_failed": EXIT_FETCH_FAILED,
    "not_found": EXIT_FETCH_FAILED,
    "parser_found_nothing": EXIT_FETCH_FAILED,
    "partial": EXIT_PARTIAL,
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


@dataclass
class RunFlags:
    """What a scrape learned besides its rows, handed from an engine to
    finish_run(). One object instead of loose booleans so the three engines
    cannot each grow their own subset."""
    blocked: bool = False
    not_found: bool = False
    parser_failed: bool = False
    remote_api_error: bool = False
    # Why a run that finished normally stopped: "site_last_page",
    # "no_new_rows", "requested_pages_done" or "single_product".
    stop_reason: Optional[str] = None


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
@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename it over `path`.

    `open(path, "w")` truncates before a byte is written, so a crash, a kill
    or a full disk halfway through `json.dump` left a SHORTER file where a
    complete one was — the previous good run destroyed by the attempt to
    replace it (reproduced 2026-10-14: a 497-byte good file became 2 bytes
    of invalid JSON).

    * The temp file lives in the target's own directory: `os.replace` is
      atomic only within one filesystem.
    * `fsync` before the rename makes the bytes durable, not just visible.
    * `newline` is passed through because the csv module needs `newline=""`.
    * Mode: an existing target keeps its own mode (someone may have
      tightened it on purpose); a new one gets `0o666 & ~umask`, exactly
      what `open()` gave it before. `NamedTemporaryFile` creates at 0600
      and `os.replace` keeps that, so without the chmod every output would
      silently become owner-only.
    * On any failure, including KeyboardInterrupt, the temp file is removed
      and the target is left untouched.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


# A spreadsheet reads a cell starting with one of these as a FORMULA, and
# every string in these rows (title, brand, colorway) was written by whoever
# listed the product. Tab/CR/LF are included because some readers strip a
# leading one and then evaluate what follows.
CSV_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r", "\n")
# Excel, Sheets and LibreOffice read a leading apostrophe as "text" and do
# not display it; a `csv`-module consumer does see it, which is why the
# count goes into the sidecar as `csv_cells_escaped`.
CSV_FORMULA_ESCAPE = "'"


def _csv_escape(value: Any):
    """(value, was_escaped). Strings only: a negative `price` stays a number,
    so sums over the column still work. CSV only: the JSON output keeps the
    site's text exactly as served."""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_LEADS):
        return CSV_FORMULA_ESCAPE + value, True
    return value, False


def _csv_value(value: Any) -> Any:
    # No Product field is a list or dict today; serialise one the same way
    # JSON would rather than writing Python's repr(), and do it BEFORE the
    # formula check so a serialised cell is checked too.
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def write_json(products: Sequence[Product], out_path: str) -> None:
    with _atomic(out_path) as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)


def write_csv(products: Sequence[Product], out_path: str) -> int:
    """Returns how many cells were formula-escaped."""
    escaped = 0
    with _atomic(out_path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            row = {}
            for key, value in asdict(p).items():
                row[key], was_escaped = _csv_escape(_csv_value(value))
                escaped += was_escaped
            writer.writerow(row)
    return escaped


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> Optional[int]:
    """Returns the CSV escape count for a CSV write, None for JSON."""
    if fmt == "json":
        write_json(products, out_path)
        return None
    if fmt == "csv":
        return write_csv(products, out_path)
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
    with _atomic(meta_path_for(out_path)) as f:
        f.write(json.dumps(meta, ensure_ascii=False, indent=2))


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
    not_found: bool = False,
    parser_failed: bool = False,
    stop_reason: Optional[str] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty.

    `not_found`: the requested address answered 404/410 (no block page).
    `parser_failed`: a page was served, but the parser could not read rows
    the page itself says exist (or its data block was missing). Neither is
    an empty catalogue, so neither may report exit 4.
    `stop_reason`: why a COMPLETE run stopped paginating (e.g.
    `site_last_page`); for every other status the cause is recorded instead.
    """
    failed_pages = sorted(set(failed_pages or []))
    zero_products = len(products) == 0
    # Pages were attempted and NONE completed: the content was never
    # obtained, which is a different fact from "we read the listing and it
    # held nothing" — exit 4 is a claim about the CATALOGUE and such a run
    # has no standing to make it.
    never_obtained = bool(failed_pages) and pages_completed == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or failed
    # run into "complete", and rows from SOME pages must not either. A run
    # audit (2026-09-15) found exactly that: with products present, or with
    # --allow-empty set, `blocked`/`remote_api_error` were ignored and the
    # run reported "complete", exit 0.
    if remote_api_error:
        status, cause = "remote_api_error", "remote_api_error"
    elif blocked:
        status, cause = "blocked", "blocked"
    elif not_found and zero_products:
        status, cause = "not_found", "not_found"
    elif parser_failed and zero_products:
        status, cause = "parser_found_nothing", "parser_found_nothing"
    elif never_obtained:
        status, cause = "fetch_failed", "fetch_failed"
    elif zero_products:
        status, cause = "empty", "no_results"
    elif failed_pages or parser_failed or not_found:
        # Rows were obtained, but part of the run failed: never "complete".
        status = "partial"
        cause = ("parser_found_nothing" if parser_failed
                 else "not_found" if not_found else "page_failed")
    else:
        status, cause = "complete", (stop_reason or "complete")
    exit_code = EXIT_BY_STATUS[status]

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above) writes NEITHER file NOR sidecar
    # unless the caller explicitly opted in with --allow-empty. A failure
    # sidecar beside a PREVIOUS good <out>.json would contradict that good
    # data rather than describe it; the engine's own log carries the
    # diagnostic detail of a failed run.
    if zero_products and not allow_empty:
        return exit_code

    escaped = write_output(products, out_path, fmt)
    extra = {}
    if escaped is not None:
        extra["csv_cells_escaped"] = escaped
    # The caller's own keys win over ours, never the other way round.
    extra.update(extra_meta or {})
    write_meta(
        out_path, status=status, stop_reason=cause, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra,
    )
    return exit_code
