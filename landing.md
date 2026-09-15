# StockX Scraper by 2scraper

**Open-source product & market data scraper for stockx.com — three engines, your own infrastructure by default, 2Captcha's paid products when you actually need them.**

Pull sneaker, streetwear and collectibles data — name, brand, style ID, colorway, lowest ask, highest bid, last sale, retail price — straight from StockX into JSON or CSV.

[**View source on GitHub →**](https://github.com/2scraper/stockx-scraper)

---

## Before you scrape: the official API

StockX publishes an official, authenticated **Catalog / Order / Listing API** at [developer.stockx.com](https://developer.stockx.com). If your use case fits within it and you can get approved, it's the supported, ToS-compliant way to pull StockX data. This scraper exists for everything the official API doesn't cover.

## What was actually measured

An ordinary headless Chromium, run with no proxy and no 2Captcha key, cleared stockx.com's Cloudflare bot-management scripts and returned full search, category and product data on every page requested. Say that plainly instead of selling around it: the paid products below buy you volume from many addresses, a specific exit country, or a consistent device identity — not passage through a wall that wasn't there. Full numbers, and what does show up in practice, are in the [repository README](https://github.com/2scraper/stockx-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (primary), **Selenium**, and **Puppeteer** (via pyppeteer), all producing the identical output schema and exit codes
- Search by term, a category/browse listing, or a single product page
- Reads StockX's own embedded data payload first (`__NEXT_DATA__` react-query cache on listings, JSON-LD on product pages) — no fragile DOM scraping as the primary path
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar on every run (status, pages completed, price-confirmation rate)
- Optional 2Captcha integration, wired in but never required to get started

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Captcha solving — [2captcha.com](https://2captcha.com)** | Detects a challenge, decides whether it's actually blocking you (not just present), solves it |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Drop credentials into `.env`, rotated automatically with per-exit failure tracking |

## Who this is for

Resale market researchers, price-tracking tools, and sneaker/streetwear data teams who need coverage the official API doesn't provide.

## Get started

```bash
git clone https://github.com/2scraper/stockx-scraper.git
cd stockx-scraper
pip install -r requirements-playwright.txt && playwright install chromium
cp .env.example .env   # optional — only needed for the paid products

python3 playwright_scraper.py --category sneakers --pages 3 --format json --out stockx_sneakers.json
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/stockx-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
