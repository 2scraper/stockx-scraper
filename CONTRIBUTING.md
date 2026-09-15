# Contributing

1. Fork, branch, make your change.
2. `pip install -r requirements-playwright.txt` (or `-selenium` / `-puppeteer`) and run `python3 smoke_test.py` — it must pass with **no** engine installed at all, so if you're only testing one engine, that's still a meaningful run.
3. If you touched `product_parser.py`, capture the real page you're changing behaviour for first (see `CLAUDE.md`'s bootstrapping section if you have access to it, or just: save a real, scrubbed HTML/JSON capture under `tests/fixtures/` and assert real values against it in `smoke_test.py` — not just "some rows populated").
4. Keep the three engines behaving identically: same exit codes, same `Product` schema, same CLI flags. If one needs to diverge (see `selenium_scraper.py`'s CDP-credential limitation for an example), say why in a comment.
5. Open a PR. CI runs the offline suite on two Python versions plus one `engine-smoke` job per engine, each in its own virtualenv.

Bug reports and feature requests: open an issue. Please include the exact command you ran and the `.meta.json` sidecar from the run, not just a description.
