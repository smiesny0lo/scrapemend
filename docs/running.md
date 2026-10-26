# Running specmend

## Commands

| Command | What it does |
|---|---|
| `specmend validate SPEC` | check a spec file; every problem with its line number |
| `specmend run SPEC [--out FILE]` | scrape with the active spec version, write JSON lines, judge the run, heal per `heal.mode` |
| `specmend check SPEC [--sample 5]` | scrape a small sample and judge it; writes no records, never changes the baseline, never heals |
| `specmend runs [SOURCE_ID]` | run history, newest first |
| `specmend accept-drift RUN_ID` | make a drift run the last good run, once you have checked its data (see [drift](#drift)) |
| `specmend confirm SPEC VERSION` | confirm a version `heal` promoted automatically, once you have checked its data: its runs become the last good run again (see [provisional versions](#provisional-versions)) |
| `specmend heal SPEC [--provider P \| --patch FILE]` | repair the latest failed run, verify the repair (with a shadow run over the source), promote it or leave it for review |
| `specmend proposals SPEC` | repair proposals with their verification score |
| `specmend diff SPEC ID` | what proposal ID would change, as a diff of the spec |
| `specmend promote SPEC ID [--force]` | verify proposal ID again and make it the active version. `--force` promotes it when a check couldn't run (never when one failed), and applies a proposal made for another version than the active one to the active version (verified again too) |
| `specmend rollback SPEC [--to N]` | make an older version active again |
| `specmend versions SPEC` / `show SPEC` | stored versions / one version as YAML |
| `specmend prune SPEC [--dry-run]` | delete the snapshots of older runs no one needs any more (every run does this too; see [snapshots](#snapshots)) |

`python -m specmend ...` works the same way, which helps when the `specmend` script isn't on your PATH.

Shared options, also available as environment variables (see `.env.example`):

| Option | Variable | Default |
|---|---|---|
| `--db` | `SPECMEND_DB` | `specmend.db`: run history, baselines, anchors, spec versions, proposals |
| `--snapshots` | `SPECMEND_SNAPSHOT_DIR` | `snapshots/`: the pages the newest runs downloaded (see [snapshots](#snapshots)) |
| `--user-agent` | `SPECMEND_USER_AGENT` | `specmend/<version> (+https://github.com/smiesny0lo/specmend)` |
| `--notify` | `SPECMEND_NOTIFY` | none; `stdout`, `log` or a plugin's name |
| `--provider` | `SPECMEND_REPAIR_PROVIDER` | `none`; `anthropic` or a plugin's name |
| `--start` | none | the spec's `fetch.start`; a runtime override that is never stored |

Records go to stdout or `--out` as JSON lines. Decimals are written as exact strings (`"566600.00"`) and dates as ISO strings. Invalid records (a required field missing, a `pattern` not matched) are held back unless you pass `--keep-invalid`. The report goes to stderr.

Everything specmend prints, records and reports alike, is UTF-8 on every platform, including Windows pipes and files. On Windows PowerShell 5.1, prefer `--out` to `>`: PowerShell decodes a program's output in the console's code page and re-encodes it as UTF-16, which garbles anything that isn't ASCII.

## What a run reports

Every run is compared with the **last good run**: the latest complete run whose status was `ok`, or a `drift` run you accepted (see [drift](#drift)):

| Status | Signal | Exit code |
|---|---|---|
| `ok` | nothing wrong | 0 |
| `drift` | the data parses, but is distributed unlike the last good run's: far fewer or more records, a field's empty rate moved by more than 5 points, a value of a small set (a status) that 5% of records had is gone, or more than 10% of a number field's values changed magnitude; alert only, never repaired | 0 |
| `blocked` | 401/403, robots.txt disallows, or a bot-wall page; the run stops and nothing tries to get around it | 2 |
| `transient` | timeouts, 5xx or 429 that outlasted the retries | 2 |
| `layout_changed` | the page structure changed (fingerprints) and extraction fails | 3 |
| `empty` | the item container matched nothing on a page whose structure didn't change | 3 |
| `partial` | some fields or pages fail, but the structure is unchanged | 3 |

Exit code 1 means a usage or spec error. A quarantined source also exits with 3 until it's repaired. A run whose heal was blocked exits with 2, whatever the run's own status.

Each report says how many HTTP requests that run sent, retries and robots.txt included (`requests: 211 (0 retried)`). When a run heals, the heal reports its own requests (mostly the shadow run's), and so does the run after a promotion: no line counts another step's.

**Naming the broken fields.** When the item container finds nothing, specmend re-checks three detail pages it already knows (anchors) to name the broken fields:

```
status: layout_changed
  layout_changed: the item container matches nothing; the page structure changed (detail
  pages 5% similar, listing pages 8% similar to the last good run). On 3 known detail
  pages: broken: title, category, ...; only via fallbacks: reference, authority, ...
    failing: item.container, fetch.paginate.next, title, category, ...
```

**Warnings** don't fail a run but still alert. For example, *fields working only through fallback strategies* means the data is fine, but the page is starting to change.

Only a complete `ok` run of `specmend run` updates the baseline: the last good run, its page fingerprints, its anchors and its records. `check` and `--limit` runs never do, and neither does a `drift` run, or a run of a [provisional version](#provisional-versions).

### Drift

A `drift` run writes its records and exits with 0, but alerts: its data is distributed unlike the last good run's. That may be the world changing (statuses renamed, a new kind of notice), or a repair that is wrong on some records (see [what the gate can't catch](verification.md#what-it-cant-catch)). specmend can't tell which, so a drift run doesn't become the baseline. The runs after it are compared with the same last good run as before, so while the data stays like this they drift too, and alert on every run, until you decide:

```
status: drift
  drift: value: 15% of its values have another number of digits than in the last good run (7 digits: 0%, was 15%)
    failing: value
  the last good run stays the baseline: runs keep drifting until you check this data and accept it with: specmend accept-drift 20261026T090000Z-3f9a
```

- If the data is right, accept the run: `specmend accept-drift RUN_ID` makes it the last good run, with its page fingerprints, anchors and records. The runs after it are compared with it.
- If it is wrong, fix the spec (roll back, or write a new version) and run again.

Only the latest drift run since the last good run can be accepted: its anchors and records are kept until then. An `ok` run in between becomes the last good run instead, and nothing older can be accepted.

### Provisional versions

A version that `heal` promoted by itself (`heal.mode: auto`) is **provisional**: no human has looked at it. Its runs write their records as usual, but they don't become the last good run. Later runs, and the repairs of later breakages, are still compared with the last good run before the promotion, and every run of the version warns:

```
  warning: v2 was promoted automatically and is provisional: its runs don't become the last good run, and runs and repairs are compared with run 20261026T090000Z-3f9a, until you check its data and confirm it: specmend confirm <spec> 2
```

So a repair that is wrong on a few records, in a way the gate can't catch, never becomes the standard the next repair is held to. Check its data, then:

- If it is right, confirm it: `specmend confirm SPEC VERSION`. Its latest good run becomes the last good run, with its page fingerprints, anchors and records, and so does every good run of it after that.
- If it is wrong, roll back, or fix the spec in a new version.

A version you promote with `specmend promote` is confirmed by you. Accepting a drift run of a provisional version (`accept-drift`) confirms that version too. `specmend versions` marks the provisional ones.

Don't leave a version provisional for long. The last good run it is compared with keeps ageing, and on a source whose records come and go, a correct repair verified against it may fail for coverage (see [sources that churn](verification.md#sources-that-churn)).

## Snapshots

Every page a run downloads is stored under `--snapshots`, one directory per run, before it is parsed. A source keeps the snapshots of its newest `snapshots.keep_runs` runs (default 10). When a run ends, the snapshots of older runs are deleted, except those still needed:

- **the last good run**: runs and repairs are compared with it, and a repair provider is shown its pages;
- **a drift run that can still be accepted**, or **the latest good run of a provisional version**: `accept-drift` or `confirm` would make it the last good run;
- **the failed run of a proposal that can still be promoted** (accepted or unverified): `promote` verifies it again on that run's pages, including the ones `heal` fetched for the shadow run;
- **the last failed run**: `heal` repairs the latest failed run by default, on its pages.

The run's report says when it deleted some (`pruned: the snapshots of 2 older runs`). `specmend prune SPEC` prunes at any time, with the active version's `keep_runs`; `--dry-run` only lists what would go:

```
$ specmend prune examples/sources/demo.yaml --dry-run
tenders-demo: keeps the snapshots of the newest 10 runs (snapshots.keep_runs: 10) and of older runs still needed
  would delete  20261019T060000Z-1a2b
  would delete  20261020T060000Z-3c4d
  kept          20261018T060000Z-5e6f: the last good run
  kept          20261021T060000Z-7a8b: proposal 3 can still be promoted
```

A store that fails to delete (a file in use) is logged and skipped: retention never fails a run. Deleting a run's snapshots keeps its report, so `specmend runs` still lists it.

## Politeness

specmend is polite by default, and none of this can be turned off from a spec repair:

- **robots.txt** is read once per site and checked before every request, redirect targets included.
  - A missing robots.txt (4xx) means no rules.
  - An unreachable one (5xx) means no crawling.
- **Spacing** of `1 / rate.rps` seconds is kept between requests to the same host.
  - A `Crawl-delay` in robots.txt can only make it slower.
  - Every 429 doubles the spacing for the rest of the run.
- **Retries:** timeouts, connection errors, 408, 429 and 5xx are tried up to 4 times in all, with backoff (1 s, 2 s, 4 s plus jitter) or exactly the wait `Retry-After` asks for. If the server asks for an absurd wait, the run gives up instead of waiting.
- **401/403** stops the run as `blocked`. It's never retried and never worked around.
- **During `heal` and `promote`**, a 401/403 or a page robots.txt disallows stops them as `blocked` too: exit code 2, a `heal_blocked` alert, the source quarantined and nothing promoted. There is no `--force` past a block (see [blocked](verification.md#blocked)).
- **User-Agent:** set it to name you or your project and a contact address.

## Alerts

With `--notify stdout` (or `log`), specmend reports these events:

- `run_failed`, `run_recovered`, `warning` and `drift`;
- for repairs: `repair_proposed`, `repair_rejected` and `repair_promoted`, and `heal_blocked` when the site refused us while healing;
- `quarantined`, when a source needs a human.

`log` sends them to the `specmend` logger, for whatever handlers your host configures. A notifier that fails is logged and skipped; it never breaks a run. To add your own channel (a webhook, a chat message), register a notifier under the `specmend.notifiers` entry point group. It's a class with a `notify(event)` method; see `specmend.events`.

## Scheduling

specmend has no scheduler. Run it from cron, a systemd timer or a CI schedule, and use the exit codes:

```cron
# Every day at 06:00; alerts go to the log.
0 6 * * * cd /srv/scrapers && specmend run sources/notices.yaml --out out/notices.jsonl --notify log
```

## Quarantine

When a run fails, the healing ladder gets a chance. If no repair passes verification, a repair couldn't be fully verified (a page wouldn't load, for example), or no provider is configured, the source is **quarantined**:

- `specmend run` refuses it with exit code 3, unless you pass `--force`.
- `specmend check` still works.

Quarantine ends when you promote a repair, roll back, or register a new spec file version.

## From Python

Everything the CLI does is available as a library:

```python
from specmend.fetch import HttpFetcher
from specmend.heal.ladder import run_and_heal
from specmend.heal.providers import make_provider
from specmend.snapshots import FilesystemSnapshotStore
from specmend.spec import load_spec
from specmend.state import State

state = State("specmend.db")
spec = state.specs.register(load_spec("sources/notices.yaml")).spec  # the active version
with HttpFetcher.for_spec(
    spec.fetch, user_agent="acme-bot/1.0 (+mailto:ops@example.org)"
) as fetcher:
    report, healing, rerun = run_and_heal(
        spec,
        state,
        FilesystemSnapshotStore("snapshots"),
        fetcher,
        make_provider("none"),
        on_record=lambda record: print(record.record),
    )
print(report.status, [f.message for f in report.failures])
```

`Fetcher`, `SnapshotStore`, `Strategy`, `RepairProvider` and `Notifier` are `typing.Protocol`s, so you can pass your own implementation of any of them.
