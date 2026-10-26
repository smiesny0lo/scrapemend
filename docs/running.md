# Running scrapemend

## Commands

| Command | What it does |
|---|---|
| `scrapemend validate SPEC` | check a spec file; every problem with its line number |
| `scrapemend run SPEC [--out FILE]` | scrape with the active spec version, write JSON lines, judge the run, heal per `heal.mode` |
| `scrapemend check SPEC [--sample 5]` | scrape a small sample and judge it; writes no records, never changes the baseline, never heals |
| `scrapemend runs [SOURCE_ID]` | run history, newest first |
| `scrapemend accept-drift RUN_ID` | make a drift run the last good run, once you have checked its data (see [drift](#drift)) |
| `scrapemend heal SPEC [--provider P \| --patch FILE]` | repair the latest failed run, verify the repair (with a shadow run over the source), promote it or leave it for review |
| `scrapemend proposals SPEC` | repair proposals with their verification score |
| `scrapemend diff SPEC ID` | what proposal ID would change, as a diff of the spec |
| `scrapemend promote SPEC ID [--force]` | verify proposal ID again and make it the active version. `--force` promotes it when a check couldn't run (never when one failed), and applies a proposal made for another version than the active one to the active version (verified again too) |
| `scrapemend rollback SPEC [--to N]` | make an older version active again |
| `scrapemend versions SPEC` / `show SPEC` | stored versions / one version as YAML |

`python -m scrapemend ...` works the same way, which helps when the `scrapemend` script isn't on your PATH.

Shared options, also available as environment variables (see `.env.example`):

| Option | Variable | Default |
|---|---|---|
| `--db` | `SCRAPEMEND_DB` | `scrapemend.db`: run history, baselines, anchors, spec versions, proposals |
| `--snapshots` | `SCRAPEMEND_SNAPSHOT_DIR` | `snapshots/`: every page downloaded |
| `--user-agent` | `SCRAPEMEND_USER_AGENT` | `scrapemend/<version> (+https://github.com/smiesny0lo/scrapemend)` |
| `--notify` | `SCRAPEMEND_NOTIFY` | none; `stdout`, `log` or a plugin's name |
| `--provider` | `SCRAPEMEND_REPAIR_PROVIDER` | `none`; `anthropic` or a plugin's name |
| `--start` | none | the spec's `fetch.start`; a runtime override that is never stored |

Records go to stdout or `--out` as JSON lines. Decimals are written as exact strings (`"566600.00"`) and dates as ISO strings. Invalid records (a required field missing, a `pattern` not matched) are held back unless you pass `--keep-invalid`. The report goes to stderr.

Everything scrapemend prints, records and reports alike, is UTF-8 on every platform, including Windows pipes and files. On Windows PowerShell 5.1, prefer `--out` to `>`: PowerShell decodes a program's output in the console's code page and re-encodes it as UTF-16, which garbles anything that isn't ASCII.

## What a run reports

Every run is compared with the **last good run**: the latest complete run whose status was `ok`, or a `drift` run you accepted (see [drift](#drift)):

| Status | Signal | Exit code |
|---|---|---|
| `ok` | nothing wrong | 0 |
| `drift` | the data parses, but is distributed unlike the last good run's: far fewer or more records, a field's empty rate moved by more than 5 points, a value of a small set (a status) that 5% of records had is gone, or numbers changed magnitude; alert only, never repaired | 0 |
| `blocked` | 401/403, robots.txt disallows, or a bot-wall page; the run stops and nothing tries to get around it | 2 |
| `transient` | timeouts, 5xx or 429 that outlasted the retries | 2 |
| `layout_changed` | the page structure changed (fingerprints) and extraction fails | 3 |
| `empty` | the item container matched nothing on a page whose structure didn't change | 3 |
| `partial` | some fields or pages fail, but the structure is unchanged | 3 |

Exit code 1 means a usage or spec error. A quarantined source also exits with 3 until it's repaired.

**Naming the broken fields.** When the item container finds nothing, scrapemend re-checks three detail pages it already knows (anchors) to name the broken fields:

```
status: layout_changed
  layout_changed: the item container matches nothing; the page structure changed (detail
  pages 11% similar, listing pages 14% similar to the last good run). On 3 known detail
  pages: broken: title, category, ...; only via fallbacks: reference, authority, ...
    failing: item.container, fetch.paginate.next, title, category, ...
```

**Warnings** don't fail a run but still alert. For example, *fields working only through fallback strategies* means the data is fine, but the page is starting to change.

Only a complete `ok` run of `scrapemend run` updates the baseline: the last good run, its page fingerprints, its anchors and its records. `check` and `--limit` runs never do, and neither does a `drift` run.

### Drift

A `drift` run writes its records and exits with 0, but alerts: its data is distributed unlike the last good run's. That may be the world changing (statuses renamed, a new kind of notice), or a repair that is wrong on some records (see [what the gate can't catch](verification.md#what-it-cant-catch)). scrapemend can't tell which, so a drift run doesn't become the baseline. The runs after it are compared with the same last good run as before, so while the data stays like this they drift too, and alert on every run, until you decide:

```
status: drift
  drift: value: 15% of its values have another number of digits than in the last good run (7 digits: 0%, was 15%)
    failing: value
  the last good run stays the baseline: runs keep drifting until you check this data and accept it with: scrapemend accept-drift 20261026T090000Z-3f9a
```

- If the data is right, accept the run: `scrapemend accept-drift RUN_ID` makes it the last good run, with its page fingerprints, anchors and records. The runs after it are compared with it.
- If it is wrong, fix the spec (roll back, or write a new version) and run again.

Only the latest drift run since the last good run can be accepted: its anchors and records are kept until then. An `ok` run in between becomes the last good run instead, and nothing older can be accepted.

## Politeness

scrapemend is polite by default, and none of this can be turned off from a spec repair:

- **robots.txt** is read once per site and checked before every request, redirect targets included.
  - A missing robots.txt (4xx) means no rules.
  - An unreachable one (5xx) means no crawling.
- **Spacing** of `1 / rate.rps` seconds is kept between requests to the same host.
  - A `Crawl-delay` in robots.txt can only make it slower.
  - Every 429 doubles the spacing for the rest of the run.
- **Retries:** timeouts, connection errors, 408, 429 and 5xx are tried up to 4 times in all, with backoff (1 s, 2 s, 4 s plus jitter) or exactly the wait `Retry-After` asks for. If the server asks for an absurd wait, the run gives up instead of waiting.
- **401/403** stops the run as `blocked`. It's never retried and never worked around.
- **User-Agent:** set it to name you or your project and a contact address.

## Alerts

With `--notify stdout` (or `log`), scrapemend reports these events:

- `run_failed`, `run_recovered`, `warning` and `drift`;
- for repairs: `repair_proposed`, `repair_rejected` and `repair_promoted`;
- `quarantined`, when a source needs a human.

`log` sends them to the `scrapemend` logger, for whatever handlers your host configures. A notifier that fails is logged and skipped; it never breaks a run. To add your own channel (a webhook, a chat message), register a notifier under the `scrapemend.notifiers` entry point group. It's a class with a `notify(event)` method; see `scrapemend.events`.

## Scheduling

scrapemend has no scheduler. Run it from cron, a systemd timer or a CI schedule, and use the exit codes:

```cron
# Every day at 06:00; alerts go to the log.
0 6 * * * cd /srv/scrapers && scrapemend run sources/notices.yaml --out out/notices.jsonl --notify log
```

## Quarantine

When a run fails, the healing ladder gets a chance. If no repair passes verification, a repair couldn't be fully verified (a page wouldn't load, for example), or no provider is configured, the source is **quarantined**:

- `scrapemend run` refuses it with exit code 3, unless you pass `--force`.
- `scrapemend check` still works.

Quarantine ends when you promote a repair, roll back, or register a new spec file version.

## From Python

Everything the CLI does is available as a library:

```python
from scrapemend.fetch import HttpFetcher
from scrapemend.heal.ladder import run_and_heal
from scrapemend.heal.providers import make_provider
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import load_spec
from scrapemend.state import State

state = State("scrapemend.db")
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
