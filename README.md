# scrapemend

A self-healing scraper engine for Python.

When a target site changes its layout, scrapemend detects the breakage, tries fallback extraction strategies, asks a repair provider (optionally an LLM) to propose a fixed source spec, **verifies the proposal against known-good anchor records**, and promotes it only if verification passes.

> **Status:** pre-alpha.

## Principles

- **Sources are config, not code.** Repairs edit versioned specs; nothing an LLM returns is executed.
- **Verify before trusting.** A repair is promoted only if it reproduces known-good records.
- **Polite by default.** robots.txt, per-domain rate limits, an identifiable User-Agent. No CAPTCHA solving or block evasion.
- **Works offline.** The core runs without an LLM.

## Demo site

`examples/mutating_site/` is a fake website whose layout can be switched while it runs, and which can simulate failures: random 503s, rate limiting, and blocking. scrapemend uses it to show that it can detect breakage and heal. See [its README](examples/mutating_site/README.md).

```sh
pip install -e ".[demo]"
python -m examples.mutating_site --layout 1 --chaos off
```

In a second terminal, scrape it with the demo spec:

```sh
scrapemend validate examples/sources/demo.yaml
scrapemend run examples/sources/demo.yaml --out records.jsonl
```

Records are written as JSON lines; a report goes to stderr. Every downloaded page is kept under `snapshots/` so it can be re-parsed later without fetching it again, and each run is judged against the last good one (stored in `scrapemend.db`):

| Status | Meaning | Exit code |
|---|---|---|
| `ok`, `drift` | data is fine (`drift`: it parses, but looks different from last time) | 0 |
| `blocked`, `transient` | the site refused us or kept failing; never worked around | 2 |
| `layout_changed`, `empty`, `partial` | the data is broken; the report names the failing fields | 3 |

Switch the demo site to another layout (`curl -X POST http://127.0.0.1:8000/admin/layout/2`) and run again to see `layout_changed`. `scrapemend check <spec>` samples 5 items without touching the baseline; `scrapemend runs` lists the history.

## Development

Requires Python 3.13+.

```sh
pip install -e ".[dev]"
pre-commit install
pytest
ruff check . && ruff format .
mypy
```

## License

[MIT](LICENSE)
