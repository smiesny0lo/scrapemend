"""Phase 6 end to end: detect -> propose -> verify -> promote -> scrape again.

Done when: scrape v1 -> switch to v2 -> detect -> propose -> verify -> promote ->
scrape succeeds; the same for v3. The LLM path runs the real AnthropicProvider
against Claude's answers recorded from a live run (tests/fixtures/recorded).
"""

import os
import random
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from examples.mutating_site import app as demo_site
from examples.mutating_site.app import create_app
from examples.mutating_site.chaos import Chaos, parse_modes
from examples.mutating_site.records import generate_notices
from scrapemend.cli import app as cli
from scrapemend.events import Event
from scrapemend.fetch import HttpFetcher, Requests, Response, TransientError
from scrapemend.heal import ladder
from scrapemend.heal import verify as verification
from scrapemend.heal.context import RepairContext
from scrapemend.heal.ladder import PromotionRefused, heal, promote, rollback, run_and_heal
from scrapemend.heal.pages import PageSource
from scrapemend.heal.proposals import Proposal
from scrapemend.heal.providers import NoneProvider, PatchProvider
from scrapemend.heal.providers.anthropic import AnthropicProvider
from scrapemend.heal.verify import Check, VerificationReport
from scrapemend.runner import run_source
from scrapemend.runs import RunReport
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import RegexSpec, SourceSpec, load_patch, load_spec
from scrapemend.spec.patch import FieldPatch, SpecPatch
from scrapemend.spec.store import SpecVersion
from scrapemend.state import State
from tests.support import FakeClock, RecordedClaude, serve

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
DEMO_SPEC = ROOT / "examples" / "sources" / "demo.yaml"
REPAIRS = ROOT / "examples" / "repairs"
BAD = ROOT / "tests" / "fixtures" / "repairs"
NOTICES = generate_notices()


class Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def notify(self, event: Event) -> None:
        self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


@dataclass
class Site:
    app: FastAPI
    base_url: str
    clock: FakeClock
    baseline_dir: Path


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Site]:
    """The demo site, and one good run on layout v1 for every test to start from."""
    clock = FakeClock()
    app = create_app(layout=1)
    app.state.chaos.clock = clock
    baseline_dir = tmp_path_factory.mktemp("baseline")
    with serve(app) as base_url:
        s = Site(app, base_url, clock, baseline_dir)
        state = State(baseline_dir / "state.db")
        spec = rebased(s, state.specs.register(file_spec()).spec)
        report = run_source(
            spec, fetcher(s, spec), FilesystemSnapshotStore(baseline_dir / "snapshots"), state
        )
        assert report.status == "ok"
        state.close()
        yield s


def file_spec(mode: str = "propose", version: int = 1) -> SourceSpec:
    """The spec as stored: demo.yaml, possibly with another heal.mode and version."""
    data = load_spec(DEMO_SPEC).model_dump(mode="json")
    data["heal"]["mode"] = mode
    data["version"] = version
    return SourceSpec.model_validate(data)


def rebased(site: Site, spec: SourceSpec) -> SourceSpec:
    """A stored spec pointed at the test server, like `--start` does at runtime."""
    data = spec.model_dump(mode="json")
    data["fetch"]["start"] = f"{site.base_url}/notices"
    return SourceSpec.model_validate(data)


def fetcher(site: Site, spec: SourceSpec) -> HttpFetcher:
    return HttpFetcher.for_spec(
        spec.fetch, clock=site.clock, sleep=site.clock.sleep, rng=random.Random(0)
    )


@dataclass
class World:
    site: Site
    state: State
    snapshots: FilesystemSnapshotStore
    db: Path
    snapshot_dir: Path
    events: Collect

    @property
    def spec(self) -> SourceSpec:
        active = self.state.specs.active("tenders-demo")
        assert active is not None
        return self.rebase(active.spec)

    def rebase(self, spec: SourceSpec) -> SourceSpec:
        return rebased(self.site, spec)

    def layout(self, n: int) -> None:
        self.site.app.state.layout = n

    def run(self, spec: SourceSpec | None = None, **kwargs: Any) -> RunReport:
        spec = spec or self.spec
        return run_source(
            spec,
            fetcher(self.site, spec),
            self.snapshots,
            self.state,
            notifier=self.events,
            **kwargs,
        )

    def promote(self, proposal: Proposal, force: bool = False) -> SpecVersion:
        """Promote a proposal, verifying it again on the failed run's pages."""
        pages = PageSource(
            self.snapshots,
            proposal.source_id,
            proposal.run_id,
            fetcher=fetcher(self.site, self.spec),
        )
        return promote(self.state, proposal, pages, force=force)

    def heal(self, provider: Any, spec: SourceSpec | None = None, **kwargs: Any) -> Any:
        spec = spec or self.spec
        return heal(
            spec,
            self.state,
            self.snapshots,
            fetcher(self.site, spec),
            provider,
            notifier=self.events,
            **kwargs,
        )


@pytest.fixture
def world(site: Site, tmp_path: Path) -> Iterator[World]:
    site.app.state.layout = 1
    site.app.state.chaos = Chaos(clock=site.clock)
    shutil.copy(site.baseline_dir / "state.db", tmp_path / "state.db")
    shutil.copytree(site.baseline_dir / "snapshots", tmp_path / "snapshots")
    state = State(tmp_path / "state.db")
    yield World(
        site=site,
        state=state,
        snapshots=FilesystemSnapshotStore(tmp_path / "snapshots"),
        db=tmp_path / "state.db",
        snapshot_dir=tmp_path / "snapshots",
        events=Collect(),
    )
    state.close()


def expected_records(base_url: str) -> dict[str, str]:
    return {f"{base_url}/notices/{n.id}": n.title for n in NOTICES}


# --- the milestone -------------------------------------------------------------------------


@pytest.mark.parametrize("layout", [2, 3])
def test_recorded_claude_answers_heal_the_redesign(world: World, layout: int) -> None:
    """The real AnthropicProvider, request and all, answered by Claude's structured
    answers recorded from a live run: no API call. It shows that those answers pass
    today's gate and heal the site, not that Claude would answer the same again;
    test_live_claude_heals_the_redesign checks that, when asked to."""
    world.layout(layout)
    failed = world.run()
    assert failed.status == "layout_changed"

    claude = RecordedClaude(f"anthropic-demo-v{layout}")
    outcome = world.heal(AnthropicProvider(client=claude))
    assert outcome.status == "proposed"  # demo.yaml has heal.mode: propose
    [proposal] = outcome.proposals
    assert proposal.report.accepted and proposal.report.score == 1.0

    # What Claude was shown: the context, three of the ten anchors, the new pages.
    sent = claude.requests[0]["messages"][0]["content"][0]["text"]
    assert sent.count("- url: ") == 3
    assert ("tender-table" if layout == 2 else "rounded-lg") in sent

    version = world.promote(proposal)
    assert version.version == 2 and version.origin == "repair"
    titles: dict[str, str] = {}
    healed = world.run(on_record=lambda r: titles.__setitem__(str(r.url), str(r.record["title"])))
    assert (healed.status, healed.spec_version, healed.summary.valid_records) == ("ok", 2, 200)
    assert titles == expected_records(world.site.base_url)
    assert world.events.kinds == ["run_failed", "repair_proposed", "run_recovered"]


@pytest.mark.skipif(
    os.environ.get("SCRAPEMEND_LIVE_ANTHROPIC") != "1",
    reason="calls the Anthropic API: set SCRAPEMEND_LIVE_ANTHROPIC=1 and ANTHROPIC_API_KEY",
)
@pytest.mark.parametrize("layout", [2, 3])
def test_live_claude_heals_the_redesign(world: World, layout: int) -> None:
    """Claude, asked now, repairs the redesign through the gate (one or two API calls)."""
    world.layout(layout)
    assert world.run().status == "layout_changed"
    outcome = world.heal(AnthropicProvider())
    assert outcome.status == "proposed", outcome.message
    accepted = next(p for p in outcome.proposals if p.status == "accepted")
    world.promote(accepted)
    titles: dict[str, str] = {}
    healed = world.run(on_record=lambda r: titles.__setitem__(str(r.url), str(r.record["title"])))
    assert (healed.status, healed.summary.valid_records) == ("ok", 200)
    assert titles == expected_records(world.site.base_url)


def test_auto_mode_verifies_again_exactly_like_promote(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Auto mode used to verify a repair again on the PageSource `heal` built, whose
    # list of detail pages was fixed when it was made, so it saw 3 known records where
    # `scrapemend promote` saw 20. The same proposal, promoted both ways, must get the
    # same report.
    world.layout(2)
    world.run()
    twin_dir = tmp_path / "twin"  # the same state, for the promotion by hand
    twin_dir.mkdir()
    shutil.copy(world.db, twin_dir / "state.db")
    shutil.copytree(world.snapshot_dir, twin_dir / "snapshots")
    patch = load_patch(REPAIRS / "demo-v2.yaml")

    built: list[PageSource] = []

    class Built(PageSource):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(ladder, "PageSource", Built)
    auto = world.heal(PatchProvider(patch), mode="auto")
    monkeypatch.undo()
    assert auto.status == "promoted"
    # Pages to verify the repair on, then fresh ones to promote it, like `promote`'s:
    # by then every page it needs is stored.
    assert len(built) == 2 and built[1].fetched == 0

    twin = State(twin_dir / "state.db")
    try:
        snapshots = FilesystemSnapshotStore(twin_dir / "snapshots")
        active = twin.specs.active("tenders-demo")
        assert active is not None
        spec = world.rebase(active.spec)
        manual = heal(spec, twin, snapshots, fetcher(world.site, spec), PatchProvider(patch))
        assert manual.status == "proposed" and manual.run_id is not None
        pages = PageSource(
            snapshots, "tenders-demo", manual.run_id, fetcher=fetcher(world.site, spec)
        )
        promote(twin, manual.proposals[0], pages)
        by_auto = world.state.proposals.get(auto.proposals[0].id)
        by_hand = twin.proposals.get(manual.proposals[0].id)
        assert by_auto is not None and by_hand is not None
        assert (by_auto.status, by_hand.status) == ("promoted", "promoted")
        assert by_auto.report == by_hand.report
        assert by_auto.report.shadow is not None and by_auto.report.shadow.compared == 200
    finally:
        twin.close()


def test_auto_mode_refuses_what_promote_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If verifying again for the promotion doesn't pass, nothing is promoted: as if a
    # page stopped loading in between.
    real = verification.verify
    reports: list[VerificationReport] = []

    def verify(*args: Any, **kwargs: Any) -> VerificationReport:
        report = real(*args, **kwargs)
        reports.append(report)
        if len(reports) == 1:
            return report
        gone = Check(name="shadow run", status="not run", reason="a page did not load")
        return report.model_copy(update={"checks": [*report.checks[:-1], gone]})

    monkeypatch.setattr(ladder, "verify", verify)
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")), mode="auto")
    assert outcome.status == "quarantined"
    assert outcome.message == (
        "proposal 1 passed verification, but not again to be promoted: proposal 1 could not "
        "be verified again: shadow run: a page did not load; pass --force to promote it "
        "anyway (never past a check that failed)"
    )
    stored = world.state.proposals.get(1)
    assert stored is not None and stored.status == "unverified"
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


@pytest.mark.parametrize("layout", [2, 3])
def test_auto_mode_heals_within_one_run(world: World, layout: int) -> None:
    world.layout(layout)
    world.state.specs.register(file_spec(mode="auto", version=2))
    spec = world.spec
    provider = PatchProvider(load_patch(REPAIRS / f"demo-v{layout}.yaml"))
    first, outcome, second = run_and_heal(
        spec, world.state, world.snapshots, fetcher(world.site, spec), provider,
        notifier=world.events, rebase=world.rebase,
    )  # fmt: skip
    assert first.status == "layout_changed"
    assert outcome is not None and outcome.status == "promoted" and outcome.version == 3
    assert second is not None and (second.status, second.summary.valid_records) == ("ok", 200)
    # The run on the version auto mode promoted warns that it is provisional.
    assert world.events.kinds == ["run_failed", "repair_promoted", "run_recovered", "warning"]


def test_one_spec_heals_through_both_redesigns(world: World) -> None:
    # v1 -> layout 2 -> v2 -> layout 3 -> v3: one lineage, each repair verified against
    # the good run of the version before it, and every scrape the same 200 records.
    def scrape() -> tuple[RunReport, dict[str, Any]]:
        records: dict[str, Any] = {}
        report = world.run(on_record=lambda r: records.__setitem__(str(r.url), r.record))
        return report, records

    _, before = scrape()
    assert len(before) == 200
    for layout in (2, 3):
        world.layout(layout)
        failed, _ = scrape()
        assert failed.status == "layout_changed"
        outcome = world.heal(PatchProvider(load_patch(REPAIRS / f"demo-v{layout}.yaml")))
        [proposal] = outcome.proposals
        assert (outcome.status, proposal.base_version) == ("proposed", layout - 1)
        assert proposal.report.shadow is not None and proposal.report.shadow.compared == 200
        assert world.promote(proposal).version == layout
        healed, after = scrape()
        assert (healed.status, healed.spec_version) == ("ok", layout)
        assert after == before, f"layout {layout}"
    assert [v.origin for v in world.state.specs.history("tenders-demo")] == [
        "file",
        "repair",
        "repair",
    ]


# --- review attacks --------------------------------------------------------------------------
# Repairs that are right on most records, and that the gate used to accept: the anchors
# missed the records they get wrong. Which of them an anchor catches now depends on
# the anchors; tests/integration/test_verification.py has the shadow run catching
# each of them with ten anchors that all fit it.


@pytest.mark.parametrize(
    ("fixture", "problem"),
    [
        (  # A3: 102 of 200 values wrong, 6 of them on anchors
            "v2-value-fitted-to-one-page.yaml",
            "value reproduces 4/10 anchor values (needs 60% for a volatile field); "
            "expected '236700', got '36700'",
        ),
        (  # S1: 29 of 200 wrong, none of them an anchor
            "v2-value-drops-millions.yaml",
            "value changed on 29 of 200 records since the last good run (needs at most 10% "
            "for a volatile field); expected '1136600', got '136600'",
        ),
        (  # S2: 39 of 200 empty, 4 of them anchors: 60% of the anchors still match
            "v2-status-enumerates-examples.yaml",
            "status became empty on 39 of 200 records that had a value in the last good run "
            "(a volatile field may change, but not become empty); expected 'Awarded', got None",
        ),
        (  # S3b: 12 of 200 wrong, 1 of them an anchor
            "v2-location-last-word.yaml",
            "location reproduces 9/10 anchor values (needs 100%); "
            "expected 'Fenwick Vale', got 'Vale'",
        ),
    ],
)
def test_review_attacks_are_quarantined(world: World, fixture: str, problem: str) -> None:
    world.layout(2)
    assert world.run().status == "layout_changed"
    outcome = world.heal(PatchProvider(load_patch(BAD / fixture)))
    assert outcome.status == "quarantined"
    [proposal] = outcome.proposals
    assert proposal.status == "rejected"
    assert proposal.report.problems == [problem]


def test_a_repair_fitted_to_the_examples_is_not_promoted_automatically(world: World) -> None:
    # Review attack A3 under heal.mode auto: it used to become v2 with no human involved.
    world.layout(2)
    world.run()
    outcome = world.heal(
        PatchProvider(load_patch(BAD / "v2-value-fitted-to-one-page.yaml")), mode="auto"
    )
    assert outcome.status == "quarantined"
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


# --- fail closed: a repair that can't be verified is not promoted ---------------------------


def forget_the_good_records(world: World) -> None:
    """Like a database from before the last good run's records were kept."""
    with world.state.db:
        world.state.db.execute("DELETE FROM good_records")


def test_a_repair_that_cannot_be_verified_is_never_promoted_automatically(world: World) -> None:
    world.layout(2)
    world.run()
    forget_the_good_records(world)
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")), mode="auto")
    assert outcome.status == "quarantined"  # auto mode never forces
    [proposal] = outcome.proposals
    assert (proposal.status, proposal.report.verdict, proposal.report.problems) == (
        "unverified",
        "not run",
        [],
    )
    assert outcome.message == (
        "proposal 1 could not be verified: shadow run: the last good run's records are not "
        "stored; review it, then promote it with --force, or run heal again"
    )
    assert world.events.kinds[-1] == "quarantined"
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


def test_only_force_promotes_a_repair_whose_checks_did_not_run(world: World) -> None:
    world.layout(2)
    world.run()
    forget_the_good_records(world)
    [proposal] = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml"))).proposals
    with pytest.raises(PromotionRefused, match="could not be verified again") as refused:
        world.promote(proposal)
    assert refused.value.report is not None and refused.value.report.verdict == "not run"
    version = world.promote(proposal, force=True)
    assert version.version == 2
    assert "forced past checks that did not run: shadow run" in version.note
    assert world.run().status == "ok"


def test_force_never_promotes_a_repair_that_failed_a_check(world: World) -> None:
    world.layout(2)
    world.run()
    forget_the_good_records(world)  # the shadow run can't run, and the anchors fail
    [proposal] = world.heal(PatchProvider(load_patch(BAD / "v2-wrong-field.yaml"))).proposals
    assert (proposal.status, proposal.report.verdict) == ("rejected", "failed")
    with pytest.raises(PromotionRefused, match="rejected, not accepted"):
        world.promote(proposal, force=True)


class Unreachable:
    """A fetcher for which one page keeps failing, as if after every retry."""

    def __init__(self, fetcher: HttpFetcher, url: str) -> None:
        self.fetcher = fetcher
        self.url = url

    def fetch(self, url: str) -> Response:
        if url == self.url:
            raise TransientError(url, "HTTP 503 after 4 attempts")
        return self.fetcher.fetch(url)


def test_a_page_that_did_not_load_is_tried_again_by_promote(world: World) -> None:
    world.layout(2)
    failed = world.run()
    anchored = {a.url for a in world.state.anchors.all("tenders-demo")}
    target = next(
        url
        for url in sorted(world.state.anchors.records("tenders-demo"), reverse=True)
        if url not in anchored
    )
    spec = world.spec
    outcome = heal(
        spec,
        world.state,
        world.snapshots,
        Unreachable(fetcher(world.site, spec), target),
        PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")),
        run_id=failed.run_id,
    )
    assert outcome.status == "quarantined"
    [proposal] = outcome.proposals
    assert proposal.status == "unverified"
    assert (
        f"shadow run: {target} could not be loaded (HTTP 503 after 4 attempts)" in outcome.message
    )
    # The page loads again: promote fetches it, and the repair passes without --force.
    world.promote(proposal)
    promoted = world.state.proposals.get(proposal.id)
    assert promoted is not None and promoted.status == "promoted"
    assert promoted.report.accepted and promoted.report.shadow is not None
    assert promoted.report.shadow.compared == 200


@pytest.mark.parametrize(
    ("fixture", "drift"),
    [
        ("v2-value-fitted-to-one-page.yaml", "value: 53% of its values have another number"),
        ("v2-value-drops-millions.yaml", "value: 15% of its values have another number"),
        ("v2-status-enumerates-examples.yaml", "status empty in 20% of records (was 0%)"),
    ],
)
def test_a_wrong_repair_promoted_anyway_shows_as_drift(
    world: World, fixture: str, drift: str
) -> None:
    # Promoted past the gate, as an older gate let these through: the run after it
    # said ok, with 29 to 102 of 200 values wrong or empty.
    world.layout(2)
    active = world.state.specs.active("tenders-demo")
    assert active is not None
    wrong = load_patch(BAD / fixture).apply(active.spec, version=2)
    world.state.specs.add(wrong, "repair", "promoted by an older gate")
    world.state.specs.activate("tenders-demo", 2)
    report = world.run()
    assert report.status == "drift"
    assert any(f.message.startswith(drift) for f in report.failures), report.failures
    assert world.events.kinds[0] == "drift"


def test_drift_alerts_on_every_run_until_a_human_accepts_it(world: World, tmp_path: Path) -> None:
    # A bad repair forced past the gate. A drift run used to become the baseline, so
    # only the first run after it alerted: the next one compared the wrong values
    # with the wrong values and said ok. Now every run is compared with the last ok
    # run, and drifts, until `scrapemend accept-drift` makes a drift run the baseline.
    good = world.state.runs.last_good("tenders-demo")
    assert good is not None
    world.layout(2)
    active = world.state.specs.active("tenders-demo")
    assert active is not None
    wrong = load_patch(BAD / "v2-value-drops-millions.yaml").apply(active.spec, version=2)
    world.state.specs.add(wrong, "repair", "forced past the gate")
    world.state.specs.activate("tenders-demo", 2)

    drifted = [world.run(), world.run()]
    by_cli = cli_run(world, "run", "--out", str(tmp_path / "out.jsonl"))
    assert by_cli.exit_code == 0, by_cli.output  # drift alerts, but the data is written
    latest = world.state.runs.latest("tenders-demo")
    assert latest is not None
    drifted.append(latest)
    assert [r.status for r in drifted] == ["drift", "drift", "drift"]
    assert f"scrapemend accept-drift {latest.run_id}" in by_cli.output
    alerts = [[e.message for e in world.events.events if e.run_id == r.run_id] for r in drifted[:2]]
    assert alerts[0] and alerts[0] == alerts[1]  # the same alert on every run
    assert set(world.events.kinds) == {"drift"}
    # The last good run, its anchors and its records stay what they were.
    still = world.state.runs.last_good("tenders-demo")
    assert still is not None and still.run_id == good.run_id
    assert {a.run_id for a in world.state.anchors.all("tenders-demo")} == {good.run_id}

    def accept(run_id: str) -> Any:
        return CliRunner().invoke(cli, ["accept-drift", run_id, "--db", str(world.db)])

    older = accept(drifted[0].run_id)
    assert older.exit_code == 1
    assert f"review run {latest.run_id} and accept it" in older.output
    accepted = accept(latest.run_id)
    assert accepted.exit_code == 0, accepted.output
    assert accepted.output.strip() == (
        f"tenders-demo: drift run {latest.run_id} accepted: later runs and repairs are "
        "compared with its 200 records"
    )
    now = world.state.runs.last_good("tenders-demo")
    assert now is not None and (now.run_id, now.spec_version) == (latest.run_id, 2)
    assert {a.run_id for a in world.state.anchors.all("tenders-demo")} == {latest.run_id}
    known = world.state.anchors.records("tenders-demo")
    values = [Decimal(str(r["value"])) for r in known.values() if r["value"] is not None]
    assert len(known) == 200 and max(values) < 1_000_000  # the millions dropped, as accepted

    world.events.events.clear()
    after = world.run()
    assert (after.status, world.events.kinds) == ("ok", [])


# --- provisional versions: what auto mode promotes is checked by a human before it is the
# reference --------------------------------------------------------------------------------


def largest_value(world: World) -> object:
    """The known value of the notice with the largest amount, as runs and repairs are
    compared with."""
    top = max((n for n in NOTICES if n.value is not None), key=lambda n: n.value or 0)
    known = world.state.anchors.records("tenders-demo")
    return known[f"{world.site.base_url}/notices/{top.id}"]["value"]


def test_a_wrong_repair_promoted_automatically_does_not_become_the_reference(
    world: World,
) -> None:
    # Review attack B7: B1 (value wrong on the 5 largest amounts) was promoted in auto
    # mode, and its run became the last good run. At the next redesign the gate
    # compared repairs with B1's values: the correct one "changed" 5 values, and B1's
    # bug on layout v3 matched all 200. Today's gate rejects B1, so it is stored here
    # as auto mode stores what it promotes, as if the gate had let it through.
    good = world.state.runs.last_good("tenders-demo")
    assert good is not None
    right = largest_value(world)
    world.layout(2)
    world.run()
    active = world.state.specs.active("tenders-demo")
    assert active is not None
    b1 = load_patch(BAD / "v2-value-misreads-over-2m.yaml").apply(active.spec, version=2)
    world.state.specs.add(b1, "repair", "promoted automatically", provisional=True)
    world.state.specs.activate("tenders-demo", 2)

    ran = world.run()
    assert (ran.status, ran.provisional) == ("ok", True)  # 5 values wrong, as B1 reads them
    assert any("v2 was promoted automatically" in w for w in ran.warnings), ran.warnings
    # B1's output doesn't become the reference: the last good run, its anchors and its
    # records are still v1's.
    still = world.state.runs.last_good("tenders-demo")
    assert still is not None and still.run_id == good.run_id
    assert {a.run_id for a in world.state.anchors.all("tenders-demo")} == {good.run_id}
    assert largest_value(world) == right

    world.layout(3)
    assert world.run().status == "layout_changed"
    correct = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v3.yaml")))
    [proposal] = correct.proposals
    assert proposal.status == "accepted", proposal.report.problems
    assert proposal.report.shadow is not None
    assert proposal.report.shadow.fields["value"].matched == 200  # against v1's values
    wrong = world.heal(PatchProvider(load_patch(BAD / "v3-value-misreads-over-2m.yaml")))
    [proposal] = wrong.proposals
    assert proposal.status == "rejected"
    assert proposal.report.problems[0].startswith("value changed on 5 of the 10 records")


def test_auto_mode_promotes_a_provisional_version_until_a_human_confirms_it(
    world: World,
) -> None:
    good = world.state.runs.last_good("tenders-demo")
    assert good is not None
    world.layout(2)
    world.state.specs.register(file_spec(mode="auto", version=2))
    spec = world.spec
    provider = PatchProvider(load_patch(REPAIRS / "demo-v2.yaml"))
    _, outcome, second = run_and_heal(
        spec, world.state, world.snapshots, fetcher(world.site, spec), provider,
        notifier=world.events, rebase=world.rebase,
    )  # fmt: skip
    assert outcome is not None and outcome.status == "promoted" and outcome.version == 3
    assert "scrapemend confirm <spec> 3" in outcome.message
    assert world.state.specs.is_provisional("tenders-demo", 3)
    assert second is not None and (second.status, second.provisional) == ("ok", True)
    last = world.state.runs.last_good("tenders-demo")
    assert last is not None and last.run_id == good.run_id  # not the run after it

    versions = cli_run(world, "versions")
    assert "provisional" in versions.output, versions.output
    confirmed = cli_run(world, "confirm", "3")
    assert confirmed.exit_code == 0, confirmed.output
    assert confirmed.output.strip() == (
        f"tenders-demo: v3 confirmed: its run {second.run_id} is the last good run now, "
        "with its 200 records"
    )
    last = world.state.runs.last_good("tenders-demo")
    assert last is not None and last.run_id == second.run_id
    assert {a.run_id for a in world.state.anchors.all("tenders-demo")} == {second.run_id}
    after = world.run()
    assert (after.status, after.provisional, after.warnings) == ("ok", False, [])
    again = cli_run(world, "confirm", "3")
    assert again.exit_code == 1 and "v3 is confirmed already" in again.output


def test_a_version_promoted_by_hand_needs_no_confirming(world: World) -> None:
    world.layout(2)
    world.run()
    [proposal] = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml"))).proposals
    world.promote(proposal)
    assert not world.state.specs.is_provisional("tenders-demo", 2)
    healed = world.run()
    assert (healed.status, healed.provisional) == ("ok", False)
    last = world.state.runs.last_good("tenders-demo")
    assert last is not None and last.run_id == healed.run_id


# --- regexes ---------------------------------------------------------------------------------


def test_a_catastrophic_regex_cannot_hang_heal(world: World) -> None:
    # Review attack R, hand-written, so on Python's re: each search is stopped after a
    # second. Without the limit, heal ran for as long as anyone let it. In a subprocess,
    # so that if the limit ever breaks, this test fails instead of hanging the suite.
    world.layout(2)
    world.run()
    command = [
        sys.executable,
        "-m",
        "scrapemend",
        "heal",
        str(DEMO_SPEC),
        "--patch",
        str(BAD / "v2-redos.yaml"),
        "--start",
        f"{world.site.base_url}/notices",
        "--db",
        str(world.db),
        "--snapshots",
        str(world.snapshot_dir),
    ]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        done = subprocess.run(
            command, capture_output=True, encoding="utf-8", env=environment, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail("heal was still running after 60 s")
    assert done.returncode == 3, done.stderr
    assert "failed   regexes: 2 regexes: 2 on Python's re (at most 1s a search)" in done.stderr
    assert (
        r"problem: description: regex '^(\\w+\\s?)+$' took longer than 1s on 20 pages"
        in done.stderr
    ), done.stderr
    assert "not run  shadow run: skipped: a quicker check failed" in done.stderr


def test_a_providers_regexes_run_on_re2(world: World) -> None:
    # The same repair from a provider: its regexes run on RE2, in linear time. This
    # one never matches the description, and the fallback after it reads it right.
    world.layout(2)
    world.run()
    outcome = world.heal(Scripted(load_patch(BAD / "v2-redos.yaml")))
    [proposal] = outcome.proposals
    regex = proposal.patch.fields["description"].strategies[0]
    assert isinstance(regex, RegexSpec) and regex.engine == "re2"
    assert proposal.report.check("regexes").reason == "2 regexes: 2 on RE2"
    assert proposal.status == "accepted", proposal.report.problems


def test_a_providers_regex_that_re2_cannot_compile_is_rejected(world: World) -> None:
    world.layout(2)
    world.run()
    correct = load_patch(REPAIRS / "demo-v2.yaml")
    status = FieldPatch.model_validate({"strategies": [{"regex": r"Status:(?=\s)\s*(\w+)"}]})
    outcome = world.heal(
        Scripted(correct.model_copy(update={"fields": correct.fields | {"status": status}}))
    )
    assert outcome.status == "quarantined"
    [proposal] = outcome.proposals
    [check] = proposal.report.checks  # nothing else ran
    assert (check.name, check.status) == ("regexes", "failed")
    assert check.problems == [
        r"fields.status.strategies[0]: RE2 can't compile 'Status:(?=\\s)\\s*(\\w+)': "
        "invalid perl operator: (?="
    ]


# --- the ladder's other rungs ---------------------------------------------------------------


class Scripted:
    """A provider that proposes the given patches in turn and keeps the contexts it saw."""

    name = "scripted"

    def __init__(self, *patches: SpecPatch) -> None:
        self.patches = list(patches)
        self.contexts: list[RepairContext] = []

    def propose(self, context: RepairContext) -> SpecPatch | None:
        self.contexts.append(context)
        return self.patches.pop(0) if self.patches else None


def test_rejected_attempt_is_fed_back(world: World) -> None:
    world.layout(2)
    world.run()
    provider = Scripted(
        load_patch(BAD / "v2-wrong-field.yaml"), load_patch(REPAIRS / "demo-v2.yaml")
    )
    outcome = world.heal(provider)
    assert [p.status for p in outcome.proposals] == ["rejected", "accepted"]
    second = provider.contexts[1]
    [attempt] = second.attempts
    assert any("location reproduces 0/10" in problem for problem in attempt.problems)


def test_no_passing_repair_quarantines(world: World) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(BAD / "v2-wrong-field.yaml")))
    assert outcome.status == "quarantined"
    assert "location reproduces" in outcome.message
    state = world.state.specs.state("tenders-demo")
    assert state is not None and state.quarantine_reason == outcome.message
    assert world.events.kinds[-1] == "repair_rejected"


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (NoneProvider(), "proposed nothing"),
        (
            AnthropicProvider(client=RecordedClaude("anthropic-demo-v2", stop_reason="refusal")),
            "declined",
        ),
    ],
    ids=["none", "refusal"],
)
def test_no_proposal_quarantines(world: World, provider: Any, reason: str) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(provider)
    assert outcome.status == "quarantined" and reason in outcome.message
    assert world.events.kinds[-1] == "quarantined"


def test_blocked_is_never_repaired(world: World) -> None:
    world.site.app.state.chaos = Chaos(parse_modes("blocked"), clock=world.site.clock)
    assert world.run().status == "blocked"
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")))
    assert outcome.status == "skipped" and "never works around blocks" in outcome.message
    assert world.state.proposals.for_source("tenders-demo") == []


def test_a_block_during_heal_stops_it(world: World) -> None:
    # The site blocks us after the run: a 403 during heal was a traceback, exit code 1,
    # with no alert and nothing recorded.
    world.layout(2)
    world.run()
    world.site.app.state.chaos = Chaos(parse_modes("blocked"), clock=world.site.clock)
    repair = str(REPAIRS / "demo-v2.yaml")
    healed = cli_run(world, "heal", "--patch", repair, "--notify", "stdout")
    assert healed.exit_code == 2, healed.output
    assert "heal: blocked: the site refused us (HTTP 403: " in healed.output
    assert "[ERROR] tenders-demo: heal_blocked: " in healed.output
    assert "--force" not in healed.output
    state = world.state.specs.state("tenders-demo")
    assert state is not None and (state.quarantine_reason or "").startswith("heal was blocked")
    assert world.state.proposals.for_source("tenders-demo") == []
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


def test_a_robots_disallow_during_heal_stops_it_too(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # robots.txt disallows the listing's later pages, which only heal reads: heal took
    # them for pages that didn't load, and said to promote the repair with --force.
    rules = "User-agent: *\nDisallow: /admin/\nDisallow: /notices?page=\n"
    monkeypatch.setattr(demo_site, "ROBOTS_TXT", rules)
    world.layout(2)
    repair = str(REPAIRS / "demo-v2.yaml")
    ran = cli_run(world, "run", "--patch", repair, "--out", str(tmp_path / "out.jsonl"))
    assert ran.exit_code == 2, ran.output  # the run's own layout_changed would be 3
    assert "status: layout_changed" in ran.output
    assert "heal: blocked: the site refused us (disallowed by robots.txt: " in ran.output
    assert "--force" not in ran.output


def test_a_block_during_promote_stops_it(world: World) -> None:
    world.layout(2)
    failed = world.run()
    down = f"{world.site.base_url}/notices/TN-2026-0091"
    spec = world.spec
    outcome = heal(
        spec, world.state, world.snapshots, Unreachable(fetcher(world.site, spec), down),
        PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")), run_id=failed.run_id,
    )  # fmt: skip
    [proposal] = outcome.proposals
    assert proposal.status == "unverified"
    world.site.app.state.chaos = Chaos(parse_modes("blocked"), clock=world.site.clock)
    for force in ([], ["--force"]):
        refused = cli_run(world, "promote", str(proposal.id), *force)
        assert refused.exit_code == 2, refused.output
        assert "promote: blocked: the site refused us (HTTP 403: " in refused.output
        assert "pass --force" not in refused.output
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


def test_an_outage_during_heal_leaves_the_repair_unverified(world: World) -> None:
    # Every page answers 503 after the run. The correct repair was rejected for
    # "listing page 2 could not be loaded": it couldn't be forced, and a provider's
    # second attempt went to fixing a link that was fine.
    world.layout(2)
    world.run()
    world.site.app.state.chaos = Chaos(
        parse_modes("flaky"), failure_rate=1.0, clock=world.site.clock
    )
    correct = load_patch(REPAIRS / "demo-v2.yaml")
    outcome = world.heal(Scripted(correct, correct))
    assert outcome.status == "quarantined"
    [proposal] = outcome.proposals  # no second attempt: a new patch can't make a page load
    assert (proposal.status, proposal.report.problems) == ("unverified", [])
    assert proposal.report.check("listing").status == "not run"


def test_a_spec_that_does_not_choose_a_heal_mode_never_promotes_by_itself(world: World) -> None:
    # Auto mode puts a repair's output in front of no one: it is chosen in the spec,
    # never had by default. A repair that passes, after a layout change, waits.
    data = file_spec(version=2).model_dump(mode="json", exclude_unset=True)
    data["heal"].pop("mode")
    spec = SourceSpec.model_validate(data)
    assert spec.heal.mode == "propose"
    world.state.specs.register(spec)
    world.layout(2)
    spec = world.spec
    provider = PatchProvider(load_patch(REPAIRS / "demo-v2.yaml"))
    first, outcome, second = run_and_heal(
        spec, world.state, world.snapshots, fetcher(world.site, spec), provider,
        notifier=world.events, rebase=world.rebase,
    )  # fmt: skip
    assert first.status == "layout_changed"
    assert outcome is not None and outcome.status == "proposed", outcome
    assert "heal.mode is propose" in outcome.message
    assert second is None
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 2


def test_partial_failure_is_proposed_not_auto_promoted(world: World) -> None:
    category = load_spec(DEMO_SPEC).fields["category"].model_dump(mode="json")
    category["strategies"] = [{"css": "dd.gone"}]
    data = file_spec(mode="auto", version=2).model_dump(mode="json")
    data["fields"]["category"] = category
    world.state.specs.register(SourceSpec.model_validate(data))
    assert world.run().status == "partial"
    fix = SpecPatch.model_validate(
        {"fields": {"category": {"strategies": [{"css": "dd.category"}]}}}
    )
    outcome = world.heal(Scripted(fix))
    assert outcome.status == "proposed"
    assert "no structural evidence" in outcome.message


def test_low_confidence_is_not_auto_promoted(world: World) -> None:
    world.state.specs.register(file_spec(mode="auto", version=2))
    world.layout(2)
    world.run()
    unsure = load_patch(REPAIRS / "demo-v2.yaml").model_copy(update={"confidence": 0.3})
    outcome = world.heal(Scripted(unsure))
    assert outcome.status == "proposed" and "confidence 0.30" in outcome.message


def test_rollback_restores_the_previous_version(world: World) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")))
    world.promote(outcome.proposals[0])
    assert world.run().status == "ok"
    back = rollback(world.state, "tenders-demo")
    assert back.version == 1
    # The site hasn't changed since the last good run (on v2), but the spec has:
    # the old container matches nothing on an unchanged page.
    assert world.run().status == "empty"


def test_a_failure_of_an_older_version_is_not_repaired_again(world: World) -> None:
    world.layout(2)
    failed = world.run()
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")))
    world.promote(outcome.proposals[0])
    again = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")), run_id=failed.run_id)
    assert again.status == "skipped" and "used v1" in again.message


def test_without_anchors_a_repair_cannot_be_verified(world: World, tmp_path: Path) -> None:
    fresh = State(tmp_path / "fresh.db")
    spec = world.rebase(fresh.specs.register(file_spec()).spec)
    world.layout(2)
    snapshots = FilesystemSnapshotStore(tmp_path / "fresh-snapshots")
    run_source(spec, fetcher(world.site, spec), snapshots, fresh)
    outcome = heal(
        spec,
        fresh,
        snapshots,
        fetcher(world.site, spec),
        PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")),
    )
    assert outcome.status == "quarantined" and "no anchors" in outcome.message


def keeping(world: World, runs: int, version: int = 2) -> None:
    """A new version of the spec file, which keeps the snapshots of `runs` runs."""
    data = file_spec(version=version).model_dump(mode="json")
    data["snapshots"] = {"keep_runs": runs}
    world.state.specs.register(SourceSpec.model_validate(data))


def test_runs_prune_old_snapshots_but_keep_the_pages_promote_needs(world: World) -> None:
    keeping(world, 2)
    [good] = world.snapshots.runs("tenders-demo")  # the last good run's
    world.layout(2)
    failed = world.run()
    [proposal] = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml"))).proposals
    assert proposal.status == "accepted"
    checks = [world.run(mode="check", limit=5) for _ in range(3)]  # each fails, and prunes
    # Each run deleted what no one needs: the first check run. The last good run and
    # the failed run of proposal 1, which can still be promoted, stay.
    assert world.snapshots.runs("tenders-demo") == [
        good,
        failed.run_id,
        checks[1].run_id,
        checks[2].run_id,
    ]
    assert [c.pruned for c in checks] == [[], [], [checks[0].run_id]]
    stored = world.state.runs.get(checks[2].run_id)
    assert stored is not None and stored.pruned == [checks[0].run_id]
    world.promote(proposal)  # verified again on the failed run's pages
    healed = world.run()
    assert healed.status == "ok"  # the new last good run: the old one isn't needed now
    assert healed.pruned == [good, failed.run_id, checks[1].run_id]
    assert world.snapshots.runs("tenders-demo") == [checks[2].run_id, healed.run_id]


def test_only_accepted_proposals_can_be_promoted(world: World) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(BAD / "v2-empty.yaml")))
    with pytest.raises(ValueError, match="rejected, not accepted"):
        world.promote(outcome.proposals[0])


# --- the CLI ---------------------------------------------------------------------------------

runner = CliRunner()


def cli_run(world: World, *args: str) -> Any:
    common = ["--db", str(world.db)]
    if args[0] in ("run", "check", "heal", "promote", "prune"):
        common += ["--snapshots", str(world.snapshot_dir)]
    if args[0] in ("run", "check", "heal"):
        common += ["--start", f"{world.site.base_url}/notices"]
    return runner.invoke(cli, [args[0], str(DEMO_SPEC), *args[1:], *common])


def test_cli_quarantine_heal_diff_promote_rollback(world: World, tmp_path: Path) -> None:
    world.layout(2)

    broken = cli_run(world, "run", "--limit", "5", "--out", str(tmp_path / "a.jsonl"))
    assert broken.exit_code == 3, broken.output
    assert "status: layout_changed" in broken.output
    assert "heal: quarantined: repair provider none proposed nothing" in broken.output

    refused = cli_run(world, "run", "--limit", "5")
    assert refused.exit_code == 3 and "is quarantined" in refused.output

    healed = cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    assert healed.exit_code == 0, healed.output
    assert "heal: proposed: proposal 1 passed verification" in healed.output
    # Every check, with what it covered.
    for line in [
        "passed   regexes: 1 regex: 1 on Python's re (at most 1s a search)",
        "passed   listing: 20 items on the first listing page, 3 listing pages along the "
        "next-page link",
        "passed   anchors: 10 of 10 anchor pages compared; weakest field 100%",
        "passed   sample: 10 other pages",
        "passed   known values: category, location, status on 20 pages",
        "passed   shadow run: the whole source: 10 listing pages, 200 records; all 200 of the "
        "last good run's records compared",
    ]:
        assert line in healed.output, healed.output

    listed = cli_run(world, "proposals")
    assert (
        listed.exit_code == 0 and "accepted" in listed.output and "item.container" in listed.output
    )

    diff = cli_run(world, "diff", "1")
    assert diff.exit_code == 0
    assert "-    css: table.notices tr.notice" in diff.output
    assert "+    css: table.tender-table tr.tender-row" in diff.output

    promoted = cli_run(world, "promote", "1")
    assert promoted.exit_code == 0 and "v2 is now active" in promoted.output

    out = tmp_path / "b.jsonl"
    good = cli_run(world, "run", "--limit", "5", "--out", str(out))
    assert good.exit_code == 0, good.output
    assert "tenders-demo v2, run" in good.output
    assert len(out.read_text(encoding="utf-8").splitlines()) == 5

    versions = cli_run(world, "versions")
    assert "* v2" in versions.output and "repair" in versions.output

    shown = cli_run(world, "show", "--version", "2")
    assert "tender-row" in shown.output

    between = cli_run(world, "diff", "--from", "1", "--to", "2")
    assert "+    css: table.tender-table tr.tender-row" in between.output

    back = cli_run(world, "rollback")
    assert back.exit_code == 0 and "v1 (file) is active again" in back.output


def test_cli_says_what_the_shadow_run_will_read_before_it_starts(
    world: World, tmp_path: Path
) -> None:
    # The failed run stored listing page 1 and 3 probed detail pages; the quick checks
    # added listing pages 2 and 3, and 17 detail pages (anchors and sample).
    world.layout(2)
    repair = ["--patch", str(REPAIRS / "demo-v2.yaml")]
    ran = cli_run(world, "run", "--out", str(tmp_path / "out.jsonl"), *repair)
    assert ran.exit_code == 3, ran.output
    plan = (
        "heal: shadow run: the whole source, all 200 of the last good run's records: about "
        "187 requests (10 listing pages and 200 detail pages, 23 of them stored), at least 9s "
        "at 20 requests a second"
    )
    out = ran.output
    assert out.index("status: layout_changed") < out.index(plan) < out.index("heal: proposed")
    assert out.count("heal: shadow run:") == 1
    # Verified again on the pages heal stored: nothing to fetch.
    promoted = cli_run(world, "promote", "1")
    assert promoted.exit_code == 0, promoted.output
    assert (
        "promote: shadow run: the whole source, all 200 of the last good run's records: every "
        "page it reads is stored, so no requests"
    ) in promoted.output


def test_cli_prune(world: World) -> None:
    [good] = world.snapshots.runs("tenders-demo")
    world.layout(2)
    checks = [world.run(mode="check", limit=5).run_id for _ in range(3)]
    keeping(world, 1)  # fewer than before: three older runs to look at
    header = (
        "tenders-demo: keeps the snapshots of the newest run (snapshots.keep_runs: 1) "
        "and of older runs still needed"
    )
    dry = cli_run(world, "prune", "--dry-run")
    assert dry.exit_code == 0, dry.output
    assert dry.output.splitlines() == [
        header,
        f"  would delete  {checks[0]}",
        f"  would delete  {checks[1]}",
        f"  kept          {good}: the last good run",
    ]
    assert len(world.snapshots.runs("tenders-demo")) == 4

    pruned = cli_run(world, "prune")
    assert pruned.exit_code == 0, pruned.output
    assert pruned.output.splitlines() == [
        header,
        f"  deleted       {checks[0]}",
        f"  deleted       {checks[1]}",
        f"  kept          {good}: the last good run",
    ]
    assert world.snapshots.runs("tenders-demo") == [good, checks[2]]
    # A run prunes too, and says so.
    checked = cli_run(world, "check")
    assert checked.exit_code == 3, checked.output
    assert "  pruned: the snapshots of 1 older run" in checked.output
    again = cli_run(world, "prune")
    assert again.output.splitlines() == [
        header,
        "  nothing to delete",
        f"  kept          {good}: the last good run",
    ]


def requests(output: str) -> list[str]:
    return [line.strip() for line in output.splitlines() if line.strip().startswith("requests:")]


def test_cli_reports_the_requests_of_each_step_on_its_own(world: World, tmp_path: Path) -> None:
    # One fetcher serves the run, the heal and the run after it; each used to report
    # the fetcher's total so far, so a run's line counted the heal's requests too.
    world.state.specs.register(file_spec(mode="auto", version=2))
    world.layout(2)
    repair = ["--patch", str(REPAIRS / "demo-v2.yaml")]
    ran = cli_run(world, "run", "--out", str(tmp_path / "out.jsonl"), *repair)
    assert ran.exit_code == 0, ran.output
    assert "heal: promoted" in ran.output
    assert requests(ran.output) == [
        "requests: 5 (0 retried)",  # robots.txt, listing page 1, 3 probed detail pages
        "requests: 206 (0 retried)",  # the heal: quick checks and shadow run, then promote's
        "requests: 210 (0 retried)",  # the run on v3: 10 listing and 200 detail pages
    ]
    second, first = world.state.runs.recent("tenders-demo", limit=2)
    assert (first.requests, second.requests) == (
        Requests(sent=5, retried=0),
        Requests(sent=210, retried=0),
    )


def test_heal_reports_its_own_requests(world: World) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")))
    assert outcome.requests == Requests(sent=1 + 206, retried=0)  # its own robots.txt
    # Again, on the pages the first heal stored: not even robots.txt is needed.
    healed = cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    assert healed.exit_code == 0, healed.output
    assert "heal: proposed: proposal 2" in healed.output
    assert requests(healed.output) == ["requests: 0 (0 retried)"]


def test_cli_promote_verifies_the_proposal_again(world: World) -> None:
    # A proposal stored as accepted by an earlier, weaker gate: status reading the
    # category (review attack 1b) passed when volatile fields only had to be present.
    world.layout(2)
    failed = world.run()
    old_report = VerificationReport(
        checks=[Check(name="anchors", status="passed", reason="volatile fields were present")]
    )
    old = world.state.proposals.add(
        "tenders-demo",
        1,
        failed.run_id,
        "old-gate",
        load_patch(BAD / "v2-status-reads-category.yaml"),
        old_report,
    )
    refused = cli_run(world, "promote", str(old.id))
    assert refused.exit_code == 3, refused.output
    assert f"proposal {old.id} no longer passes verification" in refused.output
    assert "status reproduces 0/10 anchor values" in refused.output
    stored = world.state.proposals.get(old.id)
    assert stored is not None and stored.status == "rejected"
    assert stored.report.problems and not stored.report.accepted
    active = world.state.specs.active("tenders-demo")
    assert active is not None and active.version == 1


def test_cli_promote_force_past_checks_that_did_not_run(world: World) -> None:
    world.layout(2)
    world.run()
    forget_the_good_records(world)
    healed = cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    assert healed.exit_code == 3, healed.output
    assert "proposal 1 (patch:demo-v2.yaml, confidence 0.90): not run" in healed.output
    assert "not run  shadow run: the last good run's records are not stored" in healed.output

    listed = cli_run(world, "proposals")
    assert "unverified" in listed.output

    refused = cli_run(world, "promote", "1")
    assert refused.exit_code == 3, refused.output
    assert "proposal 1 could not be verified again: shadow run: " in refused.output
    assert "pass --force to promote it anyway" in refused.output

    forced = cli_run(world, "promote", "1", "--force")
    assert forced.exit_code == 0, forced.output
    assert (
        "promoted with --force although these checks did not run: shadow run (the last good "
        "run's records are not stored); v2 is now active"
    ) in forced.output


def evidence(output: str, field: str) -> str:
    """The report's line for one field."""
    return next(line.strip() for line in output.splitlines() if line.strip().startswith(field))


def test_every_report_says_what_each_field_changed(world: World) -> None:
    # B1's report read word for word like the correct repair's: the 5 values it
    # changed were within the allowance, and nothing said so.
    world.layout(2)
    world.run()
    b1 = cli_run(world, "heal", "--patch", str(BAD / "v2-value-misreads-over-2m.yaml"))
    correct = cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    assert (b1.exit_code, correct.exit_code) == (3, 0), b1.output + correct.output
    rule = "(volatile: up to 20 may change, none may become empty)"
    assert (
        evidence(b1.output, "value:")
        == f"value: 200 compared, 5 changed, 0 emptied {rule}: tolerated 5"
    )
    assert (
        evidence(correct.output, "value:")
        == f"value: 200 compared, 0 changed, 0 emptied {rule}: none"
    )
    for output in (b1.output, correct.output):
        assert "fields, on the 200 records of the last good run it compared:" in output
        assert "records: found 200 of the last good run's 200 (at least 180 needed)" in output
    promoted = cli_run(world, "promote", "2")  # verified again: the same evidence
    assert promoted.exit_code == 0, promoted.output
    assert evidence(promoted.output, "value:") == evidence(correct.output, "value:")


def test_promote_force_prints_the_evidence_before_promoting(world: World) -> None:
    world.layout(2)
    world.run()
    forget_the_good_records(world)  # the shadow run can't run: the anchors' fields are shown
    cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    forced = cli_run(world, "promote", "1", "--force")
    assert forced.exit_code == 0, forced.output
    out = forced.output
    shown = out.index("fields, on the 10 anchor pages it compared:")
    assert out.index("not run  shadow run") < shown < out.index("promoted with --force")
    assert "value: 10 compared, 0 changed, 0 emptied" in out


def test_force_sees_what_a_shadow_run_that_stopped_had_compared(world: World) -> None:
    # Review attack S1 (value drops the millions) with a detail page that keeps failing:
    # the shadow run stopped after 29 records, 6 of them with a changed value (21%).
    # The rules can't fail it on so few, so --force may promote it; the evidence it is
    # shown says what was seen, before anything is stored.
    world.layout(2)
    failed = world.run()
    down = f"{world.site.base_url}/notices/TN-2026-0091"  # the 30th in listing order
    spec = world.spec
    outcome = heal(
        spec, world.state, world.snapshots, Unreachable(fetcher(world.site, spec), down),
        PatchProvider(load_patch(BAD / "v2-value-drops-millions.yaml")), run_id=failed.run_id,
    )  # fmt: skip
    [proposal] = outcome.proposals
    assert proposal.status == "unverified"
    pages = PageSource(
        world.snapshots,
        "tenders-demo",
        failed.run_id,
        fetcher=Unreachable(fetcher(world.site, spec), down),
    )
    seen: list[tuple[list[str], int]] = []

    def shown(report: VerificationReport) -> None:
        active = world.state.specs.active("tenders-demo")
        assert active is not None
        seen.append((report.evidence(), active.version))

    promote(world.state, proposal, pages, force=True, on_verified=shown)
    [(lines, version)] = seen
    assert version == 1  # shown before the promotion
    assert lines[0] == (
        "fields, on the 29 records of the last good run it compared before it stopped, of "
        "the 200 it would have:"
    )
    assert (
        "  value: 29 compared, 6 changed, 0 emptied (volatile: up to 20 of 200 may change, "
        "none may become empty): tolerated 6"
    ) in lines
    assert "records: found 29 of the last good run's 200 before it stopped: not judged" in lines


def test_cli_promote_refuses_a_proposal_for_another_version(world: World) -> None:
    world.layout(2)
    world.run()
    healed = cli_run(world, "heal", "--patch", str(REPAIRS / "demo-v2.yaml"))
    assert "proposal 1 passed verification" in healed.output
    world.state.specs.register(file_spec(version=2))  # someone edited the spec meanwhile

    stale = cli_run(world, "promote", "1")
    assert stale.exit_code == 1
    assert "proposal 1 was made for v1, but v2 is active" in stale.output
    assert "--force" in stale.output

    forced = cli_run(world, "promote", "1", "--force")
    assert forced.exit_code == 0, forced.output
    assert "re-based onto v2" in forced.output and "v3 is now active" in forced.output
    assert world.run().status == "ok"


def test_promote_force_fails_if_the_patch_does_not_fit(world: World) -> None:
    world.layout(2)
    world.run()
    outcome = world.heal(PatchProvider(load_patch(REPAIRS / "demo-v2.yaml")))
    data = file_spec(version=2).model_dump(mode="json")
    data["fetch"]["paginate"] = None  # the active version no longer paginates
    world.state.specs.register(SourceSpec.model_validate(data))
    with pytest.raises(PromotionRefused, match="does not fit v2: patch sets next_page"):
        world.promote(outcome.proposals[0], force=True)


def test_cli_errors(world: World, tmp_path: Path) -> None:
    assert cli_run(world, "promote", "99").exit_code == 1
    assert "unknown repair provider" in cli_run(world, "heal", "--provider", "magic").output
    assert "no proposals yet" in cli_run(world, "proposals").output

    edited = tmp_path / "demo.yaml"
    edited.write_text(DEMO_SPEC.read_text("utf-8").replace("rps: 20", "rps: 2"), encoding="utf-8")
    conflict = runner.invoke(cli, ["check", str(edited), "--db", str(world.db)])
    assert conflict.exit_code == 1
    assert "give the edited spec a new version (next free: 2)" in conflict.output
