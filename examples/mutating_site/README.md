# Mutating demo site

A small fake procurement portal for testing specmend. It serves 200 synthetic tender notices and can switch between three page layouts while it is running. Every layout shows **the same records at the same URLs**. Only the HTML changes, the way a real site changes when it is redesigned.

```sh
pip install -e ".[demo]"
python -m examples.mutating_site                 # layout 1, no chaos
python -m examples.mutating_site --layout 3 --chaos flaky --failure-rate 0.5
python -m examples.mutating_site --help
```

Open <http://localhost:8000>. To switch layouts or chaos while the site is running, use the "Try it out" buttons at <http://localhost:8000/docs>, or run:

```sh
curl -X POST http://localhost:8000/admin/layout/2
curl -X POST http://localhost:8000/admin/chaos/flaky,ratelimit
curl -X POST http://localhost:8000/admin/chaos/off
```

On Windows PowerShell, type `curl.exe` instead of `curl`. Plain `curl` there is an alias for a different command.

`uvicorn examples.mutating_site.app:app` also works. In that case configure the site through the environment variables `LAYOUT`, `CHAOS` and `CHAOS_FAILURE_RATE`.

## Pages

| URL | Content |
|---|---|
| `/notices?page=N` | Listing, 20 notices per page, newest first: title, authority, deadline, value |
| `/notices/{id}` | Detail page with all fields: reference, authority, category, location, published, deadline, value, status, description |
| `/robots.txt` | Allows everything except `/admin/` |
| `/admin/layout` | `GET` returns the current layout. `POST /admin/layout/{n}` switches to layout `n` |
| `/admin/chaos` | `GET` returns the current chaos modes. `POST /admin/chaos/{modes}?failure_rate=0.3` changes them |

## Layouts

| Layout | What changes | What a scraper needs to recover |
|---|---|---|
| **v1** | Baseline: a `<table>` listing and a `<dl>` of labelled fields with semantic class names (`td.title`, `dd.deadline`, `li.next`) | Nothing. This is what the spec is first written for |
| **v2** | Classes renamed (`dd.deadline` becomes `dd.tender__closing`), values wrapped in extra `<div>`/`<span>` elements, pagination changed from `li.next a` to `a[rel=next]` | New CSS selectors. The page structure is the same |
| **v3** | Card grid instead of a table, layout-only utility classes (`text-sm`, `mt-2`), the title moved from `h1` to `h2`, several fields merged into text lines (`Ref. TN-2026-0001 · Published 8 Aug 2026 · Status: Open`), numbered pagination | Label-based and regex fallbacks, because the class names no longer say what anything is |

## Chaos modes

Chaos makes the site misbehave the way real sites do, so the scraper's failure handling can be tested. Modes can be combined, for example `--chaos flaky,ratelimit`. Chaos only affects `/notices` pages. `/admin`, `/docs` and `/robots.txt` always respond, so you can always switch chaos off again.

| Mode | Behaviour | What a polite scraper should do |
|---|---|---|
| `flaky` | A random share of requests (`--failure-rate`, default 0.2) get **503 Service Unavailable** | Treat it as `transient`: back off and retry |
| `ratelimit` | More than 10 requests in 10 seconds get **429 Too Many Requests** with a `Retry-After` header | Treat it as `transient`: wait `Retry-After` seconds, then slow down |
| `blocked` | Every request gets **403** and an "Access denied" page | Treat it as `blocked`: stop the run and alert. Never try to get around it |

Flaky failures come from a seeded random generator, so the same sequence of requests always fails in the same places.

## Records

The records come from a fixed random seed (`records.py`), so every run produces identical data. About 8% of notices show the value as "Not disclosed", to exercise null handling.
