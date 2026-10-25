# Demo walkthrough

The same story as `python examples/run_demo.py`, one command at a time, so you can look at each step. It takes about 5 minutes.

You need two terminals in the repository folder, both with the virtual environment active and `pip install -e ".[demo]"` done.

**On Windows PowerShell:** type `curl.exe` (plain `curl` is an alias for something else), and use `--out` rather than `>`.

## 1. Start the demo site

Terminal 1:

```sh
python -m examples.mutating_site
```

It serves 200 made-up tender notices at <http://127.0.0.1:8000>. Its layout can be switched while it runs, and the content stays exactly the same.

## 2. Scrape it

Terminal 2:

```sh
scrapemend validate examples/sources/demo.yaml
scrapemend run examples/sources/demo.yaml --out before.jsonl
```

The run takes about 10 seconds: 20 requests a second, politely spaced. It ends with `status: ok` and writes 200 records to `before.jsonl`.

That good run became the **baseline**: page fingerprints and 10 known-good **anchor** records, stored in `scrapemend.db`. Every page is kept under `snapshots/`.

## 3. Redesign the site

```sh
curl.exe -X POST http://127.0.0.1:8000/admin/layout/2
```

Open <http://127.0.0.1:8000> in a browser: same notices, new markup. Layout v2 renamed every CSS class and changed the pagination.

## 4. Watch it break

```sh
scrapemend run examples/sources/demo.yaml --out broken.jsonl
```

The command exits with code 3 (broken data) and reports:

```
status: layout_changed
  layout_changed: the item container matches nothing; the page structure changed ...
  On 3 known detail pages: broken: title, category, location, published, status, description;
  only via fallbacks: reference, authority, deadline, value
heal: quarantined: repair provider none proposed nothing
```

The container found nothing, so scrapemend re-checked three anchor pages to find out which fields broke. Four fields still work through their fallback strategies.

No repair provider is configured, so the source is **quarantined**. Run it again and it refuses until someone repairs it:

```sh
scrapemend run examples/sources/demo.yaml
#  tenders-demo is quarantined: repair provider none proposed nothing
```

## 5. Repair it

Use the hand-written repair:

```sh
scrapemend heal examples/sources/demo.yaml --patch examples/repairs/demo-v2.yaml
```

or let Claude write one (this needs `pip install -e ".[anthropic]"` and `ANTHROPIC_API_KEY`):

```sh
scrapemend heal examples/sources/demo.yaml --provider anthropic
```

Either way, the repair goes through the verification gate before anything changes:

```
heal: proposed: proposal 1 passed verification (heal.mode is propose); promote it with: scrapemend promote <spec> 1
  proposal 1 (patch:demo-v2.yaml, confidence 0.90): passed
    passed   regexes: 1 regex: 1 on Python's re (at most 1s a search)
    passed   listing: 20 items on the first listing page, 3 listing pages along the next-page link
    passed   anchors: 10 of 10 anchor pages compared; weakest field 100%
    passed   sample: 10 other pages
    passed   known values: category, location, status on 20 pages
    passed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the last good run's records compared
```

Every field reproduced the known values on all 10 anchor pages, and the next-page link led through three listing pages of new notices. Then the **shadow run**: the repaired spec crawled the whole site, about 200 requests at the spec's rate limit, and read every one of the 200 notices exactly as the good run had. The pages it fetched are stored, so `promote` needs none of them again.

To see a rejection, try a repair that maps one field wrongly:

```sh
scrapemend heal examples/sources/demo.yaml --patch tests/fixtures/repairs/v2-wrong-field.yaml
#    failed   anchors: 10 of 10 anchor pages compared; weakest field 0%
#      problem: location reproduces 0/10 anchor values (needs 100%); expected 'Ravensholm', got 'Energy'
#      problem: category and location read the same element on 10 of 10 anchor pages
#    ...
#    not run  shadow run: skipped: a quicker check failed
```

Or one that is right on most notices: its `status` regex only knows the values Open and Closed, so it passes on the anchors and fails in the shadow run.

```sh
scrapemend heal examples/sources/demo.yaml --patch tests/fixtures/repairs/v2-status-enumerates-examples.yaml
#    passed   anchors: 10 of 10 anchor pages compared; weakest field 60%
#    ...
#    failed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the last good run's records compared
#      problem: status became empty on 39 of 200 records that had a value in the last good run (a volatile field may change, but not become empty); expected 'Awarded', got None
```

Nothing passed these times, so the source is quarantined again. Promoting the verified proposal 1 in the next step lifts that.

## 6. Review and promote

```sh
scrapemend proposals examples/sources/demo.yaml
scrapemend diff examples/sources/demo.yaml 1
scrapemend promote examples/sources/demo.yaml 1
```

`promote` runs the verification gate once more before it changes anything, on the pages `heal` stored. The repair is now spec **version 2**. The YAML file on disk is untouched: specs are versioned, never edited in place. `scrapemend versions examples/sources/demo.yaml` shows both versions.

## 7. Scrape again

```sh
scrapemend run examples/sources/demo.yaml --out after.jsonl
```

`status: ok` again, now with `tenders-demo v2`, and `after.jsonl` holds the same 200 records as `before.jsonl`.

## More to try

- **Undo:** `scrapemend rollback examples/sources/demo.yaml` makes v1 active again.
- **The harder redesign:** `curl.exe -X POST http://127.0.0.1:8000/admin/layout/3` is a card grid where several fields are merged into lines of text. The repair is `examples/repairs/demo-v3.yaml`, or ask Claude.
- **Failure modes:** restart the site with `--chaos flaky`, `--chaos ratelimit` or `--chaos blocked`. The first two still succeed, thanks to retries and backoff. Blocked stops at once with exit code 2 and is never retried.
- **Repair without a human:** set `heal: { mode: auto }` in a copy of the spec (with a new `version`). A verified repair is then promoted immediately, and the run scrapes again with it.
- **History:** `scrapemend runs` shows every run and its status.
