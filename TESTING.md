# Testing with real credentials and the live site

Everything in this repo's own CHANGELOG/README was verified either offline
(`smoke_test.py`, fixture-replay) or against 2Captcha's REST API directly.
**No engine has been run against the live stockx.com from this project's own
build environment** — outbound access to it was blocked there. This file is
the checklist for doing that for real, on a machine that isn't blocked.

Run everything below from a normal terminal on your own machine (not a
sandboxed/restricted shell) — `~/stockx-scraper` or wherever this repo lives
for you.

## 1. Basic setup

```bash
cd ~/stockx-scraper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env
```

Open `.env` and fill in the values you already have — `TWOCAPTCHA_KEY`,
and `STOCKX_PROXY` / `STOCKX_CDP_ENDPOINT` if you want to test those too.
Leave a line blank/absent rather than half-filled — `env_config.py` treats
an empty value as "not set", never as a literal empty credential.

Sanity-check what got picked up, without ever printing a secret:

```bash
python3 env_config.py
```

## 2. The free path, against the real site

This alone is the most important test — it's the one thing that could not
be verified at all during the build:

```bash
python3 playwright_scraper.py --category sneakers --pages 3 --format json --out /tmp/stockx_test.json
echo "exit code: $?"
cat /tmp/stockx_test.json.meta.json
```

Expect `exit code: 0` and `"status": "complete"`, `"pages_completed": 3`,
a `product_count` in the dozens, and `price_confirmed_pct` close to 1.0.
If a captcha shows up instead (unlikely on a residential IP, per README —
but corporate/VPN networks are more likely to trigger one), you'll see
`"status": "blocked"` — that itself is useful information; save the
`.meta.json` and the `--dump-html` output (add `--dump-html` to the command
above) if you want to report it.

Try a single product page too, to exercise the JSON-LD path specifically:

```bash
python3 playwright_scraper.py --url "https://stockx.com/air-jordan-4-retro-og-flight-club" --format csv --out /tmp/stockx_product.csv
cat /tmp/stockx_product.csv
```

(if that exact URL 404s by the time you read this — StockX listings do
churn — swap in any current product URL from stockx.com)

## 3. Selenium, for real

Your own machine likely has a real Chrome install and normal network
access, so Selenium Manager should just fetch a matching chromedriver on
its own — a simpler path than the workaround this repo's own build needed:

```bash
python3 -m venv .venv-selenium   # separate venv — see README "Engines"
source .venv-selenium/bin/activate
pip install -r requirements-selenium.txt
python3 selenium_scraper.py --category sneakers --pages 3 --out /tmp/stockx_selenium.json
```

If chromedriver still doesn't match your Chrome version and Selenium
Manager can't fetch one (a locked-down corporate network, say), the same
fix this repo's build used works here too:

```bash
pip install chromedriver-py==<your-chrome-major-version>.<whatever-pip-offers>
export SELENIUM_CHROME_BIN="$(which google-chrome || which chromium)"   # only if you also want a specific Chrome binary
# put the chromedriver-py binary on PATH, or pass its path directly — see README "Engines"
```

## 4. Puppeteer (pyppeteer), for real

```bash
python3 -m venv .venv-puppeteer
source .venv-puppeteer/bin/activate
pip install -r requirements-puppeteer.txt
python3 puppeteer_scraper.py --category sneakers --pages 3 --out /tmp/stockx_puppeteer.json
```

## 5. The 2Captcha REST API, with your real key

Confirms the key and hits real, billed-nothing endpoints first:

```bash
python3 -c "
import env_config
from scraper_api_client import TwoCaptchaClient
args = type('A', (), {'twocaptcha_key': None, 'proxy': None, 'cdp_endpoint': None, 'url': None})()
env_config.apply_env(args)
c = TwoCaptchaClient(args.twocaptcha_key)
print('balance: \$%.2f' % c.get_balance())
"
```

## 6. The Scraping Browser API (`--cdp-endpoint`), for real

`STOCKX_CDP_ENDPOINT` in `.env` is picked up automatically — you do not need
to pass `--cdp-endpoint` on the command line:

```bash
python3 playwright_scraper.py --category sneakers --pages 1 --out /tmp/stockx_cdp.json
```

**Gotcha**: if `.env` has BOTH `STOCKX_CDP_ENDPOINT` and `STOCKX_PROXY` set,
the code ignores `STOCKX_PROXY` and warns — a CDP session already carries
its own exit IP, stacking a second one on top is a contradiction, not
better cover (this is also true of a fingerprint — never set one over
`--cdp-endpoint` either). Comment out whichever one you're not testing in
`.env` if you want to test them in isolation.

Since this is a live remote browser session, it's also the one path that
could plausibly encounter a real challenge — if it does, look for
`Captcha.detected` / `Captcha.solveFinished` / `Captcha.solveFailed` in the
log output; that's 2Captcha's own CDP domain handling it end-to-end, which
is the one place captcha solving is fully wired up (see README "Known
limitations" on why a locally-launched browser's captcha token isn't
auto-injected the same way).

## 7. The residential proxy (`--proxy` / `STOCKX_PROXY`), for real

Same auto-pickup from `.env` (comment out `STOCKX_CDP_ENDPOINT` first per
the gotcha above):

```bash
python3 playwright_scraper.py --category sneakers --pages 3 --out /tmp/stockx_proxy.json
```

Compare `price` / currency against the no-proxy run from step 2 — a
different exit country should show a different currency (see README
"What was actually measured" on the Netherlands-exit EUR observation).

## 8. Push to GitHub and let CI do the rest

```bash
git remote add origin git@github.com:2scraper/stockx-scraper.git
git push -u origin main
git push --tags
```

Then, in the GitHub repo's Settings:

- **Secrets and variables → Actions**: add `TWOCAPTCHA_KEY`, `STOCKX_PROXY`
  (only if you want the canary using it), and `CLAUDE_CODE_OAUTH_TOKEN` (for
  `claude.yml` / `claude-code-review.yml` — both silently no-op without it,
  by design, rather than failing every PR check).
- **About** (gear icon next to the repo description): description, topics,
  homepage — see `CLAUDE.md` §2 for the exact pattern; nothing in this repo
  can set these for you, they're GitHub-side settings, not files.
- **Actions → canary → Run workflow**: dispatch it manually at least once
  (it has `workflow_dispatch` for exactly this) rather than waiting a day
  for the cron and trusting the badge blind.

## 9. What "done" looks like

- `tests.yml` green on both Python versions and all three `engine-smoke`
  matrix legs.
- One manually-dispatched `canary.yml` run, green, with a product count and
  price-confirmation percentage you've actually looked at (not just the
  badge).
- At least one of steps 2-7 above run by a human, with the actual output
  looked at — not just "exit code 0".
