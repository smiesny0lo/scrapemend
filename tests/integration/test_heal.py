"""Phase 6 end to end: detect -> propose -> verify -> promote -> scrape again.

Done when: scrape v1 -> switch to v2 -> detect -> propose -> verify -> promote ->
scrape succeeds; the same for v3. The LLM path runs the real AnthropicProvider
against Claude's answers recorded from a live run (tests/fixtures/recorded).
"""

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
from scrapemend.fetch import HttpFetcher
from scrapemend.heal.context import RepairContext
from scrapemend.heal.ladder import heal, promote, rollback, run_and_heal
from scrapemend.heal.providers import NoneProvider, PatchProvider
from scrapemend.heal.providers.anthropic import AnthropicProvider
from scrapemend.runner import run_source
from scrapemend.runs import RunReport
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, load_patch, load_spec
from scrapemend.spec.patch import SpecPatch
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
def test_claude_heals_the_redesign_end_to_end(world: World, layout: int) -> None:
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

    version = promote(world.state, proposal)
    assert version.version == 2 and version.origin == "repair"
    titles: dict[str, str] = {}
    healed = world.run(on_record=lambda r: titles.__setitem__(str(r.url), str(r.record["title"])))
    assert (healed.status, healed.spec_version, healed.summary.valid_records) == ("ok", 2, 200)
    assert titles == expected_records(world.site.base_url)
    assert world.events.kinds == ["run_failed", "repair_proposed", "run_recovered"]


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
    promote(world.state, outcome.proposals[0])
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
    promote(world.state, outcome.proposals[0])
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
        promote(world.state, outcome.proposals[0])


# --- the CLI ---------------------------------------------------------------------------------

runner = CliRunner()


def cli_run(world: World, *args: str) -> Any:
    common = ["--db", str(world.db)]
    if args[0] in ("run", "check", "heal"):
        common += [
            "--snapshots",
            str(world.snapshot_dir),
            "--start",
            f"{world.site.base_url}/notices",
        ]
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
    assert "weakest field 100% of anchors, 10 other pages checked" in healed.output

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


def test_cli_errors(world: World, tmp_path: Path) -> None:
    assert cli_run(world, "promote", "99").exit_code == 1
    assert "unknown repair provider" in cli_run(world, "heal", "--provider", "magic").output
    assert "no proposals yet" in cli_run(world, "proposals").output

    edited = tmp_path / "demo.yaml"
    edited.write_text(DEMO_SPEC.read_text("utf-8").replace("rps: 20", "rps: 2"), encoding="utf-8")
    conflict = runner.invoke(cli, ["check", str(edited), "--db", str(world.db)])
    assert conflict.exit_code == 1
    assert "give the edited spec a new version (next free: 2)" in conflict.output
