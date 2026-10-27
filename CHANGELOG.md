# Changelog

All notable changes to specmend are listed here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-27

The first release: a working core, tested against the bundled demo site.

### Added

- **Specs.** Sources are YAML specs, validated by Pydantic: listing and detail pages, and
  fields with typed values, normalizers and ordered fallback strategies (css, xpath,
  label, regex, or a plugin's). The run records which strategy won each field.
  `specmend validate` reports every problem with its line number.
- **Polite fetching.** robots.txt, per-domain rate limits, backoff on 429 and 5xx with
  `Retry-After`, and an identifiable User-Agent. A 401/403, a robots.txt disallow or a
  bot-wall page stops the run as `blocked`. Blocks are never worked around.
- **Snapshots.** Every page a run downloads is stored. The newest runs' snapshots are
  kept (`snapshots.keep_runs`), and so are those a comparison or repair still needs
  (`specmend prune`).
- **Detection.** Every run is compared with the last good one, using record and batch
  checks, page-structure fingerprints, per-field empty rates and the strategies that
  won. Each run is classified as `ok`, `drift`, `blocked`, `transient`,
  `layout_changed`, `empty` or `partial`, and the report names the broken fields. Run
  history is kept in SQLite (`specmend runs`).
- **Repairs.** Repair providers return a structured spec patch, never code: `none`,
  `anthropic` (the `anthropic` extra), a hand-written `--patch`, or a plugin.
  Repairs become new spec versions, never edits in place (`heal`, `proposals`,
  `diff`, `promote`, `rollback`, `versions`, `show`).
- **Verification gate.** Quick checks on anchor records come first. Then a shadow run of
  the repaired spec compares its records with the last good run's by key: stable
  fields must be identical, volatile ones may change within a limit, every kind of
  record must be found, and number fields are judged on their extremes. Regexes from
  providers run on RE2. The gate fails closed: a repair whose checks couldn't run is
  promoted only with `promote --force`, and never past a failed check.
- **Automatic mode.** `heal.mode: auto` promotes a verified repair as a provisional
  version until a human runs `specmend confirm`.
- **Drift.** After a promotion, runs whose data is distributed unlike the last good
  run's are `drift`, and alert until a human runs `specmend accept-drift`.
- **Alerts and plugins.** Alerts go to stdout or a log (`--notify`). Strategies,
  providers and notifiers can be added through the `specmend.strategies`,
  `specmend.providers` and `specmend.notifiers` entry points.
- **Demo.** A demo site with three layouts and failure modes, and a one-command demo:
  `python examples/run_demo.py`.

### Known limitations

What the gate can't catch is listed in
[docs/verification.md](docs/verification.md#what-it-cant-catch), and the open
follow-ups are tracked as GitHub issues.

### Notes

The project was developed as `scrapemend` and renamed to `specmend` before this
release. Nothing was published under the old name.

[0.1.0]: https://github.com/smiesny0lo/specmend/releases/tag/v0.1.0
