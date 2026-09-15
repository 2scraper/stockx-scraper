# Security

## Reporting a vulnerability

Please report security issues privately — open a GitHub security advisory on this repo, or email security@2captcha.com — rather than a public issue. Include the version/commit and a minimal reproduction.

## What this tool does with credentials

- `TWOCAPTCHA_KEY`, `STOCKX_PROXY`, `STOCKX_CDP_ENDPOINT` are read only from `.env` or the environment (see `env_config.py`) — never from the command line, and never logged in full (`proxy_pool.py` masks credentials in every log line, keeping only host:port, which is the useful part of a log and not the secret).
- This project's own code doesn't phone home. The only network calls it makes are to `stockx.com` and, when configured, `api.2captcha.com` / `cb.2captcha.com`. **One caveat on the Selenium engine**: Selenium's own bundled Selenium Manager sends anonymous usage stats to `plausible.io` by default whenever it launches a browser — confirmed live (visible as `Error sending stats to Plausible: ...` on stderr) — which `selenium_scraper.py` now disables on your behalf (`SE_AVOID_STATS=true`, set as a default rather than forced, so it never overrides a value you set yourself) so this project's "nothing phones home" claim actually holds. Selenium Manager can still make a *separate* network call to resolve a matching `chromedriver` version if one isn't already reachable on `PATH` / `SELENIUM_CHROME_BIN` — set that variable to a Chrome/Chromium binary you already have to avoid it entirely (see `selenium_scraper.py`'s module docstring).
- This is a scraper, not an account tool: it never logs in, never touches an authenticated StockX session, and never writes anything back to stockx.com.
