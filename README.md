# scrapemend

**A self-healing scraper engine for Python.** When a website changes its layout, scrapemend notices, names what broke, gets a repaired spec (from Claude, a plugin, or you), **proves the repair against known-good records**, and only then puts it to work.

```
scrape v1 ──> site redesigned ──> layout_changed: title, category, ... broken
                                         │
                       repair proposed ──┴──> verified on held-out records ──> promoted as v2 ──> scrape OK
```

Scrapers are written as **specs**: YAML that says where items are and how to read each field. A spec is data, never code, so a repair is a new spec version that can be diffed, reviewed and rolled back. Nothing an LLM returns is ever executed.

## The demo, in one command

```sh
git clone https://github.com/smiesny0lo/scrapemend && cd scrapemend
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[demo]"
python examples/run_demo.py
```

It starts a local demo site and scrapes it, then redesigns the site underneath the scraper and lets scrapemend heal. It takes about 30 seconds:

```
== 1. The demo site is up at http://127.0.0.1:52130. Scrape it with examples/sources/demo.yaml
  tenders-demo v1, run 20261025T115430Z-ba06: 10 listing pages, 200 items, 200 records, 0 invalid, 0 failed
  status: ok

== 2. The site is redesigned (layout v2); the spec now finds nothing
  status: layout_changed
    layout_changed: the item container matches nothing; the page structure changed (detail pages
    11% similar, listing pages 14% similar to the last good run). On 3 known detail pages:
    broken: title, category, location, published, status, description;
    only via fallbacks: reference, authority, deadline, value
  heal: proposed: proposal 1 passed verification (heal.mode is propose)
    proposal 1 (patch:demo-v2.yaml, confidence 0.90): passed, weakest field 100% of anchors,
    10 other pages checked

== 3. What the verified repair changes (it waits for a human to promote it)
  -    css: table.notices tr.notice
  +    css: table.tender-table tr.tender-row
  ...
== 4. Promote it: the repaired spec becomes version 2
== 5. Scrape the redesigned site again
  tenders-demo v2, run 20261025T115444Z-da57: 10 listing pages, 200 items, 200 records, 0 invalid, 0 failed
  status: ok

== Healed: 200 records, identical to before the redesign (26s)
```

The offline demo uses a repair from `examples/repairs/`. To let **Claude** write it, run `pip install -e ".[demo,anthropic]"`, set `ANTHROPIC_API_KEY`, and use:

```sh
python examples/run_demo.py --provider anthropic
python examples/run_demo.py --provider anthropic --layout 3   # the harder redesign
```

For the same story one command at a time, see [examples/demo.md](examples/demo.md).

## How it works

1. **Scrape.** Each field lists several strategies (css, xpath, label, regex, or a plugin's). The first one whose text converts to the field's type wins. Every downloaded page is kept as a snapshot.
2. **Detect.** Every run is compared with the last good one, using page-structure fingerprints, per-field empty rates, and which strategies won. The run is classified as `ok`, `drift`, `blocked`, `transient`, `layout_changed`, `empty` or `partial`.
3. **Repair.** A provider proposes a patch: new selectors and strategies, nothing else. It can't change the start URL, the rate limit, field types or the checks.
4. **Verify.** On pages under the new layout, every field must reproduce the known values of the **anchor** records, each field on its own. The provider saw three anchors; the rest are held out, so a repair that only memorised the examples fails. Other pages are checked for plausibility.
5. **Promote or quarantine.** A verified repair becomes the next spec version, either automatically (`heal.mode: auto`, with structural evidence) or after `scrapemend promote`. If nothing passes, the source is quarantined and an alert goes out.

Blocks are never worked around. A 401/403, a robots.txt disallow or a bot-wall page stops the run as `blocked`. scrapemend respects robots.txt, keeps per-domain rate limits, backs off on 429/5xx, and sends an identifiable User-Agent.

## Quick start with your own site

```sh
pip install -e .                     # or with the anthropic extra
scrapemend validate my-source.yaml   # every problem, with its line number
scrapemend run my-source.yaml --out records.jsonl
scrapemend check my-source.yaml      # a 5-item health check
scrapemend runs                      # history and statuses
```

When a run fails with `layout_changed`:

```sh
scrapemend heal my-source.yaml --provider anthropic   # or --patch my-fix.yaml
scrapemend diff my-source.yaml 1
scrapemend promote my-source.yaml 1
```

Start from [examples/sources/demo.yaml](examples/sources/demo.yaml). Exit codes: 0 good, 1 usage error, 2 blocked or unreachable, 3 broken data.

## Documentation

- [Writing specs](docs/writing-specs.md): fields, strategies, normalizers, expectations, versions
- [Running scrapemend](docs/running.md): commands, statuses, politeness, alerts, scheduling, the Python API
- [Verification, explained](docs/verification.md): anchors, the gate, held-out examples, quarantine
- [Repair providers](docs/providers.md): the anthropic provider, hand-written patches, writing your own
- [Custom strategies](docs/custom-strategies.md): plugins for anything css, xpath, label and regex can't reach

## Status

v0.1: a working core. It has been tested against the bundled demo site, including its three layouts and its failure modes (random 503s, rate limiting, blocking). It hasn't yet been battle-tested on many real sites. Planned: a browser fetcher for JavaScript-rendered pages, form-based navigation, PDF extraction, and Postgres and S3 backends.

## Development

Requires Python 3.13+.

```sh
pip install -e ".[dev]"
pre-commit install
pytest                     # unit and integration tests, about a minute
ruff check . && ruff format .
mypy                       # strict
```

The demo site runs by itself with `python -m examples.mutating_site --layout 2 --chaos flaky`. See [its README](examples/mutating_site/README.md). Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
