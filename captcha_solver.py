#!/usr/bin/env python3
"""captcha_solver.py — detection + solving policy. Comments may name a site
for context; no site-specific selector or URL lives here. Per-site markers
are passed in by the caller (see BOT_CHALLENGE_MARKERS in product_parser.py)
rather than hardcoded, so this module stays reusable across the family.

Policy, matching the family's hard-won rule that "detected != blocking":

  - Detection stays broad — run it on every page, unconditionally.
  - A captcha WIDGET present on a page whose products are already rendered
    guards nothing and is not solved: `--solve-captcha when-blocked` (the
    default) only pays for a solve when `count_product_links(html) == 0`.
  - A missing key or a solver-side error (including a network error
    talking to the solver) is a WARNING, never a crash — the run continues,
    and only reports EXIT_BLOCKED if the page really was gated (see
    output_writer.EXIT_BLOCKED).
  - A paid solve is bought ONLY when the caller can use the token
    (`can_use_token=True`) and the page's `SolveBudget` still has an
    allowance — checked BEFORE the purchase. None of this repository's
    engines injects a token into a locally launched browser today (that
    step is widget-specific and no real StockX challenge has been seen to
    verify one against), so every engine passes `can_use_token=False` and
    no solve is bought: a token nobody injects cannot change the outcome,
    it only costs money. Over `--cdp-endpoint` the Scraping Browser API
    solves inside its own session instead.

No JavaScript crosses this module's boundary: it hands back a plain
(captcha_type, sitekey, token) triple. An engine that implements injection
does it in its own driver's dialect.
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Sequence

from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

# Generic signals any family site might show. A per-site BOT_CHALLENGE_MARKERS
# list (product_parser.py) is unioned with this, never a replacement for it.
GENERIC_BOT_CHALLENGE_MARKERS: Sequence[str] = (
    "cf-turnstile", "challenges.cloudflare.com", "cdn-cgi/challenge-platform",
    "g-recaptcha", "recaptcha/api.js", "grecaptcha",
    "h-captcha", "hcaptcha.com/1/api.js",
    "px-captcha", "perimeterx",
    "datadome",
    "attention required", "just a moment", "verify you are human",
)


class CaptchaType(str, Enum):
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    RECAPTCHA_V2 = "recaptcha_v2"
    RECAPTCHA_V3 = "recaptcha_v3"
    HCAPTCHA = "hcaptcha"


_SITEKEY_PATTERNS = {
    CaptchaType.CLOUDFLARE_TURNSTILE: re.compile(
        r'class="[^"]*cf-turnstile[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.RECAPTCHA_V2: re.compile(
        r'class="[^"]*g-recaptcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
    CaptchaType.HCAPTCHA: re.compile(
        r'class="[^"]*h-captcha[^"]*"[^>]*data-sitekey="([^"]+)"'
    ),
}
# v3 ships no visible widget — the sitekey rides on the loader's `render=`
# query param instead of a data-sitekey attribute.
_RECAPTCHA_V3_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=([\w-]+)")
_RECAPTCHA_EXPLICIT_LOADER_RE = re.compile(r"recaptcha/api\.js\?render=explicit")


# --------------------------------------------------------------------------- #
# CLI flags. `--captcha-api`, `--min-score` and `--solve-captcha always` only
# matter to a solve whose token is put into the page. No engine here injects
# a solved token, so those flags could only change nothing; they stay
# parseable for one release so an existing invocation gets a clear exit 2
# (argparse's usage-error exit) instead of a silent no-op.
# --------------------------------------------------------------------------- #
NOT_IMPLEMENTED_REASON = (
    "this repository does not implement captcha-token injection, so it would "
    "change nothing; remove it from the command line"
)
SOLVE_CAPTCHA_POLICIES = ("off", "when-blocked")


def _refuse_flag(flag: str):
    def _type(value: str):
        raise argparse.ArgumentTypeError(f"not implemented: {NOT_IMPLEMENTED_REASON}")
    _type.__name__ = flag.lstrip("-").replace("-", "_")
    return _type


# The engines make no 2Captcha API call at all (no solve is bought, see
# above, and no Fingerprint API flag is wired up), so a key typed here could
# only change nothing. Its destination is deliberately not `twocaptcha_key`:
# env_config fills only destinations the parser defines, so TWOCAPTCHA_KEY
# from .env or the environment is never loaded by an engine either.
TWOCAPTCHA_KEY_REASON = (
    "the engines in this repository make no 2Captcha API call and buy no "
    "captcha solves, so a key would change nothing; remove it from the "
    "command line"
)


def _refuse_twocaptcha_key(value: str):
    # Never echo the value: it may be a real key.
    raise argparse.ArgumentTypeError(f"not used: {TWOCAPTCHA_KEY_REASON}")


def _solve_captcha_policy(value: str) -> str:
    if value == "always":
        raise argparse.ArgumentTypeError(
            f"'always' is not implemented: {NOT_IMPLEMENTED_REASON} "
            f"(use 'when-blocked', the default, or 'off')")
    return value


def add_captcha_arguments(p: argparse.ArgumentParser) -> None:
    """The captcha flags, identical in every engine."""
    p.add_argument(
        "--solve-captcha", type=_solve_captcha_policy, choices=SOLVE_CAPTCHA_POLICIES,
        default="when-blocked",
        help="when-blocked (default): detect a captcha and report a page it gates as blocked "
             "; over --cdp-endpoint, the Playwright and pyppeteer engines also turn on "
             "the Scraping Browser API's in-session solver. off: skip both. "
             "'always' is refused with exit 2 (not implemented)")
    p.add_argument("--captcha-api", type=_refuse_flag("--captcha-api"), default=None,
                   help="Not implemented: refused with exit 2")
    p.add_argument("--min-score", type=_refuse_flag("--min-score"), default=None,
                   help="Not implemented: refused with exit 2")
    p.add_argument("--twocaptcha-key", dest="refused_twocaptcha_key", type=_refuse_twocaptcha_key,
                   default=None, metavar="KEY",
                   help="Refused with exit 2: the engines make no 2Captcha API call, so a key "
                        "changes nothing (TWOCAPTCHA_KEY is not read by the engines either)")


class SolveBudget:
    """Paid solves allowed for ONE page. Create one per page, before that
    page's retry/rotation loop, and pass it to every solve call site for the
    page: a retry or a fresh exit does not reset it, and `claim()` is called
    before the purchase, never after."""

    def __init__(self, allowance: int = 1):
        self.allowance = allowance
        self.spent = 0

    def claim(self) -> bool:
        if self.spent >= self.allowance:
            return False
        self.spent += 1
        return True


@dataclass
class CaptchaSignal:
    captcha_type: CaptchaType
    sitekey: Optional[str]
    invisible: bool = False


# The Scraping Browser API's managed Chromium ships 2Captcha's OWN
# captcha-solving extension pre-installed (confirmed live, 2026-09-14:
# extension id kjmkgkdkpedkejedfhmfcenooemhbpbo, content scripts named
# .../captcha/{turnstile,amazon_waf,yandex,lemin}/{interceptor,hunter}.js).
# `page.content()` over --cdp-endpoint captures THOSE injected <script>
# tags too, and their src/data attributes contain marker substrings like
# "cf-turnstile" regardless of whether the actual page has a real widget —
# on a real StockX 403 that turned out to be a plain, static Cloudflare
# WAF page with no captcha at all ("Sorry, you have been blocked" — no
# `data-sitekey` anywhere), this alone made GENERIC_BOT_CHALLENGE_MARKERS
# fire and identify_widget() correctly find nothing to solve, which then
# logged as a confusing "captcha-like marker detected but no known
# widget/sitekey" rather than a clean "no captcha present". Stripped here
# so the extension's own always-there code never counts as the page's.
_EXTENSION_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bchrome-extension://[^>]*>.*?</script>', re.I | re.S
)


def _strip_extension_noise(html: str) -> str:
    return _EXTENSION_SCRIPT_RE.sub("", html)


def detect_from_html(html: str, extra_markers: Sequence[str] = ()) -> bool:
    """Broad detection: True if ANY known marker string appears. Cheap and
    deliberately over-inclusive — see module docstring on why detection and
    blocking are decided separately."""
    haystack = _strip_extension_noise(html).lower()
    for marker in (*GENERIC_BOT_CHALLENGE_MARKERS, *extra_markers):
        if marker.lower() in haystack:
            return True
    return False


def identify_widget(html: str) -> Optional[CaptchaSignal]:
    """Best-effort: which widget, and its sitekey. Reconciles the v3-vs-
    v2-invisible ambiguity by trusting the LOADER'S query param over any
    wrapper metadata — a site can label its own wrapper "v3" while shipping
    a `render=explicit` (v2-invisible) loader; sending v3 parameters for a
    v2-invisible widget buys a token the site rejects. `render=<sitekey>`
    means v3; `render=explicit` means v2."""
    html = _strip_extension_noise(html)
    m = _RECAPTCHA_V3_LOADER_RE.search(html)
    if m and not _RECAPTCHA_EXPLICIT_LOADER_RE.search(html):
        return CaptchaSignal(CaptchaType.RECAPTCHA_V3, sitekey=m.group(1))
    for ctype, pattern in _SITEKEY_PATTERNS.items():
        m = pattern.search(html)
        if m:
            return CaptchaSignal(ctype, sitekey=m.group(1))
    return None


def _task_payload(signal: CaptchaSignal, page_url: str, *, proxyless: bool) -> dict:
    type_map = {
        CaptchaType.CLOUDFLARE_TURNSTILE: "TurnstileTaskProxyless" if proxyless else "TurnstileTask",
        CaptchaType.RECAPTCHA_V2: "RecaptchaV2TaskProxyless" if proxyless else "RecaptchaV2Task",
        CaptchaType.RECAPTCHA_V3: "RecaptchaV3TaskProxyless",
        CaptchaType.HCAPTCHA: "HCaptchaTaskProxyless" if proxyless else "HCaptchaTask",
    }
    task = {"type": type_map[signal.captcha_type], "websiteURL": page_url, "websiteKey": signal.sitekey}
    return task


def solve_when_blocked(
    *,
    client: Optional[TwoCaptchaClient],
    page_url: str,
    html: str,
    count_product_links: Callable[[str], int],
    budget: SolveBudget,
    can_use_token: bool,
    extra_markers: Sequence[str] = (),
    proxyless: bool = True,
) -> dict:
    """The default `--solve-captcha when-blocked` policy. Cheap first: count
    product links on the page AS-IS — no readiness wait, no scroll — because
    a page whose products are already rendered is not blocked, and running
    a 20s readiness wait first would burn it even when solving is what
    actually makes products appear on a page that IS gated.

    `budget` and `can_use_token` are required, so no call site can buy a
    solve without deciding both.
    """
    if not detect_from_html(html, extra_markers):
        return {"action": "no_captcha_detected"}

    if count_product_links(html) > 0:
        return {"action": "skipped_products_present"}

    signal = identify_widget(html)
    if signal is None:
        return {"action": "detected_unidentified_widget"}

    if not can_use_token or client is None:
        # Detected and gated, but nothing here would inject a token: do not
        # buy one.
        return {"action": "detected_not_solved", "captcha_type": signal.captcha_type.value}
    if not budget.claim():
        return {"action": "budget_exhausted", "captcha_type": signal.captcha_type.value}

    try:
        task = _task_payload(signal, page_url, proxyless=proxyless)
        token = client.solve_and_wait(task)
        return {"action": "solved", "captcha_type": signal.captcha_type.value, "token": token}
    except TwoCaptchaAuthError as exc:
        return {"action": "warning_no_key", "detail": str(exc)}
    except TwoCaptchaError as exc:
        return {"action": "warning_solver_error", "detail": str(exc)}
