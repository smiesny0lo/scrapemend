"""Phase 4 end to end: judged runs of the demo spec against the demo site.

Milestone: after a good run on layout v1, a run on v2 reports layout_changed and
names the failing fields; chaos modes come out as transient and blocked.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from examples.mutating_site.app import create_app
from examples.mutating_site.chaos import Chaos, parse_modes
from specmend.cli import app as cli
from specmend.detect.fingerprint import CHANGED_BELOW
from specmend.events import Event
from specmend.fetch import HttpFetcher
from specmend.pipeline import ValidatedRecord
from specmend.runner import run_source
from specmend.runs import RunMode, RunReport, RunStore
from specmend.snapshots import FilesystemSnapshotStore
from specmend.spec import SourceSpec, load_spec
from specmend.state import State
from tests.support import FakeClock, serve

pytestmark = pytest.mark.integration

DEMO_SPEC = Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml"
FIELDS = ["reference", "title", "authority", "category", "location", "published", "deadline"]
FIELDS += ["value", "status", "description"]


class Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        self.events.append(event)


@dataclass
class Demo:
    app: FastAPI
    base_url: str
    clock: FakeClock
    state: State
    snapshots: FilesystemSnapshotStore
    db: Path
    snapshot_dir: Path
    events: Collect

    def spec(self, **changes: Any) -> SourceSpec:
        data = load_spec(DEMO_SPEC).model_dump(mode="json")
        data["fetch"]["start"] = f"{self.base_url}/notices"
        for path, value in changes.items():
            target = data
            *parents, last = path.split("__")
            for part in parents:
                target = target[part]
            target[last] = value
        return SourceSpec.model_validate(data)

    def run(
        self,
        spec: SourceSpec | None = None,
        mode: RunMode = "run",
        limit: int | None = None,
        records: list[ValidatedRecord] | None = None,
    ) -> RunReport:
        spec = spec or self.spec()
        fetcher = HttpFetcher.for_spec(
            spec.fetch, clock=self.clock, sleep=self.clock.sleep, rng=random.Random(0)
        )
        return run_source(
            spec,
            fetcher,
            self.snapshots,
            self.state,
            mode=mode,
            limit=limit,
            on_record=records.append if records is not None else None,
            notifier=self.events,
        )

    @property
    def runs(self) -> RunStore:
        return self.state.runs

    def chaos(self, modes: str, failure_rate: float = 0.2) -> None:
        self.app.state.chaos = Chaos(parse_modes(modes), failure_rate, clock=self.clock)


@pytest.fixture
def demo(tmp_path: Path) -> Iterator[Demo]:
    clock = FakeClock()
    app = create_app(layout=1)
    app.state.chaos.clock = clock
    db = tmp_path / "state.db"
    with serve(app) as base_url:
        yield Demo(
            app=app,
            base_url=base_url,
            clock=clock,
            state=State(db),
            snapshots=FilesystemSnapshotStore(tmp_path / "snapshots"),
            db=db,
            snapshot_dir=tmp_path / "snapshots",
            events=Collect(),
        )


# --- the milestone ----------------------------------------------------------------------


def test_v2_reports_layout_changed_naming_the_failing_fields(demo: Demo) -> None:
    baseline = demo.run()
    assert (baseline.status, baseline.summary.valid_records) == ("ok", 200)

    demo.app.state.layout = 2
    report = demo.run()

    assert report.status == "layout_changed"
    [failure] = report.failures
    assert failure.fields == [
        "item.container",
        "fetch.paginate.next",
        "title",
        "category",
        "location",
        "published",
        "status",
        "description",
    ]
    assert report.diagnosis is not None
    assert report.diagnosis.fallback == ["reference", "authority", "deadline", "value"]
    assert len(report.diagnosis.pages) == 3
    assert report.structure["listing"] < CHANGED_BELOW
    assert report.structure["detail"] < CHANGED_BELOW  # the probed detail pages
    assert [e.kind for e in demo.events.events] == ["run_failed"]
    assert "layout_changed" in demo.events.events[0].message


def test_v3_breaks_even_more_fields(demo: Demo) -> None:
    demo.run()
    demo.app.state.layout = 3
    report = demo.run()
    assert report.status == "layout_changed"
    assert report.diagnosis is not None
    assert report.diagnosis.fallback == ["authority", "value"]
    assert "reference" in report.failures[0].fields and "deadline" in report.failures[0].fields


def test_without_a_good_run_to_compare_to_it_is_only_empty(demo: Demo) -> None:
    # The very first run can't know the page used to look different.
    demo.app.state.layout = 2
    assert demo.run().status == "empty"


def test_a_failed_run_does_not_become_the_baseline(demo: Demo) -> None:
    demo.run()
    first = demo.runs.baseline("tenders-demo", "listing")
    demo.app.state.layout = 2
    demo.run()
    assert demo.runs.baseline("tenders-demo", "listing") == first
    demo.app.state.layout = 1
    assert demo.run().structure["listing"] > 0.85


# --- chaos ----------------------------------------------------------------------------------


def test_blocked(demo: Demo) -> None:
    demo.chaos("blocked")
    report = demo.run()
    assert report.status == "blocked"
    assert "HTTP 403" in report.failures[0].message


def test_dead_server_is_transient(demo: Demo) -> None:
    demo.chaos("flaky", failure_rate=1.0)
    assert demo.run().status == "transient"


def test_rate_limited_run_is_still_good(demo: Demo) -> None:
    demo.chaos("ratelimit")
    report = demo.run()
    assert (report.status, report.summary.valid_records) == ("ok", 200)


def test_recovery_is_announced(demo: Demo) -> None:
    demo.chaos("blocked")
    demo.run()
    demo.chaos("off")
    assert demo.run().status == "ok"
    assert [e.kind for e in demo.events.events] == ["run_failed", "run_recovered"]


# --- other categories -------------------------------------------------------------------------


def test_one_broken_field_is_partial(demo: Demo) -> None:
    demo.run()
    category = load_spec(DEMO_SPEC).fields["category"].model_dump(mode="json")
    category["strategies"] = [{"css": "dd.no-such-class"}]
    report = demo.run(demo.spec(fields__category=category))
    assert report.status == "partial"
    assert report.failures[0].fields == ["category"]
    assert "empty in every record" in report.failures[0].message


def test_container_matching_nothing_on_an_unchanged_page_is_empty(demo: Demo) -> None:
    demo.run()
    report = demo.run(demo.spec(item__container={"css": "tr.no-such-row"}))
    assert report.status == "empty"


def test_pagination_that_stops_is_named(demo: Demo) -> None:
    demo.run()
    spec = demo.spec(fetch__paginate={"next": {"css": "li.gone a"}, "max_pages": 20})
    report = demo.run(spec)
    assert report.status == "partial"
    assert report.failures[0].fields == ["fetch.paginate.next"]
    assert report.summary.listing_pages == 1


def test_invalid_records_are_held_back(demo: Demo) -> None:
    reference = load_spec(DEMO_SPEC).fields["reference"].model_dump(mode="json")
    reference["pattern"] = r"TN-2026-00\d\d"  # only TN-2026-0001..0099 match
    written: list[ValidatedRecord] = []
    report = demo.run(demo.spec(fields__reference=reference), records=written)
    assert report.summary.invalid_records == 101
    assert len(written) == 99 and all(r.valid for r in written)
    assert report.status == "partial"
    assert report.failures[0].fields == ["reference"]


def test_fallbacks_taking_over_warn_without_failing(demo: Demo) -> None:
    demo.run()
    deadline = load_spec(DEMO_SPEC).fields["deadline"].model_dump(mode="json")
    deadline["strategies"] = [{"css": "dd.gone"}, {"label": "Deadline"}]
    report = demo.run(demo.spec(fields__deadline=deadline))
    assert report.status == "ok"
    assert report.warnings == [
        "fields working only through fallback strategies: deadline (200/200)"
    ]
    assert [e.kind for e in demo.events.events] == ["warning"]


# --- check mode -------------------------------------------------------------------------------


def test_check_samples_and_leaves_the_baseline_alone(demo: Demo) -> None:
    demo.run()
    baseline = demo.runs.last_good("tenders-demo")
    demo.app.state.layout = 2
    check = demo.run(mode="check", limit=5)
    assert (check.mode, check.status, check.complete) == ("check", "layout_changed", False)
    demo.app.state.layout = 1
    healthy = demo.run(mode="check", limit=5)
    assert (healthy.status, healthy.summary.items) == ("ok", 5)
    last_good = demo.runs.last_good("tenders-demo")
    assert baseline is not None and last_good is not None
    assert last_good.run_id == baseline.run_id


# --- the CLI ----------------------------------------------------------------------------------

runner = CliRunner()


def cli_args(demo: Demo, *args: str) -> list[str]:
    return [*args, "--db", str(demo.db), "--snapshots", str(demo.snapshot_dir)]


def test_cli_reports_layout_changed_with_exit_code_3(demo: Demo) -> None:
    demo.run()  # the baseline, quickly, with the fake clock
    demo.app.state.layout = 2
    result = runner.invoke(
        cli,
        cli_args(
            demo, "run", str(DEMO_SPEC), "--start", f"{demo.base_url}/notices", "--notify", "stdout"
        ),
    )
    assert result.exit_code == 3, result.output
    assert "status: layout_changed" in result.output
    assert "failing: item.container, fetch.paginate.next, title, category" in result.output
    assert "[ERROR] tenders-demo: run_failed: layout_changed" in result.output


def test_cli_check(demo: Demo) -> None:
    result = runner.invoke(
        cli, cli_args(demo, "check", str(DEMO_SPEC), "--start", f"{demo.base_url}/notices")
    )
    assert result.exit_code == 0, result.output
    assert "1 listing pages, 5 items, 5 records" in result.output
    assert "status: ok" in result.output


def test_cli_blocked_exit_code_2(demo: Demo) -> None:
    demo.chaos("blocked")
    result = runner.invoke(
        cli, cli_args(demo, "check", str(DEMO_SPEC), "--start", f"{demo.base_url}/notices")
    )
    assert result.exit_code == 2
    assert "status: blocked" in result.output


def test_cli_runs_lists_history(demo: Demo) -> None:
    demo.run()
    demo.app.state.layout = 2
    demo.run()
    result = runner.invoke(cli, ["runs", "--db", str(demo.db)])
    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].split()[:6] == ["RUN", "SOURCE", "VER", "MODE", "STATUS", "RECORDS"]
    assert [line.split()[4] for line in lines[1:]] == ["layout_changed", "ok"]


def test_cli_unknown_notifier(demo: Demo) -> None:
    result = runner.invoke(cli, cli_args(demo, "check", str(DEMO_SPEC), "--notify", "pigeon"))
    assert result.exit_code == 1
    assert "unknown notifier 'pigeon'" in result.output


def test_cli_runs_without_history(tmp_path: Path) -> None:
    result = runner.invoke(cli, ["runs", "--db", str(tmp_path / "empty.db")])
    assert (result.exit_code, result.output.strip()) == (0, "no runs yet")


def test_every_field_is_listed(demo: Demo) -> None:
    assert list(demo.spec().fields) == FIELDS
