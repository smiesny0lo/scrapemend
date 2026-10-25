"""Phase 6 end to end: detect -> propose -> verify -> promote -> scrape again.

Done when: scrape v1 -> switch to v2 -> detect -> propose -> verify -> promote ->
scrape succeeds; the same for v3. The LLM path runs the real AnthropicProvider
against Claude's answers recorded from a live run (tests/fixtures/recorded).
"""

import os
import random
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from typer.testing import CliRunner

from examples.mutating_site.app import create_app
from examples.mutating_site.chaos import Chaos, parse_modes
from examples.mutating_site.records import generate_notices
from scrapemend.cli import app as cli
from scrapemend.events import Event
from scrapemend.fetch import HttpFetcher, Response, TransientError
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
from scrapemend.spec import SourceSpec, load_patch, load_spec
from scrapemend.spec.patch import SpecPatch
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
    assert world.events.kinds == ["run_failed", "repair_promoted", "run_recovered"]


# --- review attacks --------------------------------------------------------------------------
# Repairs that are right on most records, and that the gate used to accept: the anchors
# missed the records they get wrong. tests/integration/test_verification.py has their
# exact problems, with anchors that all fit the attack.


@pytest.mark.parametrize(
    ("fixture", "field"),
    [
        ("v2-value-fitted-to-one-page.yaml", "value"),  # A3: 102 of 200 wrong
        ("v2-value-drops-millions.yaml", "value"),  # S1: 29 of 200 wrong
        ("v2-status-enumerates-examples.yaml", "status"),  # S2: 39 of 200 empty
        ("v2-location-last-word.yaml", "location"),  # S3b: 12 of 200 wrong
    ],
)
def test_review_attacks_are_quarantined(world: World, fixture: str, field: str) -> None:
    world.layout(2)
    assert world.run().status == "layout_changed"
    outcome = world.heal(PatchProvider(load_patch(BAD / fixture)))
    assert outcome.status == "quarantined"
    [proposal] = outcome.proposals
    assert proposal.status == "rejected"
    assert proposal.report.problems
    assert all(problem.startswith(f"{field} ") for problem in proposal.report.problems)


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
    if args[0] in ("run", "check", "heal", "promote"):
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
