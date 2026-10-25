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
from scrapemend.extract import Extractor
from scrapemend.fetch import HttpFetcher
from scrapemend.heal.anchors import Anchor, AnchorValue, split
from scrapemend.heal.pages import PageSource
from scrapemend.heal.verify import VerificationReport, verify
from scrapemend.pipeline import RunSummary, ValidatedRecord
from scrapemend.runner import run_source
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import CssSpec, SourceSpec, load_patch, load_spec
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
    known: dict[str, dict[str, AnchorValue]]  # the good run's records, by key (their URL)
    baseline: RunSummary
    pages: PageSource
    new_fetcher: Callable[[], HttpFetcher]

    def check(
        self,
        patch: SpecPatch,
        anchors: list[Anchor] | None = None,
        pages: PageSource | None = None,
    ) -> VerificationReport:
        candidate = patch.apply(self.spec, version=2)
        return verify(
            candidate,
            anchors or self.anchors,
            pages or self.pages,
            baseline=self.baseline,
            known=self.known,
        )

    def anchors_for(self, urls: list[str]) -> list[Anchor]:
        """Anchors for chosen pages, kept from the good run as the anchor picker would."""
        good = self.state.runs.last_good(self.spec.id)
        assert good is not None
        stored = {}
        for snapshot_id in self.snapshots.snapshots(self.spec.id, good.run_id):
            snapshot = self.snapshots.get(snapshot_id)
            if snapshot.kind == "detail":
                stored[snapshot.url] = snapshot_id
        extractor = Extractor(self.spec.fields)
        anchors = []
        for url in urls:
            result = extractor.extract(self.snapshots.get(stored[url]).document())
            record = ValidatedRecord(result, (), stored[url])
            anchors.append(Anchor.from_record(record, url, good.run_id))
        return anchors


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
            known=state.anchors.records(spec.id),
            baseline=good.summary,
            pages=PageSource(snapshots, spec.id, failed.run_id, fetcher=fetcher(), spec_version=1),
            new_fetcher=fetcher,
        )


@pytest.fixture(scope="module")
def crawled(broken: Broken) -> PageSource:
    """The pages of a failed run that reached every detail page, as if only those had
    been redesigned: its listing selectors are the correct repair's, its fields still
    v1's. Without a fetcher, so verifying against it reads only what the run stored."""
    repair = load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml")
    listing = repair.model_copy(update={"fields": {}})
    spec = listing.apply(broken.spec, version=broken.spec.version)
    failed = run_source(spec, broken.new_fetcher(), broken.snapshots, broken.state)
    assert (failed.status, failed.summary.items) == ("layout_changed", 200)
    return PageSource(broken.snapshots, broken.spec.id, failed.run_id)


def layout(b: Broken) -> int:
    return int(b.app.state.layout)


def last_word(b: Broken) -> SpecPatch:
    """Review attack 3b: the correct repair, except that location is the last word of
    the title ("Street sweeping - Ashgrove"). That is right for every place but
    Fenwick Vale: 12 of 200 notices."""
    if layout(b) == 3:
        return load_patch(BAD / "v3-location-last-word.yaml")
    correct = load_patch(REPAIRS / "demo-v2.yaml")
    location = FieldPatch.model_validate(
        {"strategies": [{"regex": r"(\w+)$", "within": {"css": "h1.tender__title"}}]}
    )
    return correct.model_copy(update={"fields": correct.fields | {"location": location}})


def test_anchors_were_kept_from_the_good_run(broken: Broken) -> None:
    assert len(broken.anchors) == 10
    assert all(a.url and a.snapshot_id for a in broken.anchors)
    assert len(broken.known) == 200  # and the values of every record
    assert all(broken.known[str(a.url)] == a.values for a in broken.anchors)


def test_correct_repair_is_accepted(broken: Broken) -> None:
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"))
    assert report.accepted, report.problems
    assert report.score == 1.0
    assert (report.anchors_checked, report.sample, report.listing_items) == (10, 10, 20)
    assert report.listing_pages == 3  # the first, and two along the next-page link


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
    # Every problem is about that field (it also reads another field's element).
    assert report.problems[0].startswith(f"{field} reproduces 0/10 anchor values")
    assert all(field in p for p in report.problems), report.problems
    others = {name: check.rate for name, check in report.fields.items() if name != field}
    assert set(others.values()) == {1.0}  # everything else is right: it really is subtle


def test_volatile_field_reading_another_field_is_rejected(broken: Broken) -> None:
    # Review attack 1b: status (volatile) reads the category. All three volatile
    # rules catch it: exact matches, the values status takes, the shared element.
    report = broken.check(load_patch(BAD / f"v{layout(broken)}-status-reads-category.yaml"))
    assert not report.accepted
    assert report.fields["status"].matched == 0
    assert set(report.problems) == {
        f"status reproduces 0/10 anchor values (needs 60% for a volatile field); "
        f"expected {broken.anchors[0].values['status']!r}, "
        f"got {broken.anchors[0].values['category']!r}",
        f"status reads values the last good run never had on 20 of 20 pages "
        f"(e.g. {broken.anchors[0].values['category']!r}; it had: Awarded, Closed, Open)",
        "category and status read the same element on 10 of 10 anchor pages",
    }


def test_next_page_link_must_lead_through_the_listing(broken: Broken) -> None:
    # Review attacks 2a (v2: the first pager link, which on page 2 is "Previous")
    # and 3c (v3: a link hard-coded to page 2). Page 1 has a link in both cases.
    fixture, problem = {
        2: (
            "v2-first-pager-link.yaml",
            "the next-page link on listing page 2 leads to a page that repeats "
            "20 of 20 items already seen",
        ),
        3: ("v3-page-2.yaml", "pagination ends after 2 listing pages; the last good run had 10"),
    }[layout(broken)]
    report = broken.check(load_patch(BAD / fixture))
    assert report.problems == [problem]


def test_container_matching_more_than_items_is_rejected(broken: Broken) -> None:
    if layout(broken) == 2:
        # Review attack 2b: every table row, the header row (which has no link) too.
        patch = load_patch(BAD / "v2-header-row.yaml")
        matched = 21
    else:
        # Every div of the grid: the cards, and the authority line inside each.
        correct = load_patch(REPAIRS / "demo-v3.yaml")
        patch = correct.model_copy(update={"container": CssSpec(css="div.grid div")})
        matched = 40
    report = broken.check(patch)
    assert report.problems == [
        f"only 20 of {matched} items link to a detail page",
        f"only 20 of {matched} items on listing page 2 link to a detail page",
        f"only 20 of {matched} items on listing page 3 link to a detail page",
    ]


def test_a_field_wrong_on_a_few_records_is_rejected_whatever_the_anchors(
    broken: Broken, crawled: PageSource
) -> None:
    # Review attack 3b. Ten anchors drawn from 200 notices miss all 12 Fenwick Vale
    # ones about half the time. The failed run stored every detail page, though, and
    # on those the field reproduces only 188 of the last good run's 200 values.
    patch = last_word(broken)
    urls = sorted(broken.known)
    missed = 0
    for seed in range(10):
        anchors = broken.anchors_for(random.Random(seed).sample(urls, 10))
        missed += all(a.values["location"] != "Fenwick Vale" for a in anchors)
        report = broken.check(patch, anchors, crawled)
        assert not report.accepted, seed
        assert report.run_records == 200
        assert report.run_fields["location"].matched == 188
        assert (
            "location reproduces 188/200 values of the last good run (needs 100%); "
            "expected 'Fenwick Vale', got 'Vale'"
        ) in report.problems
        assert all(p.startswith("location ") for p in report.problems), report.problems
    assert missed == 5  # seeds 0, 1, 2, 6 and 8: no anchor could have caught it


def test_correct_repair_reproduces_every_stored_record(broken: Broken, crawled: PageSource) -> None:
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"), pages=crawled)
    assert report.accepted, report.problems
    assert (report.run_records, report.anchors_checked, report.sample) == (200, 10, 10)
    assert {check.rate for check in report.run_fields.values()} == {1.0}
    assert report.notes == []


def test_without_stored_detail_pages_only_anchors_are_compared(broken: Broken) -> None:
    # The v1 item container matches nothing on the new listing, so the failed run
    # reached no detail pages: it stored only the three anchor pages it probed. A
    # field wrong only on records outside the anchors passes here: see
    # docs/verification.md, "What the gate can and can't catch".
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"))
    assert report.accepted, report.problems
    assert (report.run_records, report.run_fields) == (3, {})
    assert report.notes == [
        "only 3 records on the failed run's stored pages are known from the last good run "
        "(needs 20), so known values were compared on the anchors only"
    ]


def test_unloadable_anchor_pages_are_tolerated(broken: Broken) -> None:
    # A notice taken down since the good run: its page is gone, not wrong.
    gone = broken.anchors[0].model_copy(update={"url": f"{broken.spec.fetch.start}/TN-0000-0000"})
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"))
    tolerant = broken.check(
        load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"), [gone, *broken.anchors[1:]]
    )
    assert report.accepted and tolerant.accepted, tolerant.problems
    assert (report.anchors_checked, tolerant.anchors_checked) == (10, 9)


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
    # Listing pages 2 and 3, 7 anchor pages the failed run hadn't probed, 10 samples.
    assert pages.fetched == 19
    wrong = load_patch(BAD / "v2-wrong-field.yaml").apply(broken.spec, version=2)
    verify(wrong, broken.anchors, pages, baseline=broken.baseline)
    assert pages.fetched == 19  # a second candidate needs no new requests
    stored = [
        broken.snapshots.get(i) for i in broken.snapshots.snapshots(broken.spec.id, failed.run_id)
    ]
    # The run's listing page and diagnosis probes, then the verification pages.
    assert [s.kind for s in stored].count("listing") == 1 + 2
    assert [s.kind for s in stored].count("detail") == 3 + 17
    again = PageSource(broken.snapshots, broken.spec.id, failed.run_id)  # offline now
    assert again.listing_pages()[0].url == f"{broken.spec.fetch.start}"  # still page 1 first
    report = verify(good, broken.anchors, again, baseline=broken.baseline, known=broken.known)
    assert report.accepted, report.problems
    # Like `promote`: the pages the first verification stored count as the run's now.
    assert report.run_records == 20


def test_diagnosis_uses_anchor_pages(broken: Broken) -> None:
    failed = run_source(broken.spec, broken.new_fetcher(), broken.snapshots, broken.state)
    assert failed.diagnosis is not None
    assert failed.diagnosis.pages == [a.url for a in broken.anchors][:3]
