"""Phase 5 end to end: the verification gate against the demo site.

Done when: the gate accepts all correct hand-written repairs and rejects all bad
ones (wrong field mapped, empty, overfitted to the anchors a provider was shown).
"""

import random
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.types import Receive, Scope, Send

from examples.mutating_site.app import create_app
from examples.mutating_site.records import Notice, generate_notices
from specmend.extract import Extractor
from specmend.fetch import HttpFetcher
from specmend.heal.anchors import Anchor, AnchorValue, split
from specmend.heal.pages import PageSource
from specmend.heal.verify import VerificationReport, verify
from specmend.pipeline import RunSummary, ValidatedRecord
from specmend.runner import run_source
from specmend.snapshots import FilesystemSnapshotStore
from specmend.spec import CssSpec, SourceSpec, XPathSpec, load_patch, load_spec
from specmend.spec.patch import FieldPatch, SpecPatch
from specmend.state import State
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


class Switch:
    """A site whose app can be swapped while it is served, at the same address."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


@contextmanager
def redesigned(
    tmp_path: Path,
    layout: int,
    notices: list[Notice] | None = None,
    change: Callable[[dict[str, Any]], None] | None = None,
    after: list[Notice] | None = None,
) -> Iterator[Broken]:
    """The demo site (or one serving `notices`) after a good run on layout v1, then a
    redesign to `layout` (serving `after`, if given) and a failed run. `change` edits
    demo.yaml's data first."""
    clock = FakeClock()
    app = create_app(layout=1, notices=notices)
    app.state.chaos.clock = clock
    site = Switch(app)
    state = State(tmp_path / "state.db")
    snapshots = FilesystemSnapshotStore(tmp_path / "snapshots")
    with serve(site) as base_url:
        data = load_spec(ROOT / "examples" / "sources" / "demo.yaml").model_dump(mode="json")
        data["fetch"]["start"] = f"{base_url}/notices"
        if change is not None:
            change(data)
        spec = SourceSpec.model_validate(data)

        def fetcher() -> HttpFetcher:
            return HttpFetcher.for_spec(
                spec.fetch, clock=clock, sleep=clock.sleep, rng=random.Random(0)
            )

        assert run_source(spec, fetcher(), snapshots, state).status == "ok"
        if after is not None:
            app = site.app = create_app(layout=layout, notices=after)
            app.state.chaos.clock = clock
        app.state.layout = layout
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
        state.close()


# One good run and one redesign per layout, shared by the tests of this module.
@pytest.fixture(scope="module", params=[2, 3], ids=["v2", "v3"])
def broken(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Broken]:
    with redesigned(tmp_path_factory.mktemp(f"v{request.param}"), request.param) as b:
        yield b


def large(data: dict[str, Any]) -> None:
    """demo.yaml for a source of 1200 notices, verified with heal.shadow_max_items'
    default (500): the shadow run samples."""
    data["fetch"]["paginate"]["max_pages"] = 100
    data["expect"]["min_items_per_run"] = 1200
    del data["heal"]["shadow_max_items"]


@pytest.fixture(scope="module")
def big(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Broken]:
    """1200 notices on 60 listing pages, redesigned to layout v2. The first 200 are
    the demo's own."""
    with redesigned(tmp_path_factory.mktemp("big"), 2, generate_notices(count=1200), large) as b:
        yield b


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
    # The shadow run crawled the source and compared every record with the last good run.
    shadow = report.shadow
    assert shadow is not None
    assert (shadow.scope, shadow.listing_pages, shadow.items) == ("full", 10, 200)
    assert (shadow.records, shadow.compared) == (200, 200)
    assert {f.matched for f in shadow.fields.values()} == {200}


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
        if report.shadow is None:  # an anchor had Fenwick Vale: rejected before the crawl
            continue
        assert report.shadow.compared == 200
        assert report.shadow.fields["location"].matched == 188
        assert (
            "location reproduces 188/200 values of the last good run (needs 100%); "
            "expected 'Fenwick Vale', got 'Vale'"
        ) in report.problems
        assert all(p.startswith("location ") for p in report.problems), report.problems
    assert missed == 5  # seeds 0, 1, 2, 6 and 8: no anchor could have caught it


def test_correct_repair_reproduces_every_stored_record(broken: Broken, crawled: PageSource) -> None:
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"), pages=crawled)
    assert report.accepted, report.problems
    assert report.shadow is not None
    assert (report.shadow.compared, report.anchors_checked, report.sample) == (200, 10, 10)
    assert {f.matched for f in report.shadow.fields.values()} == {200}
    assert report.not_run == []  # no check was left out


def test_without_stored_detail_pages_the_shadow_run_fetches_them(broken: Broken) -> None:
    # Replaces test_without_stored_detail_pages_only_anchors_are_compared, which showed
    # the hole: the v1 item container matches nothing on the new listing, so the failed
    # run stored only the three anchor pages it probed, and only the anchors' values
    # were compared. The shadow run fetches the rest and compares every record.
    report = broken.check(load_patch(REPAIRS / f"demo-v{layout(broken)}.yaml"))
    assert report.accepted, report.problems
    assert report.shadow is not None
    assert (report.shadow.records, report.shadow.compared) == (200, 200)
    assert report.not_run == []  # no check was left out


# Review attacks: each is the correct repair with one defect that is right on most
# records. fixture -> (layout, the notices it reads right, the shadow run's problem)
ATTACKS: dict[str, tuple[int, Callable[[Notice], bool], str]] = {
    "v2-value-fitted-to-one-page.yaml": (  # A3
        2,
        lambda n: n.value is None or n.value < 100_000,
        "value changed on 102 of 200 records since the last good run (needs at most 10% "
        "for a volatile field)",
    ),
    "v2-value-drops-millions.yaml": (  # S1
        2,
        lambda n: n.value is None or n.value < 1_000_000,
        "value changed on 29 of 200 records since the last good run (needs at most 10% "
        "for a volatile field)",
    ),
    "v2-status-enumerates-examples.yaml": (  # S2
        2,
        lambda n: n.status != "Awarded",
        "status became empty on 39 of 200 records that had a value in the last good run",
    ),
    "v2-location-last-word.yaml": (  # S3b
        2,
        lambda n: n.location != "Fenwick Vale",
        "location reproduces 188/200 values of the last good run (needs 100%); "
        "expected 'Fenwick Vale', got 'Vale'",
    ),
    "v3-location-last-word.yaml": (  # S3
        3,
        lambda n: n.location != "Fenwick Vale",
        "location reproduces 188/200 values of the last good run (needs 100%); "
        "expected 'Fenwick Vale', got 'Vale'",
    ),
}
NOTICES = {n.id: n for n in generate_notices()}


def test_review_attacks_are_rejected_even_when_every_anchor_fits(broken: Broken) -> None:
    # The worst case for the anchors: ten of them, all records the attack reads right,
    # on the pages a real failure leaves (only three probed detail pages stored).
    for fixture, (attack_layout, fits, problem) in ATTACKS.items():
        if attack_layout != layout(broken):
            continue
        fitting = [url for url in sorted(broken.known) if fits(NOTICES[url.rsplit("/", 1)[1]])]
        report = broken.check(load_patch(BAD / fixture), broken.anchors_for(fitting[:10]))
        assert {check.rate for check in report.fields.values()} == {1.0}, fixture
        assert not report.accepted, fixture
        [found] = report.problems
        assert found.startswith(problem), (fixture, found)


def test_a_field_wrong_on_a_few_records_is_rejected_whatever_the_anchors_or_stored_pages(
    broken: Broken,
) -> None:
    # Review attack S3/S3b, on the pages a real failure leaves: before the shadow run,
    # 5 of these 10 draws (seeds 0, 1, 2, 6 and 8) passed.
    patch = last_word(broken)
    urls = sorted(broken.known)
    for seed in range(10):
        anchors = broken.anchors_for(random.Random(seed).sample(urls, 10))
        report = broken.check(patch, anchors)
        assert not report.accepted, seed
        if any(a.values["location"] == "Fenwick Vale" for a in anchors):
            assert re.match(r"location reproduces \d/10 anchor values", report.problems[0])
        else:
            assert report.problems == [
                "location reproduces 188/200 values of the last good run (needs 100%); "
                "expected 'Fenwick Vale', got 'Vale'"
            ]


def test_review_attacks_on_the_anchors_stay_rejected(broken: Broken) -> None:
    # A1 (v2): published reads the deadline; A2 (v3): description reads another paragraph.
    fixture, field = {
        2: ("v2-published-reads-deadline.yaml", "published"),
        3: ("v3-description-wrong-element.yaml", "description"),
    }[layout(broken)]
    report = broken.check(load_patch(BAD / fixture))
    assert not report.accepted
    assert report.problems[0].startswith(f"{field} reproduces 0/10 anchor values")


def test_a_sampled_shadow_run_reads_every_listing_page_and_a_sample_of_detail_pages(
    broken: Broken,
) -> None:
    # Replaces test_a_sampled_shadow_run_draws_from_the_whole_source, which pinned the
    # listing pages a sample drew at random (5 of 10, by URL): it never followed the
    # candidate's next-page link. Now it reads every listing page along that link, and
    # draws the detail pages it reads from all their items, the same ones every time.
    data = broken.spec.model_dump(mode="json")
    data["heal"]["shadow_max_items"] = 50
    sampled = SourceSpec.model_validate(data)

    def check(fixture: Path, anchors: list[Anchor] = broken.anchors) -> VerificationReport:
        candidate = load_patch(fixture).apply(sampled, version=2)
        return verify(
            candidate, anchors, broken.pages, baseline=broken.baseline, known=broken.known
        )

    report = check(REPAIRS / f"demo-v{layout(broken)}.yaml")
    assert report.accepted, report.problems
    shadow = report.shadow
    assert shadow is not None
    assert (shadow.scope, shadow.listing_pages, shadow.items) == ("sample", 10, 200)
    assert (shadow.drawn, shadow.records, shadow.compared) == (50, 50, 50)
    assert check(REPAIRS / f"demo-v{layout(broken)}.yaml").shadow == shadow
    if layout(broken) == 2:  # S2, with anchors it fits: every Awarded status empty
        _, fits, _ = ATTACKS["v2-status-enumerates-examples.yaml"]
        fitting = [url for url in sorted(broken.known) if fits(NOTICES[url.rsplit("/", 1)[1]])]
        s2 = check(BAD / "v2-status-enumerates-examples.yaml", broken.anchors_for(fitting[:10]))
        [problem] = s2.problems
        assert problem.startswith("status became empty on ")


def test_a_sampled_shadow_run_follows_the_next_page_link_to_its_end(big: Broken) -> None:
    # Review attack B6 on a source of 1200 notices, where the shadow run samples: the
    # next-page link stops at page 10 of 60. A sample drew listing pages from the last
    # good run's, by URL, so it never followed the link: B6 passed, and the next run
    # read 180 of 1200 records.
    report = big.check(load_patch(BAD / "v2-next-stops-at-page-10.yaml"))
    assert report.problems == [
        "the shadow run found 180 items on 9 listing pages, where the last good run found "
        "1200 (needs 90%): the item container or the pagination miss some"
    ]
    correct = big.check(load_patch(REPAIRS / "demo-v2.yaml"))
    assert correct.accepted, correct.problems
    shadow = correct.shadow
    assert shadow is not None
    assert (shadow.scope, shadow.listing_pages, shadow.items) == ("sample", 60, 1200)
    assert (shadow.drawn, shadow.compared) == (500, 500)


B1 = (  # value wrong on the 5 largest amounts of 200: within the 10% it may change by
    "value changed on 5 of the 10 records where it was largest in the last good run, "
    "against 0 of the other 190: a strategy may misread large numbers"
)


def test_a_number_field_wrong_only_on_its_largest_values_is_rejected(broken: Broken) -> None:
    # Review attacks B1 (v2) and B7 (v3): every amount from EUR 2M up loses its
    # millions. They passed the shadow run, which compared all 200 records: 5 changed
    # values are within what a volatile field may change by. They are the five largest.
    report = broken.check(load_patch(BAD / f"v{layout(broken)}-value-misreads-over-2m.yaml"))
    assert report.shadow is not None and report.shadow.fields["value"].changed == 5
    [problem] = report.problems
    assert problem.startswith(B1), problem
    largest = max(n.value for n in NOTICES.values() if n.value is not None)
    assert f"expected '{largest.normalize():f}'" in problem  # the largest amount, misread


def test_a_sample_reads_the_largest_and_smallest_values_of_every_number_field(
    big: Broken,
) -> None:
    # B1 on 1200 notices: about 30 amounts from EUR 2M up, of which a random sample of
    # 500 reads a dozen. It reads the 10 largest and 10 smallest amounts too, always.
    report = big.check(load_patch(BAD / "v2-value-misreads-over-2m.yaml"))
    [problem] = report.problems
    assert problem.startswith("value changed on 10 of the 10 records where it was largest"), problem


UNDISCLOSED = sum(n.value is None for n in NOTICES.values())  # "Not disclosed": 9 of 200


def skips_undisclosed(b: Broken) -> SpecPatch:
    """Review attack B4c: the correct repair, except that the item container only
    matches items with an amount in euros."""
    if layout(b) == 2:
        return load_patch(BAD / "v2-container-skips-undisclosed.yaml")
    cards = "//div[contains(@class, 'grid')]/div[contains(@class, 'rounded-lg')]"
    container = XPathSpec(xpath=f"{cards}[contains(., '€')]")
    return load_patch(REPAIRS / "demo-v3.yaml").model_copy(update={"container": container})


def test_the_shadow_run_must_find_every_kind_of_record(broken: Broken) -> None:
    # Review attack B4c: the item container skips every notice whose value is "Not
    # disclosed". 191 of 200 found is within the 10% the shadow run may miss, so it
    # passed, and the 9 were never read again.
    report = broken.check(skips_undisclosed(broken))
    assert report.shadow is not None and report.shadow.compared == 200 - UNDISCLOSED
    assert report.problems == [
        f"the shadow run found 0 of the {UNDISCLOSED} records of the last good run whose "
        f"value was empty (needs 90%), against {200 - UNDISCLOSED} of the other "
        f"{200 - UNDISCLOSED}: the item container, the detail links or the pagination miss "
        "that kind of record"
    ]


def test_a_sample_must_find_every_kind_of_record(big: Broken) -> None:
    # B4c on 1200 notices: a sample judges it on every listing page's links.
    report = big.check(load_patch(BAD / "v2-container-skips-undisclosed.yaml"))
    [problem] = report.problems
    assert problem.startswith("the shadow run found 0 of the "), problem
    assert "records of the last good run whose value was empty" in problem


def published_since(count: int) -> list[Notice]:
    """The demo's 200 notices and `count` more, published in December 2026, after the
    last good run, with their deadlines in 2027."""
    notices = generate_notices(count=200 + count)
    since = [
        replace(
            n,
            published=date(2026, 12, 10) + timedelta(days=i % 20),
            deadline=date(2027, 1, 5) + timedelta(days=(i * 7) % 50),
        )
        for i, n in enumerate(notices[200:])
    ]
    return notices[:200] + since


@pytest.fixture(scope="module")
def newer(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Broken]:
    """The demo site redesigned to layout v2, with 20 notices published since the good
    run: records the last good run didn't have."""
    with redesigned(tmp_path_factory.mktemp("newer"), 2, after=published_since(20)) as b:
        yield b


def test_a_report_says_which_records_got_only_the_lighter_checks(newer: Broken) -> None:
    # Review attack B2: deadline hard-codes 2026, with a fallback that reads the first
    # date in 2026 on the page. It is right on every record the last good run had, and
    # reads the publication date on the 20 published since, whose deadlines are in
    # 2027. The gate can't catch that (verification.md, what it can't catch): only the
    # required fields and the small sets of values of new records are judged. It passes,
    # and the report says what it didn't judge.
    report = newer.check(load_patch(BAD / "v2-deadline-year-2026.yaml"))
    assert report.accepted, report.problems
    assert report.shadow is not None and (report.shadow.new, report.shadow.compared) == (20, 200)
    evidence = report.evidence()
    assert "  deadline: 200 compared, 0 changed, 0 emptied (stable: none may differ): none" in (
        evidence
    )
    assert (
        "new: 20 records the last good run didn't have: only their required fields, and "
        "fields with a small set of values, were checked"
    ) in evidence


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
    verify(good, broken.anchors, pages, baseline=broken.baseline, known=broken.known)
    # Listing pages 2 to 10, and every detail page but the 3 the failed run had probed.
    assert pages.fetched == 9 + 197
    wrong = load_patch(BAD / "v2-wrong-field.yaml").apply(broken.spec, version=2)
    verify(wrong, broken.anchors, pages, baseline=broken.baseline, known=broken.known)
    assert pages.fetched == 9 + 197  # a second candidate needs no new requests
    stored = [
        broken.snapshots.get(i) for i in broken.snapshots.snapshots(broken.spec.id, failed.run_id)
    ]
    # The run's listing page and diagnosis probes, then the verification pages.
    assert [s.kind for s in stored].count("listing") == 1 + 9
    assert [s.kind for s in stored].count("detail") == 3 + 197
    again = PageSource(broken.snapshots, broken.spec.id, failed.run_id)  # offline now
    assert again.listing_pages()[0].url == f"{broken.spec.fetch.start}"  # still page 1 first
    report = verify(good, broken.anchors, again, baseline=broken.baseline, known=broken.known)
    assert report.accepted, report.problems
    # Like `promote`: the pages the first verification stored are enough for the verdict.
    assert report.shadow is not None and report.shadow.compared == 200


def test_the_same_site_on_two_ports_keeps_the_same_anchors(tmp_path: Path) -> None:
    # Anchors used to be picked by hashing the URL, port included, so the same bug was
    # caught on one deployment and not on another (review attacks S3 and S3b).
    picked = []
    for n in range(2):
        clock = FakeClock()
        app = create_app(layout=1)
        app.state.chaos.clock = clock
        with serve(app) as base_url:
            data = load_spec(ROOT / "examples" / "sources" / "demo.yaml").model_dump(mode="json")
            data["fetch"]["start"] = f"{base_url}/notices"
            spec = SourceSpec.model_validate(data)
            state = State(tmp_path / f"state-{n}.db")
            fetcher = HttpFetcher.for_spec(
                spec.fetch, clock=clock, sleep=clock.sleep, rng=random.Random(0)
            )
            snapshots = FilesystemSnapshotStore(tmp_path / f"snapshots-{n}")
            assert run_source(spec, fetcher, snapshots, state).status == "ok"
            picked.append(
                (base_url, [str(a.url).removeprefix(base_url) for a in state.anchors.all(spec.id)])
            )
            state.close()
    (first_url, first), (second_url, second) = picked
    assert first_url != second_url
    assert first == second and len(first) == 10


def test_diagnosis_uses_anchor_pages(broken: Broken) -> None:
    failed = run_source(broken.spec, broken.new_fetcher(), broken.snapshots, broken.state)
    assert failed.diagnosis is not None
    assert failed.diagnosis.pages == [a.url for a in broken.anchors][:3]
