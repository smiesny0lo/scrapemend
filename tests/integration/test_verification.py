"""Phase 5 end to end: the verification gate against the demo site.

Done when: the gate accepts all correct hand-written repairs and rejects all bad
ones (wrong field mapped, empty, overfitted to the anchors a provider was shown).
"""

import random
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI

from examples.mutating_site.app import create_app
from scrapemend.fetch import HttpFetcher
from scrapemend.heal.anchors import Anchor, split
from scrapemend.heal.pages import PageSource
from scrapemend.heal.verify import VerificationReport, verify
from scrapemend.pipeline import RunSummary
from scrapemend.runner import run_source
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, load_patch, load_spec
from scrapemend.spec.patch import FieldPatch, SpecPatch
from scrapemend.state import State
from tests.support import FakeClock, serve

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
REPAIRS = ROOT / "examples" / "repairs"
BAD = ROOT / "tests" / "fixtures" / "repairs"


@dataclass
class Broken:
    """The demo site after a redesign, with a good v1 run behind it."""

    app: FastAPI
    spec: SourceSpec
    state: State
    snapshots: FilesystemSnapshotStore
    clock: FakeClock
    anchors: list[Anchor]
    baseline: RunSummary
    pages: PageSource
    new_fetcher: Callable[[], HttpFetcher]

    def check(self, patch: SpecPatch) -> VerificationReport:
        return verify(
            patch.apply(self.spec, version=2), self.anchors, self.pages, baseline=self.baseline
        )


# One good run and one redesign per layout, shared by the tests of this module.
@pytest.fixture(scope="module", params=[2, 3], ids=["v2", "v3"])
def broken(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Broken]:
    tmp_path = tmp_path_factory.mktemp(f"v{request.param}")
    clock = FakeClock()
    app = create_app(layout=1)
    app.state.chaos.clock = clock
    state = State(tmp_path / "state.db")
    snapshots = FilesystemSnapshotStore(tmp_path / "snapshots")
    with serve(app) as base_url:
        data = load_spec(ROOT / "examples" / "sources" / "demo.yaml").model_dump(mode="json")
        data["fetch"]["start"] = f"{base_url}/notices"
        spec = SourceSpec.model_validate(data)

        def fetcher() -> HttpFetcher:
            return HttpFetcher.for_spec(
                spec.fetch, clock=clock, sleep=clock.sleep, rng=random.Random(0)
            )

        assert run_source(spec, fetcher(), snapshots, state).status == "ok"
        app.state.layout = request.param
        failed = run_source(spec, fetcher(), snapshots, state)
        assert failed.status == "layout_changed"
        good = state.runs.last_good(spec.id)
        assert good is not None
        yield Broken(
            app=app,
            spec=spec,
            state=state,
            snapshots=snapshots,
            clock=clock,
            anchors=state.anchors.all(spec.id),
            baseline=good.summary,
            pages=PageSource(snapshots, spec.id, failed.run_id, fetcher=fetcher(), spec_version=1),
            new_fetcher=fetcher,
        )


def layout(b: Broken) -> int:
    return int(b.app.state.layout)


def test_anchors_were_kept_from_the_good_run(broken: Broken) -> None:
    assert len(broken.anchors) == 10
    assert all(a.url and a.snapshot_id for a in broken.anchors)


def test_correct_repair_is_accepted(broken: Broken) -> None:
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"))
    assert report.accepted, report.problems
    assert report.score == 1.0
    assert (report.anchors_checked, report.sample, report.listing_items) == (10, 10, 20)


def test_unrepaired_spec_is_rejected(broken: Broken) -> None:
    report = verify(broken.spec, broken.anchors, broken.pages, baseline=broken.baseline)
    assert not report.accepted
    assert "the item container matches nothing on the listing page" in report.problems


def test_repair_for_the_other_layout_is_rejected(broken: Broken) -> None:
    other = 3 if layout(broken) == 2 else 2
    assert not broken.check(load_patch(REPAIRS / f"demo-v{other}.yaml")).accepted


def test_wrong_field_mapped_is_rejected(broken: Broken) -> None:
    fixture, field = {
        2: ("v2-wrong-field.yaml", "location"),
        3: ("v3-swapped-dates.yaml", "deadline"),
    }[layout(broken)]
    report = broken.check(load_patch(BAD / fixture))
    assert not report.accepted
    assert report.fields[field].matched == 0
    assert [p.split(" ")[0] for p in report.problems] == [field]
    others = {name: check.rate for name, check in report.fields.items() if name != field}
    assert set(others.values()) == {1.0}  # everything else is right: it really is subtle


def test_empty_repair_is_rejected(broken: Broken) -> None:
    report = broken.check(load_patch(BAD / "v2-empty.yaml"))
    assert not report.accepted
    assert report.listing_items == 0


@pytest.mark.parametrize("field", ["title", "description"])
def test_repair_overfitted_to_the_shown_anchors_is_rejected(broken: Broken, field: str) -> None:
    # The kind of repair an LLM produces when it copies the examples it was shown
    # instead of finding where the value lives: right on those pages, nowhere else.
    shown, held_out = split(broken.anchors, 3)
    memorised = "|".join(re.escape(str(a.values[field])) for a in shown)
    good = load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml")
    fields = dict(good.fields)
    fields[field] = FieldPatch.model_validate({"strategies": [{"regex": f"({memorised})"}]})
    report = broken.check(good.model_copy(update={"fields": fields}))

    assert not report.accepted
    # Perfect on what it was shown, and on held-out anchors only where they happen to
    # share a shown value (titles repeat: "Street sweeping - Ashgrove" can occur twice).
    memorised_values = {a.values[field] for a in shown}
    assert report.fields[field].matched == sum(
        a.values[field] in memorised_values for a in broken.anchors
    )
    assert report.fields[field].matched < report.fields[field].compared
    assert report.fields[field].compared == len(shown) + len(held_out)


def test_verification_reuses_pages_and_stores_the_ones_it_fetched(broken: Broken) -> None:
    failed = run_source(broken.spec, broken.new_fetcher(), broken.snapshots, broken.state)
    pages = PageSource(
        broken.snapshots, broken.spec.id, failed.run_id, fetcher=broken.new_fetcher()
    )
    good = load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml").apply(broken.spec, version=2)
    verify(good, broken.anchors, pages, baseline=broken.baseline)
    assert pages.fetched == 17  # 7 anchor pages the failed run hadn't probed, 10 samples
    wrong = load_patch(BAD / "v2-wrong-field.yaml").apply(broken.spec, version=2)
    verify(wrong, broken.anchors, pages, baseline=broken.baseline)
    assert pages.fetched == 17  # a second candidate needs no new requests
    stored = broken.snapshots.snapshots(broken.spec.id, failed.run_id)
    assert len(stored) == 1 + 3 + 17  # listing, diagnosis probes, verification pages


def test_diagnosis_uses_anchor_pages(broken: Broken) -> None:
    failed = run_source(broken.spec, broken.new_fetcher(), broken.snapshots, broken.state)
    assert failed.diagnosis is not None
    assert failed.diagnosis.pages == [a.url for a in broken.anchors][:3]
