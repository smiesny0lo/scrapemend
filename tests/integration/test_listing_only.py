"""Listing-only specs end to end: no item.follow, every field read from the listing.

Without detail pages, every item's page URL is its listing page's, so an anchor's
URL only says where the item was listed. Verification has to find each anchor's
row on that page by its key.
"""

import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from examples.mutating_site.app import create_app
from scrapemend.extract import Value
from scrapemend.fetch import HttpFetcher
from scrapemend.heal.context import RepairContext
from scrapemend.heal.ladder import heal, promote
from scrapemend.heal.providers import PatchProvider
from scrapemend.runner import run_source
from scrapemend.runs import RunReport
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, load_patch, load_spec
from scrapemend.spec.patch import SpecPatch
from scrapemend.state import State
from tests.support import FakeClock, serve

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
SPEC = ROOT / "examples" / "sources" / "demo-listing.yaml"
REPAIRS = ROOT / "examples" / "repairs"
BAD = ROOT / "tests" / "fixtures" / "repairs"


@dataclass
class Site:
    app: FastAPI
    base_url: str
    clock: FakeClock


@pytest.fixture(scope="module")
def site() -> Iterator[Site]:
    clock = FakeClock()
    app = create_app(layout=1)
    app.state.chaos.clock = clock
    with serve(app) as base_url:
        yield Site(app, base_url, clock)


@dataclass
class World:
    site: Site
    state: State
    snapshots: FilesystemSnapshotStore

    @property
    def spec(self) -> SourceSpec:
        active = self.state.specs.active("tenders-listing-demo")
        assert active is not None
        data = active.spec.model_dump(mode="json")
        data["fetch"]["start"] = f"{self.site.base_url}/notices"
        return SourceSpec.model_validate(data)

    def fetcher(self) -> HttpFetcher:
        clock = self.site.clock
        return HttpFetcher.for_spec(
            self.spec.fetch, clock=clock, sleep=clock.sleep, rng=random.Random(0)
        )

    def run(self, records: list[dict[str, Value]] | None = None) -> RunReport:
        def keep(record: Any) -> None:
            if records is not None:
                records.append(record.record)

        return run_source(self.spec, self.fetcher(), self.snapshots, self.state, on_record=keep)


class Recording(PatchProvider):
    """Proposes its patch and keeps the context it was shown."""

    def __init__(self, patch: SpecPatch) -> None:
        super().__init__(patch)
        self.contexts: list[RepairContext] = []

    def propose(self, context: RepairContext) -> SpecPatch | None:
        self.contexts.append(context)
        return super().propose(context)


@pytest.fixture
def world(site: Site, tmp_path: Path) -> Iterator[World]:
    """A good run of the listing-only spec on layout v1 behind it."""
    site.app.state.layout = 1
    state = State(tmp_path / "state.db")
    state.specs.register(load_spec(SPEC))
    w = World(site, state, FilesystemSnapshotStore(tmp_path / "snapshots"))
    yield w
    state.close()
    site.app.state.layout = 1


@pytest.mark.parametrize("layout", [2, 3])
def test_listing_only_spec_heals(world: World, layout: int) -> None:
    before: list[dict[str, Value]] = []
    good = world.run(before)
    assert (good.status, good.summary.valid_records, good.summary.listing_pages) == ("ok", 200, 10)

    world.site.app.state.layout = layout
    failed = world.run()
    assert failed.status == "layout_changed"
    assert failed.diagnosis is None  # no detail pages to probe
    provider = Recording(load_patch(REPAIRS / f"demo-listing-v{layout}.yaml"))
    outcome = heal(world.spec, world.state, world.snapshots, world.fetcher(), provider)
    assert outcome.status == "proposed", outcome.message
    [context] = provider.contexts
    assert context.new_listing and (context.old_detail, context.new_detail) == (None, None)
    [proposal] = outcome.proposals
    # Each anchor was found on its listing page by its key, and matched in full.
    assert (proposal.report.anchors_checked, proposal.report.score) == (10, 1.0)
    assert proposal.report.sample == 10  # other items of the first listing page

    promote(world.state, proposal)
    after: list[dict[str, Value]] = []
    healed = world.run(after)
    assert (healed.status, healed.spec_version) == ("ok", 2)
    assert after == before


def test_wrong_listing_only_repair_is_rejected(world: World) -> None:
    world.run()
    world.site.app.state.layout = 2
    world.run()
    wrong = load_patch(BAD / "listing-v2-title-reads-authority.yaml")
    outcome = heal(world.spec, world.state, world.snapshots, world.fetcher(), PatchProvider(wrong))
    assert outcome.status == "quarantined"
    report = outcome.proposals[0].report
    assert report.anchors_checked == 10
    assert report.fields["title"].matched == 0
    # Every other field is right on every anchor: the rows were found, not guessed.
    others = {name: check.rate for name, check in report.fields.items() if name != "title"}
    assert set(others.values()) == {1.0}
