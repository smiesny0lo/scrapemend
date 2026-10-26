# scrapemend

**A self-healing scraper engine for Python.** When a website changes its layout, scrapemend notices, names what broke, gets a repaired spec (from Claude, a plugin, or you), **runs the repair over the site and compares every record with the last good run**, and only then puts it to work.

```
scrape v1 ──> site redesigned ──> layout_changed: title, category, ... broken
                                         │
                       repair proposed ──┴──> shadow run: same records as v1? ──> promoted as v2 ──> scrape OK
```

Scrapers are written as **specs**: YAML that says where items are and how to read each field. A spec is data, never code, so a repair is a new spec version that can be diffed, reviewed and rolled back. Nothing an LLM returns is ever executed.

## The demo, in one command

```sh
git clone https://github.com/smiesny0lo/scrapemend && cd scrapemend
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[demo]"
python examples/run_demo.py
```

It starts a local demo site and scrapes it, then redesigns the site underneath the scraper and lets scrapemend heal. It takes about 40 seconds, and CI runs it on Linux and Windows for every pull request and every push to `main`:

```
== 1. The demo site is up at http://127.0.0.1:61424. Scrape it with examples/sources/demo.yaml
  tenders-demo v1, run 20261025T223804Z-6acb: 10 listing pages, 200 items, 200 records, 0 invalid, 0 failed
  status: ok

== 2. The site is redesigned (layout v2); the spec now finds nothing
  status: layout_changed
    layout_changed: the item container matches nothing; the page structure changed (detail pages
    11% similar, listing pages 14% similar to the last good run). On 3 known detail pages:
    broken: title, category, location, published, status, description;
    only via fallbacks: reference, authority, deadline, value
  heal: shadow run: the whole source, all 200 of the last good run's records: about 187
    requests (10 listing pages and 200 detail pages, 23 of them stored), at least 9s at 20
    requests a second
  heal: proposed: proposal 1 passed verification (heal.mode is propose)
    proposal 1 (patch:demo-v2.yaml, confidence 0.90): passed
      passed   regexes: 1 regex: 1 on Python's re (at most 1s a search)
      passed   listing: 20 items on the first listing page, 3 listing pages along the next-page link
      passed   anchors: 10 of 10 anchor pages compared; weakest field 100%
      passed   sample: 10 other pages
      passed   known values: category, location, status on 20 pages
      passed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the last
               good run's records compared

== 3. What the verified repair changes (it waits for a human to promote it)
  -    css: table.notices tr.notice
  +    css: table.tender-table tr.tender-row
  ...
== 4. Promote it: the repaired spec becomes version 2
  promote: shadow run: the whole source, all 200 of the last good run's records: every page
    it reads is stored, so no requests
  tenders-demo: proposal 1 verified again; every check passed; promoted; v2 is now active
== 5. Scrape the redesigned site again
  tenders-demo v2, run 20261025T223831Z-ef9f: 10 listing pages, 200 items, 200 records, 0 invalid, 0 failed
  status: ok

== Healed: 200 records, identical to before the redesign (39s)
```

The offline demo uses a repair from `examples/repairs/`. To let **Claude** write it, run `pip install -e ".[demo,anthropic]"`, set `ANTHROPIC_API_KEY`, and use:

```sh
python examples/run_demo.py --provider anthropic
python examples/run_demo.py --provider anthropic --layout 3   # the harder redesign
```

For the same story one command at a time, see [examples/demo.md](examples/demo.md).

## How it works

1. **Scrape.** Each field lists several strategies (css, xpath, label, regex, or a plugin's). The first one whose text converts to the field's type wins. Every downloaded page is kept as a snapshot: for the newest 10 runs, and for older runs that a comparison or a repair still needs.
2. **Detect.** Every run is compared with the last good one, using page-structure fingerprints, per-field empty rates, and which strategies won. The run is classified as `ok`, `drift`, `blocked`, `transient`, `layout_changed`, `empty` or `partial`.
3. **Repair.** A provider proposes a patch: new selectors and strategies, nothing else. It can't change the start URL, the rate limit, field types or the checks.
4. **Verify.** Quick checks first: on pages under the new layout, every field must reproduce the known values of the **anchor** records, each field on its own, and the listing and the next-page link must still work. Then the **shadow run**: the repaired spec crawls the source, politely, and its records are compared with the last good run's, by key. Stable fields must be identical on every record both runs have; a volatile one (a price, a status) may have changed on at most 10% of them, and a value may never become empty. A provider's regexes run on RE2, which no page can make slow. [What the gate guarantees, and what it can't catch](docs/verification.md#what-the-gate-guarantees).
5. **Promote or quarantine.** A repair that passed every check becomes the next spec version, either automatically (`heal.mode: auto`, with structural evidence, as a provisional version until you `scrapemend confirm` it) or after `scrapemend promote`. If nothing passes, the source is quarantined and an alert goes to your notifiers (`--notify`). A repair whose checks couldn't all run (a page that won't load) is never promoted automatically: only `scrapemend promote --force`, never past a check that failed.
6. **Watch.** The runs after a promotion are compared with the last good run: an emptier field, a common status gone, or numbers that changed magnitude are `drift`, and alert. A drift run never becomes the last good run by itself, so every run alerts until a human checks the data and runs `scrapemend accept-drift`.

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
- [Verification, explained](docs/verification.md): anchors, the shadow run, failing closed, what the gate guarantees
- [Repair providers](docs/providers.md): the anthropic provider, hand-written patches, writing your own
- [Custom strategies](docs/custom-strategies.md): plugins for anything css, xpath, label and regex can't reach

## Status

v0.1: a working core. It has been tested against the bundled demo site, including its three layouts and its failure modes (random 503s, rate limiting, blocking). It hasn't yet been battle-tested on many real sites. Planned: a browser fetcher for JavaScript-rendered pages, form-based navigation, PDF extraction, and Postgres and S3 backends.

## Development

Requires Python 3.13+.

```sh
pip install -e ".[dev]"
pre-commit install
pytest                     # unit and integration tests, a minute or two
ruff check . && ruff format .
mypy                       # strict
```

The demo site runs by itself with `python -m examples.mutating_site --layout 2 --chaos flaky`. See [its README](examples/mutating_site/README.md). Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
