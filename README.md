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
