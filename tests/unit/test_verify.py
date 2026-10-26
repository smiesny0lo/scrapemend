"""The verification gate's rules, on small hand-built pages."""

from collections.abc import Iterable
from typing import Any

import pytest

from scrapemend.extract import Document, patterns
from scrapemend.heal.anchors import Anchor, rank
from scrapemend.heal.pages import PageFailure
from scrapemend.heal.verify import SHADOW_SEED, ShadowPlan, VerificationReport, verify
from scrapemend.pipeline import FieldStats, ListingPage, RunSummary
from scrapemend.snapshots import SnapshotKind
from scrapemend.spec import SourceSpec
from scrapemend.spec.models import HealSpec, ItemSpec

BASE = "https://example.org"


def spec(**fields: dict[str, Any]) -> SourceSpec:
    default: dict[str, dict[str, Any]] = {
        "title": {"required": True, "strategies": [{"css": "h1"}]},
        "city": {"strategies": [{"css": ".city"}]},
        "price": {"type": "decimal", "volatile": True, "strategies": [{"css": ".price"}]},
    }
    return SourceSpec.model_validate(
        {
            "id": "demo",
            "fetch": {"start": f"{BASE}/list", "paginate": {"next": {"css": "a.next"}}},
            "item": {"container": {"css": "li"}, "follow": {"css": "a"}},
            "fields": default | fields,
        }
    )


def detail(title: str, city: str, price: str | None) -> str:
    price_html = f"<p class='price'>{price}</p>" if price else ""
    return f"<h1>{title}</h1><p class='city'>{city}</p>{price_html}"


def page_set(n: int) -> dict[str, str]:
    """n detail pages with distinct titles and cities."""
    return {f"{BASE}/d/{i}": detail(f"Title {i}", f"City {i % 4}", str(i)) for i in range(n)}


def listing_for(urls: list[str], next_page: str | None = "?page=2") -> str:
    items = "".join(f"<li><a href='{u}'>x</a></li>" for u in urls)
    pager = f"<a class='next' href='{next_page}'>Next</a>" if next_page else ""
    return f"<ul>{items}</ul>{pager}"


# Listing pages 2 and 3, which the next-page link of every page 1 here leads to.
MORE = {
    f"{BASE}/list?page=2": listing_for([f"{BASE}/d/{i}" for i in range(30, 40)], "?page=3"),
    f"{BASE}/list?page=3": listing_for([f"{BASE}/d/{i}" for i in range(40, 50)], None),
}


class FakePages:
    """Pages under the new layout. A page that isn't there answers 404; one listed in
    `unreachable` can't be fetched at all (a timeout, the site failing). `stored`:
    the pages a real PageSource would have at hand without a request."""

    def __init__(
        self,
        details: dict[str, str],
        listing: str | None = None,
        more: dict[str, str] = MORE,
        unreachable: Iterable[str] = (),
        stored: dict[SnapshotKind, set[str]] | None = None,
    ) -> None:
        self.details = details
        self.first = listing
        self.more = more
        self.unreachable = set(unreachable)
        self._stored = stored or {}
        self.requested: list[str] = []  # detail pages, every time one is asked for
        self.listings: list[str] = []  # listing pages, the first time each is asked for
        self._listing_docs: dict[str, Document | None] = {}

    def listing_pages(self) -> list[Document]:
        return [Document.parse(self.first, url=f"{BASE}/list")] if self.first is not None else []

    def listing(self, url: str) -> Document | None:
        if url not in self._listing_docs:
            self.listings.append(url)
            html = None if url in self.unreachable else self.more.get(url)
            self._listing_docs[url] = Document.parse(html, url=url) if html is not None else None
        return self._listing_docs[url]

    def page(self, url: str) -> Document | None:
        self.requested.append(url)
        html = None if url in self.unreachable else self.details.get(url)
        return Document.parse(html, url=url) if html is not None else None

    def stored(self, kind: SnapshotKind) -> set[str]:
        return self._stored.get(kind, set())

    def failure(self, url: str) -> PageFailure | None:
        if url in self.unreachable:
            return PageFailure("HTTP 503 after 4 attempts", unreachable=True)
        if url in self.details or url in self.more:
            return None
        return PageFailure("HTTP 404", unreachable=False)


def anchors_for(details: dict[str, str], urls: list[str]) -> list[Anchor]:
    out = []
    for url in urls:
        i = url.rsplit("/", 1)[1]
        out.append(
            Anchor(
                key=url,
                url=url,
                values={"title": f"Title {i}", "city": f"City {int(i) % 4}", "price": str(i)},
                raw={},
                strategies={},
                snapshot_id=None,
                run_id="good",
            )
        )
    return out


DETAILS = page_set(50)  # the detail pages of every item on listing pages 1 to 3
URLS = list(DETAILS)[:30]  # the items on the first listing page
MORE_URLS = [f"{BASE}/d/{i}" for i in range(30, 60)]  # items on later listing pages
ANCHORS = anchors_for(DETAILS, URLS[:10])
BASELINE = RunSummary(source_id="demo", spec_version=1, run_id="good", listing_pages=5)
KNOWN = {a.key: a.values for a in anchors_for(DETAILS, list(DETAILS))}  # the last good run's


def run(candidate: SourceSpec, pages: FakePages | None = None, **kwargs: Any) -> Any:
    pages = pages or FakePages(DETAILS, listing_for(URLS))
    anchors = kwargs.pop("anchors", ANCHORS)
    baseline = kwargs.pop("baseline", BASELINE)
    known = kwargs.pop("known", KNOWN)
    return verify(candidate, anchors, pages, baseline=baseline, known=known, **kwargs)


def statuses(report: VerificationReport) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


def not_run(report: VerificationReport) -> dict[str, str]:
    return {check.name: check.reason for check in report.checks if check.status == "not run"}


def test_a_working_spec_is_accepted() -> None:
    report = run(spec())
    assert report.accepted, report.problems
    # Every check is listed, with what it covered.
    assert statuses(report) == {
        "regexes": "passed",
        "listing": "passed",
        "anchors": "passed",
        "sample": "passed",
        "known values": "passed",
        "shadow run": "passed",
    }
    assert report.check("shadow run").reason == (
        "the whole source: 3 listing pages, 50 records; "
        "all 50 of the last good run's records compared"
    )
    assert (report.anchors_checked, report.sample, report.listing_items) == (10, 10, 30)
    assert report.score == 1.0
    assert (report.shadow.scope, report.shadow.records, report.shadow.compared) == ("full", 50, 50)


def new_price(html: str) -> str:
    return html.replace("<p class='price'>", "<p class='price'>9")


def test_volatile_field_reading_something_else_is_rejected() -> None:
    # This replaces test_volatile_fields_only_need_to_be_present, which accepted it:
    # a volatile field only had to have a value where it used to, so a strategy
    # reading another element passed at 100%.
    changed = {u: new_price(html) for u, html in DETAILS.items()}
    report = run(spec(), FakePages(changed, listing_for(URLS)))
    assert not report.accepted
    assert report.fields["price"].matched == 0
    assert report.problems[0].startswith(
        "price reproduces 0/10 anchor values (needs 60% for a volatile field)"
    )


def test_volatile_values_that_really_changed_are_accepted() -> None:
    # Two of the ten anchors have a new price since the last good run. Most still
    # match exactly, so a correct repair passes.
    changed = {
        u: new_price(html) if i in (3, 7) else html for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(changed, listing_for(URLS)))
    assert report.accepted, report.problems
    assert report.fields["price"].rate == 0.8


def test_volatile_match_is_configurable() -> None:
    changed = {u: new_price(html) if i < 5 else html for i, (u, html) in enumerate(DETAILS.items())}
    strict = spec().model_copy(update={"heal": HealSpec(volatile_match=0.6)})
    lenient = spec().model_copy(update={"heal": HealSpec(volatile_match=0.5)})
    assert not run(strict, FakePages(changed, listing_for(URLS))).accepted
    assert run(lenient, FakePages(changed, listing_for(URLS))).accepted


def test_two_fields_reading_one_element_are_rejected() -> None:
    # region is volatile, and on seven anchors its known value equals the city, so
    # reading the city element reproduces 7/10: enough for a volatile field. On the
    # other three, the two known values differ, yet both fields read one element.
    regions = [f"City {i % 4}" if i < 7 else "Elsewhere" for i in range(10)]
    anchors = [
        a.model_copy(update={"values": a.values | {"region": region}})
        for a, region in zip(ANCHORS, regions, strict=True)
    ]
    candidate = spec(region={"volatile": True, "strategies": [{"css": ".city"}]})
    report = run(candidate, anchors=anchors)
    assert report.fields["region"].rate == 0.7
    assert report.problems == ["city and region read the same element on 3 of 10 anchor pages"]


def test_a_field_may_read_part_of_an_element_another_field_reads() -> None:
    # "City 1 · North": two fields, one element, different text. That is fine.
    pages = {u: html.replace("</p>", " · North</p>", 1) for u, html in DETAILS.items()}
    anchors = [a.model_copy(update={"values": a.values | {"side": "North"}}) for a in ANCHORS]
    candidate = spec(
        city={"strategies": [{"regex": "^(.+?) · ", "within": {"css": ".city"}}]},
        side={"strategies": [{"regex": " · (.+)$", "within": {"css": ".city"}}]},
    )
    report = run(candidate, FakePages(pages, listing_for(URLS)), anchors=anchors)
    assert report.accepted, report.problems


def status_pages(read: str) -> dict[str, str]:
    """Detail pages with a status (Open or Closed) and a kind of work."""
    return {
        u: html + f"<p class='status'>{'Open' if i % 3 else 'Closed'}</p><p class='kind'>{read}</p>"
        for i, (u, html) in enumerate(DETAILS.items())
    }


def with_statuses(*known: str, filled: int = 30) -> RunSummary:
    stats = FieldStats(filled=filled, domain=list(known))
    return BASELINE.model_copy(update={"fields": {"status": stats}})


def test_a_field_with_few_values_must_keep_reading_them() -> None:
    # status took two values in the last good run. The anchors were kept before it
    # existed, so they hold no status to compare; the gate still sees that the new
    # strategy reads values status never had.
    candidate = spec(status={"volatile": True, "strategies": [{"css": ".kind"}]})
    pages = FakePages(status_pages("Works"), listing_for(URLS))
    report = run(candidate, pages, baseline=with_statuses("Open", "Closed"))
    assert report.problems == [
        "status reads values the last good run never had on 20 of 20 pages "
        "(e.g. 'Works'; it had: Closed, Open)"
    ]


def test_new_values_now_and_then_are_fine() -> None:
    candidate = spec(status={"volatile": True, "strategies": [{"css": ".status"}]})
    pages = FakePages(status_pages("Works"), listing_for(URLS))
    # "Closed" is new since the last good run, on a third of the pages.
    assert run(candidate, pages, baseline=with_statuses("Open")).accepted
    # Too few repeats to call it a small set of values: no check.
    assert run(candidate, pages, baseline=with_statuses("Pending", filled=2)).accepted


def test_volatile_field_gone_is_a_mismatch() -> None:
    gone = {u: detail(f"Title {i}", f"City {i % 4}", None) for i, u in enumerate(URLS)}
    report = run(spec(), FakePages(gone, listing_for(URLS)))
    assert not report.accepted
    assert report.fields["price"].rate == 0.0


def test_one_wrong_field_cannot_hide_behind_correct_ones() -> None:
    report = run(spec(city={"strategies": [{"css": "h1"}]}))
    assert not report.accepted
    assert report.fields["city"].matched == 0
    assert report.fields["title"].rate == 1.0
    assert "city reproduces 0/10 anchor values" in report.problems[0]


@pytest.mark.parametrize(("threshold", "accepted"), [(0.8, True), (0.9, False)])
def test_threshold(threshold: float, accepted: bool) -> None:
    # The city strategy fails on anchor pages 8 and 9: it reproduces 8 of 10 anchors.
    cities = {
        u: html.replace("class='city'", "class='town'") if i in (8, 9) else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    candidate = spec(city={"strategies": [{"css": ".city"}]})
    report = run(candidate, FakePages(cities, listing_for(URLS)), threshold=threshold)
    assert report.fields["city"].rate == 0.8
    assert report.accepted is accepted


def test_stable_fields_must_match_every_anchor_by_default() -> None:
    # One anchor of ten wrong is one record in ten wrong in every run.
    cities = {
        u: html.replace("class='city'", "class='town'") if i == 9 else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(cities, listing_for(URLS)))
    assert report.threshold == 1.0
    assert report.problems == [
        "city reproduces 9/10 anchor values (needs 100%); expected 'City 1', got None"
    ]


# --- the shadow run: the candidate crawls the source ---------------------------------
# These replace the tests of the step that compared only the pages the failed run had
# stored, which a run that never reached the detail pages left at three or four.


def test_the_shadow_run_compares_every_record_of_the_source() -> None:
    # A notice new since the last good run has no values to compare.
    new = f"{BASE}/d/99"
    details = DETAILS | {new: detail("Title 99", "City 3", "99")}
    pages = FakePages(details, listing_for([*URLS, new]))
    report = run(spec(), pages)
    assert report.accepted, report.problems
    shadow = report.shadow
    assert (shadow.scope, shadow.listing_pages, shadow.items) == ("full", 3, 51)
    assert (shadow.records, shadow.known, shadow.compared) == (51, 50, 50)
    assert {name: (f.matched, f.compared) for name, f in shadow.fields.items()} == {
        "title": (50, 50),
        "city": (50, 50),
        "price": (50, 50),
    }
    # Anchors, the sample, then every item along the listing, in order.
    assert pages.requested == [*URLS[:20], *URLS, new, *MORE_URLS[:20]]


@pytest.mark.parametrize("wrong", [25, 45], ids=["first listing page", "third listing page"])
def test_a_field_wrong_where_no_anchor_or_sample_looks_is_rejected(wrong: int) -> None:
    # Page 25 is neither an anchor (pages 0-9) nor in the sample (10-19); page 45 is
    # an item of listing page 3, which only the pagination check had loaded. Both
    # used to pass unless the failed run happened to store their pages.
    cities = {
        u: html.replace("class='city'", "class='town'") if i == wrong else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(cities, listing_for(URLS)))
    assert report.problems == [
        "city reproduces 49/50 values of the last good run (needs 100%); "
        f"expected 'City {wrong % 4}', got None"
    ]


@pytest.mark.parametrize(("changed", "accepted"), [(5, True), (6, False)])
def test_a_volatile_field_may_change_on_few_records(changed: int, accepted: bool) -> None:
    # Replaces test_volatile_fields_must_match_most_stored_records, which let 40% of
    # the stored records differ. New prices past the sample: 5 of 50 (10%) may.
    prices = {
        u: new_price(html) if 20 <= i < 20 + changed else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(prices, listing_for(URLS)))
    assert report.fields["price"].rate == 1.0  # every anchor still matches
    assert report.shadow.fields["price"].changed == changed
    assert report.accepted is accepted
    if not accepted:
        assert report.problems == [
            "price changed on 6 of 50 records since the last good run (needs at most 10% "
            "for a volatile field); expected '20', got '920'"
        ]


def price_reads(pages: dict[str, str], wrong: dict[int, str]) -> dict[str, str]:
    """The pages, with the price of the detail pages numbered in `wrong` replaced."""
    out = dict(pages)
    for i, price in wrong.items():
        out[f"{BASE}/d/{i}"] = detail(f"Title {i}", f"City {i % 4}", price)
    return out


def test_a_number_field_wrong_only_on_its_largest_values_is_rejected() -> None:
    # Review attack B1: a regex that loses the leading digits of the largest amounts.
    # 5 of 100 prices changed is within the 10% a volatile field may change by, but
    # they are the 5 largest: prices that really changed don't pick the largest.
    wrong = price_reads(page_set(100), {i: str(i)[1:] for i in range(95, 100)})
    report, _ = run_pages(spec(), details=wrong)
    assert report.shadow is not None and report.shadow.fields["price"].changed == 5
    assert report.problems == [
        "price changed on 5 of the 10 records where it was largest in the last good run, "
        "against 0 of the other 90: a strategy may misread large numbers; expected '99', "
        "got '9'"
    ]


def test_a_number_field_wrong_only_on_its_smallest_values_is_rejected() -> None:
    wrong = price_reads(page_set(100), {i: f"9{i}" for i in range(5)})
    report, _ = run_pages(spec(), details=wrong)
    assert report.problems == [
        "price changed on 5 of the 10 records where it was smallest in the last good run, "
        "against 0 of the other 90: a strategy may misread small numbers; expected '0', "
        "got '90'"
    ]


def test_changes_that_chance_puts_among_the_largest_values_are_fine() -> None:
    # 5 of 100 prices changed, 2 of them among the 10 largest: 20% of those, but that
    # happens one time in twelve when one price in twenty changed anywhere.
    wrong = price_reads(page_set(100), {i: f"9{i}" for i in (20, 30, 40, 98, 99)})
    report, _ = run_pages(spec(), details=wrong)
    assert report.accepted, report.problems
    assert report.shadow is not None and report.shadow.fields["price"].changed == 5


def test_a_sample_reads_every_value_of_a_field_with_few_values() -> None:
    # Review attack B3's shape: a stable field wrong only on the records of one rare
    # value, here one notice in Fenwick Vale that reads "Vale". A sample of 20 of 100
    # records drew 20 at random; now it reads a record of every value of city too.
    details = page_set(100) | {f"{BASE}/d/55": detail("Title 55", "Vale", "55")}
    known = {a.key: a.values for a in anchors_for(details, list(details))}
    known[f"{BASE}/d/55"] = known[f"{BASE}/d/55"] | {"city": "Fenwick Vale"}
    cities = FieldStats(filled=100, domain=[*(f"City {i}" for i in range(4)), "Fenwick Vale"])
    baseline = BASELINE.model_copy(
        update={"listing_pages": 10, "items": 100, "fields": {"city": cities}}
    )
    report, _ = run_pages(sampling(20), details=details, baseline=baseline, known=known)
    assert report.problems == [
        "city reproduces 20/21 values of the last good run (needs 100%); "
        "expected 'Fenwick Vale', got 'Vale'"
    ]


def test_volatile_change_is_configurable() -> None:
    prices = {
        u: new_price(html) if 20 <= i < 26 else html for i, (u, html) in enumerate(DETAILS.items())
    }
    lenient = spec().model_copy(update={"heal": HealSpec(volatile_change=0.2)})
    assert run(lenient, FakePages(prices, listing_for(URLS))).accepted


def test_a_volatile_value_that_became_empty_is_a_mismatch_not_a_change() -> None:
    # One price of 50 gone is 2%, well within what a volatile field may change by.
    gone = {
        u: detail(f"Title {i}", f"City {i % 4}", None) if i == 25 else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(gone, listing_for(URLS)))
    assert (report.shadow.fields["price"].emptied, report.shadow.fields["price"].changed) == (1, 0)
    assert report.problems == [
        "price became empty on 1 of 50 records that had a value in the last good run "
        "(a volatile field may change, but not become empty); expected '25', got None"
    ]


def test_a_volatile_field_with_few_values_must_keep_reading_them_in_the_shadow_run() -> None:
    # Right on the anchors and the sample (pages 0-19), wrong on the other 30. The last
    # good run's records hold no status to compare exactly.
    details = {
        u: html + f"<p class='status'>{'Open' if i < 20 else 'Works'}</p>"
        for i, (u, html) in enumerate(DETAILS.items())
    }
    candidate = spec(status={"volatile": True, "strategies": [{"css": ".status"}]})
    pages = FakePages(details, listing_for(URLS))
    report = run(candidate, pages, baseline=with_statuses("Open", "Closed"))
    assert report.problems == [
        "status reads values the last good run never had on 30 of 50 records of the shadow "
        "run (e.g. 'Works'; it had: Closed, Open)"
    ]


def test_the_shadow_run_must_find_the_last_good_runs_records() -> None:
    # The detail pages of listing page 3 are gone: 40 of 50 records found. (The last
    # good run had them, and so did every other test here.)
    pages = FakePages(
        {u: h for i, (u, h) in enumerate(DETAILS.items()) if i < 40}, listing_for(URLS)
    )
    report = run(spec(), pages)
    assert report.problems == [
        "the shadow run found 40 of the last good run's 50 records (needs 90%): the item "
        "container, the detail links or the pagination miss some"
    ]
    assert run(spec(), pages, known={k: v for k, v in KNOWN.items() if k in pages.details}).accepted


def listing_without(gone: set[int]) -> tuple[str, dict[str, str]]:
    """The first listing page and MORE, without the items numbered in `gone`."""

    def keep(urls: list[str]) -> list[str]:
        return [u for u in urls if int(u.rsplit("/", 1)[1]) not in gone]

    more = {
        f"{BASE}/list?page=2": listing_for(keep(MORE_URLS[:10]), "?page=3"),
        f"{BASE}/list?page=3": listing_for(keep(MORE_URLS[10:20]), None),
    }
    return listing_for(keep(URLS)), more


def test_the_shadow_run_must_find_every_kind_of_record() -> None:
    # Review attack B4c's shape: the item container skips every record whose price is
    # empty ("Not disclosed"). 45 of 50 records found is the 90% it must find, but
    # they are all of one kind: 0 of 5, where the others were all found.
    undisclosed = set(range(40, 45))
    details = {
        u: detail(f"Title {i}", f"City {i % 4}", None) if i in undisclosed else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    known = {
        k: v | {"price": None} if int(k.rsplit("/", 1)[1]) in undisclosed else v
        for k, v in KNOWN.items()
    }
    first, more = listing_without(undisclosed)
    report = run(spec(), FakePages(details, first, more), known=known)
    assert report.shadow is not None and report.shadow.compared == 45
    assert report.problems == [
        "the shadow run found 0 of the 5 records of the last good run whose price was empty "
        "(needs 90%), against 45 of the other 45: the item container, the detail links or "
        "the pagination miss that kind of record"
    ]


def test_records_of_one_kind_that_chance_took_are_fine() -> None:
    # 5 of 50 records gone, as a source that churns loses them: 2 of the 13 in City 1
    # (85%), which happens more than one time in three when one record in ten is gone.
    cities = FieldStats(filled=50, domain=[f"City {i}" for i in range(4)])
    baseline = BASELINE.model_copy(update={"fields": {"city": cities}})
    gone = {3, 17, 22, 36, 41}
    first, more = listing_without(gone)
    report = run(spec(), FakePages(DETAILS, first, more), baseline=baseline)
    assert report.accepted, report.problems
    assert report.shadow is not None and report.shadow.compared == 45


def found(min_found: float, cap: int | str = 500) -> SourceSpec:
    data = spec().model_dump(mode="json")
    data["heal"] |= {"shadow_min_found": min_found, "shadow_max_items": cap}
    return SourceSpec.model_validate(data)


def test_a_source_that_loses_records_naturally_may_need_less_of_them_found() -> None:
    # 40 of the last good run's 50 records are still listed: 80%, like a notice board
    # whose closed notices come down between runs.
    pages = FakePages(
        {u: h for i, (u, h) in enumerate(DETAILS.items()) if i < 40}, listing_for(URLS)
    )
    assert run(found(0.8), pages).accepted
    assert run(found(0.85), pages).problems == [
        "the shadow run found 40 of the last good run's 50 records (needs 85%): the item "
        "container, the detail links or the pagination miss some"
    ]
    assert run(found(0.8), pages).min_found == 0.8  # the report says what it was judged by


def test_a_sample_from_a_source_with_new_records_may_need_fewer_of_them_known() -> None:
    # 6 of the 30 records the sample draws are new since the last good run: 24 known.
    keys = sorted(DETAILS, key=lambda key: rank(key, SHADOW_SEED))[:30]
    known = {k: v for k, v in KNOWN.items() if k not in keys[:6]}
    first = f"{BASE}/list"
    more = MORE | {first: listing_for(URLS)}

    def sample(min_found: float) -> VerificationReport:
        pages = FakePages(DETAILS, listing_for(URLS), more)
        report: VerificationReport = run(
            found(min_found, cap=30), pages, baseline=LISTINGS, known=known
        )
        return report

    assert sample(0.8).accepted, sample(0.8).problems
    assert sample(0.85).problems == [
        "only 24 of the 30 sampled records are known from the last good run (needs 85%): "
        "the detail link or the key reads something else"
    ]


def test_a_sample_of_a_listing_that_lost_items_may_be_allowed() -> None:
    # Replaces test_a_sample_of_listing_pages_holding_fewer_items_may_be_allowed, which
    # counted the items of the listing pages a sample drew: now it reads them all.
    # Listing page 2 now lists 3 notices where the last good run saw 10: 43 of 50 items.
    more = MORE | {f"{BASE}/list?page=2": listing_for(MORE_URLS[:3], "?page=3")}
    pages = FakePages(DETAILS, listing_for(URLS), more)
    assert run(found(0.85, cap=30), pages, baseline=LISTINGS).accepted
    pages = FakePages(DETAILS, listing_for(URLS), more)
    assert run(found(0.9, cap=30), pages, baseline=LISTINGS).problems == [
        "the shadow run found 43 items on 3 listing pages, where the last good run found 50 "
        "(needs 90%): the item container or the pagination miss some"
    ]


def test_required_fields_must_be_filled_on_records_the_last_good_run_did_not_have() -> None:
    new = f"{BASE}/d/99"
    details = DETAILS | {new: "<p class='city'>City 3</p><p class='price'>99</p>"}
    report = run(spec(), FakePages(details, listing_for([*URLS, new])))
    assert report.problems == [
        "required field title is empty on 1 of 1 records the last good run didn't have"
    ]


def test_without_the_last_good_runs_records_the_shadow_run_cannot_run() -> None:
    # Fail closed: a repair whose shadow run can't run is not accepted. Nothing in it
    # is wrong, either: only --force can promote it.
    report = run(spec(), known={})
    assert not report.accepted and report.verdict == "not run"
    assert report.problems == []
    assert not_run(report) == {"shadow run": "the last good run's records are not stored"}
    assert {name for name, status in statuses(report).items() if status == "passed"} == {
        "regexes",
        "listing",
        "anchors",
        "sample",
        "known values",
    }


def test_a_detail_page_that_cannot_be_fetched_stops_the_shadow_run() -> None:
    # A timeout or a 5xx after every retry: nothing is known about that record.
    unreachable = URLS[25]
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), unreachable={unreachable}))
    assert report.verdict == "not run"
    assert not_run(report) == {
        "shadow run": f"{unreachable} could not be loaded (HTTP 503 after 4 attempts); it "
        "compared 25 of the last good run's 50 records before it stopped"
    }


@pytest.mark.parametrize(("changed", "verdict"), [(5, "not run"), (6, "failed")])
def test_mismatches_found_before_the_shadow_run_stopped_still_fail(
    changed: int, verdict: str
) -> None:
    # The crawl stops at page 45, which can't be fetched. Before it, city was wrong on
    # page 25, and price changed on `changed` pages: 6 is more than 10% of the 50 records
    # whatever the rest would have read, so --force can't promote it; 5 may not be.
    unreachable = list(DETAILS)[45]
    pages = {
        u: new_price(html) if 20 <= i < 20 + changed else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(pages, listing_for(URLS), unreachable={unreachable}))
    assert report.verdict == verdict
    wrong_city = {
        u: html.replace("class='city'", "class='town'") if i == 25 else html
        for i, (u, html) in enumerate(DETAILS.items())
    }
    stopped = run(spec(), FakePages(wrong_city, listing_for(URLS), unreachable={unreachable}))
    assert stopped.verdict == "failed"
    assert stopped.problems == [
        "city reproduces 44/45 values of the last good run (needs 100%); "
        "expected 'City 1', got None"
    ]


def test_a_listing_page_that_cannot_be_fetched_stops_the_shadow_run() -> None:
    # Listing page 4 is past the pagination check: only the shadow run gets there.
    page_4 = f"{BASE}/list?page=4"
    more = MORE | {
        f"{BASE}/list?page=3": listing_for(MORE_URLS[10:20], "?page=4"),
        page_4: listing_for(MORE_URLS[20:30], None),
    }
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more, unreachable={page_4}))
    assert not_run(report) == {
        "shadow run": f"listing page 4 could not be loaded (HTTP 503 after 4 attempts): {page_4}; "
        "it compared 50 of the last good run's 50 records before it stopped"
    }


def test_too_few_comparable_records_stop_the_shadow_run() -> None:
    # 19 of the last good run's 21 records are still listed: 90% found, but 19 records
    # to compare is too few to say anything about the rest.
    listed = list(DETAILS)[:19]
    known = {k: v for k, v in KNOWN.items() if k in listed} | {
        f"{BASE}/d/{i}": {"title": f"Title {i}", "city": "City 0", "price": "1"} for i in (98, 99)
    }
    pages = FakePages(DETAILS, listing_for(listed, None))
    report = run(
        spec(), pages, known=known, baseline=BASELINE.model_copy(update={"listing_pages": 1})
    )
    assert report.shadow.compared == 19
    assert not_run(report) == {"shadow run": "only 19 records can be compared (needs 20)"}


def numbered(pages: int, per_page: int, link_ends: int | None = None) -> dict[str, str]:
    """Listing pages 1 to `pages`, by URL, of `per_page` items each. From page
    `link_ends` on, the link onwards is one the spec's selector (a.next) doesn't find,
    as a pager whose links change past page 9 would."""
    urls = [f"{BASE}/d/{i}" for i in range(pages * per_page)]
    out = {}
    for n in range(1, pages + 1):
        listing = listing_for(urls[(n - 1) * per_page : n * per_page], None)
        if n < pages:
            kind = "more" if link_ends is not None and n >= link_ends else "next"
            listing += f"<a class='{kind}' href='?page={n + 1}'>Next</a>"
        out[f"{BASE}/list" + (f"?page={n}" if n > 1 else "")] = listing
    return out


def run_pages(
    candidate: SourceSpec,
    link_ends: int | None = None,
    details: dict[str, str] | None = None,
    **kwargs: Any,
) -> tuple[Any, FakePages]:
    """A source of 100 records on 10 listing pages, which the last good run read:
    `details` are their detail pages now, the last good run's values by default."""
    details = details or page_set(100)
    listings = numbered(10, 10, link_ends)
    pages = FakePages(details, listings[f"{BASE}/list"], listings)
    urls = [f"{BASE}/d/{i}" for i in range(100)]
    kwargs.setdefault("anchors", anchors_for(details, urls[::10]))  # one on each listing page
    kwargs.setdefault("baseline", BASELINE.model_copy(update={"listing_pages": 10, "items": 100}))
    kwargs.setdefault("known", {a.key: a.values for a in anchors_for(details, urls)})
    return run(candidate, pages, **kwargs), pages


def test_a_sampled_shadow_run_walks_every_listing_page() -> None:
    # Replaces test_a_sample_needs_the_last_good_runs_listing_pages: a sample drew
    # listing pages from the last good run's, by URL, so it never followed the
    # candidate's next-page link. It walks the listing as a run would now, and samples
    # only the detail pages, from the items of every listing page.
    report, pages = run_pages(sampling(50))  # BASELINE predates RunSummary.listings
    assert report.accepted, report.problems
    shadow = report.shadow
    assert (shadow.scope, shadow.listing_pages, shadow.items) == ("sample", 10, 100)
    assert (shadow.drawn, shadow.records, shadow.compared) == (50, 50, 50)
    assert pages.listings == [f"{BASE}/list?page={n}" for n in range(2, 11)]
    drawn = pages.requested[-50:]  # after the anchors' and the sample's pages
    assert {int(url.rsplit("/", 1)[1]) // 10 for url in drawn} == set(range(10))  # all pages


def test_a_sampled_shadow_run_follows_the_next_page_link_to_its_end() -> None:
    # Review attack B6: the next-page link works on the pages the quick checks follow,
    # and stops at page 5 of 10. Half the source would never be read again.
    report, pages = run_pages(sampling(20), link_ends=5)
    assert report.problems == [
        "the shadow run found 50 items on 5 listing pages, where the last good run found 100 "
        "(needs 90%): the item container or the pagination miss some"
    ]
    assert report.shadow is not None and report.shadow.listing_pages == 5
    assert pages.listings == [f"{BASE}/list?page={n}" for n in range(2, 6)]


# Every listing page of the source as the last good run recorded it.
LISTINGS = BASELINE.model_copy(
    update={
        "listing_pages": 3,
        "items": 50,
        "listings": [
            ListingPage(url=f"{BASE}/list", items=30),
            ListingPage(url=f"{BASE}/list?page=2", items=10),
            ListingPage(url=f"{BASE}/list?page=3", items=10),
        ],
    }
)


def sampling(cap: int | str) -> SourceSpec:
    data = spec().model_dump(mode="json")
    data["heal"]["shadow_max_items"] = cap
    return SourceSpec.model_validate(data)


def test_all_reads_the_whole_source_however_many_records() -> None:
    def pages() -> FakePages:  # page 1 too, by its URL, as a sample reads it
        return FakePages(DETAILS, listing_for(URLS), MORE | {f"{BASE}/list": listing_for(URLS)})

    capped = run(sampling(20), pages(), baseline=LISTINGS)
    assert capped.accepted, capped.problems
    shadow = capped.shadow
    assert (shadow.scope, shadow.records, shadow.compared) == ("sample", 20, 20)
    whole = run(sampling("all"), pages(), baseline=LISTINGS)
    assert whole.accepted, whole.problems
    shadow = whole.shadow
    assert (shadow.scope, shadow.records, shadow.compared) == ("full", 50, 50)


def test_by_default_a_source_of_more_than_500_records_is_sampled() -> None:
    details = page_set(600)
    urls = list(details)
    listings = {
        f"{BASE}/list" + (f"?page={n + 1}" if n else ""): listing_for(
            urls[30 * n : 30 * (n + 1)], f"?page={n + 2}" if n < 19 else None
        )
        for n in range(20)
    }
    baseline = BASELINE.model_copy(
        update={
            "listing_pages": 20,
            "listings": [ListingPage(url=url, items=30) for url in listings],
        }
    )
    kwargs: dict[str, Any] = {
        "anchors": anchors_for(details, urls[:10]),
        "baseline": baseline,
        "known": {a.key: a.values for a in anchors_for(details, urls)},
    }
    default = run(spec(), FakePages(details, listings[f"{BASE}/list"], listings), **kwargs)
    assert default.accepted, default.problems
    assert (default.shadow.scope, default.shadow.compared) == ("sample", 500)
    whole = run(sampling("all"), FakePages(details, listings[f"{BASE}/list"], listings), **kwargs)
    assert whole.accepted, whole.problems
    assert (whole.shadow.scope, whole.shadow.compared) == ("full", 600)


def test_the_shadow_run_says_what_it_will_read_before_it_starts() -> None:
    # The failed run stored listing pages 1 to 3 and 20 detail pages (the quick
    # checks'); the last good run read 5 listing pages and 50 records.
    stored: dict[SnapshotKind, set[str]] = {
        "listing": {f"{BASE}/list", *MORE},
        "detail": set(URLS[:20]),
    }
    pages = FakePages(DETAILS, listing_for(URLS), stored=stored)
    told: list[tuple[ShadowPlan, int]] = []
    report = run(spec(), pages, on_shadow=lambda plan: told.append((plan, len(pages.requested))))
    assert report.accepted, report.problems
    [(plan, requested)] = told
    assert requested == 20  # the anchors' and the sample's pages: no crawl yet
    assert plan == ShadowPlan(
        scope="full",
        records=50,
        known=50,
        listing_pages=5,
        detail_pages=50,
        stored=23,
        requests=32,
        rps=1.0,
    )
    assert len(pages.requested) == 20 + 50


def test_a_sampled_shadow_run_says_what_it_will_read() -> None:
    first = f"{BASE}/list"
    more = MORE | {first: listing_for(URLS)}
    # The first heal: only page 1 is stored of the 3 listing pages drawn, so none of
    # the 20 detail pages drawn can be known to be stored.
    told: list[ShadowPlan] = []
    stored: dict[SnapshotKind, set[str]] = {"listing": {first}, "detail": set(URLS[:20])}
    pages = FakePages(DETAILS, listing_for(URLS), more, stored=stored)
    report = run(sampling(20), pages, baseline=LISTINGS, on_shadow=told.append)
    assert report.accepted, report.problems
    plan = ShadowPlan(
        scope="sample",
        records=20,
        known=50,
        listing_pages=3,
        detail_pages=20,
        stored=1,
        requests=22,
        rps=1.0,
    )
    assert told == [plan]
    # Verified again, as promote does: every page the sample reads is stored by now.
    stored = {"listing": set(more), "detail": set(DETAILS)}
    pages = FakePages(DETAILS, listing_for(URLS), more, stored=stored)
    report = run(sampling(20), pages, baseline=LISTINGS, on_shadow=told.append)
    assert report.accepted, report.problems
    assert told[1] == plan.model_copy(update={"stored": 23, "requests": 0})


def test_a_failed_check_is_not_hidden_by_one_that_did_not_run() -> None:
    report = run(spec(city={"strategies": [{"css": "h1"}]}), known={})
    assert report.verdict == "failed"
    assert statuses(report)["anchors"] == "failed"
    assert not_run(report) == {"shadow run": "skipped: a quicker check failed"}


def test_the_shadow_run_waits_for_the_quick_checks() -> None:
    # A wrong field on the anchors already rejects the repair: no crawl of the source.
    pages = FakePages(DETAILS, listing_for(URLS))
    report = run(spec(city={"strategies": [{"css": "h1"}]}), pages)
    assert not report.accepted and report.shadow is None
    assert not_run(report) == {"shadow run": "skipped: a quicker check failed"}
    assert pages.requested == URLS[:20]


def test_anchor_pages_that_fail_to_load_are_tolerated() -> None:
    reachable = {u: h for i, (u, h) in enumerate(DETAILS.items()) if i not in (4, 8)}
    report = run(spec(), FakePages(reachable, listing_for(URLS)))
    assert report.accepted, report.problems
    assert report.anchors_checked == 8


def test_with_fewer_than_three_anchors_all_must_load() -> None:
    reachable = {u: h for i, (u, h) in enumerate(DETAILS.items()) if i != 1}
    report = run(spec(), FakePages(reachable, listing_for(URLS)), anchors=ANCHORS[:2])
    assert not report.accepted
    assert not_run(report)["anchors"] == "only 1 of 2 anchor pages could be loaded"


def test_overfitted_to_shown_anchors() -> None:
    # Matches exactly the titles of three anchors, as if memorised from examples.
    memorised = "|".join(f"Title {i}" for i in range(3))
    report = run(spec(title={"required": True, "strategies": [{"regex": rf"\b({memorised})\b"}]}))
    assert not report.accepted
    assert report.fields["title"].matched == 3
    assert any(
        "required field title is empty on 10 of 10 other pages" in p for p in report.problems
    )


def test_constant_value_on_other_pages_is_suspicious() -> None:
    # Right on every anchor, the same value everywhere else: fits only the examples.
    pages = {
        u: (html if i < 10 else detail("Title 0", "City 0", "1"))
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(pages, listing_for(URLS)))
    assert not report.accepted
    assert any("title reads 'Title 0' on every other page" in p for p in report.problems)


def test_empty_on_other_pages() -> None:
    pages = {
        u: (html if i < 10 else detail(f"Title {i}", "", str(i)))
        for i, (u, html) in enumerate(DETAILS.items())
    }
    report = run(spec(), FakePages(pages, listing_for(URLS)))
    assert "city is empty on all 10 other pages" in report.problems


def test_container_must_still_find_items() -> None:
    report = run(spec(), FakePages(DETAILS, "<p>nothing here</p>"))
    assert "the item container matches nothing on the listing page" in report.problems


def test_items_must_still_link_to_details() -> None:
    listing = listing_for(URLS[:5]) + "<ul>" + "<li>no link</li>" * 25 + "</ul>"
    report = run(spec(), FakePages(DETAILS, listing))
    assert "only 5 of 30 items link to a detail page" in report.problems


def test_every_item_must_link_to_its_detail_page() -> None:
    # A container that also matches a header row: one item in 31 without a link
    # would fail on every page of every crawl.
    listing = "<ul><li>Title</li></ul>" + listing_for(URLS)
    report = run(spec(), FakePages(DETAILS, listing))
    assert report.problems == ["only 30 of 31 items link to a detail page"]


def test_every_item_on_later_listing_pages_must_link_too() -> None:
    page_2 = "<ul><li>Title</li></ul>" + listing_for(MORE_URLS[:10], "?page=3")
    more = MORE | {f"{BASE}/list?page=2": page_2}
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more))
    assert report.problems == ["only 10 of 11 items on listing page 2 link to a detail page"]


def test_an_item_matched_twice_is_rejected() -> None:
    # The container matches each item's row and an element inside it.
    candidate = spec().model_copy(
        update={
            "item": ItemSpec.model_validate(
                {"container": {"css": "li, li > a"}, "follow": {"css": "a"}}
            )
        }
    )
    report = run(candidate)
    assert "30 of 60 items repeat another item of the same page" in report.problems


def test_pagination_must_still_work() -> None:
    report = run(spec(), FakePages(DETAILS, listing_for(URLS, next_page=None)))
    assert "no next-page link on the first listing page" in report.problems


def test_pagination_is_followed_for_two_pages() -> None:
    pages = FakePages(DETAILS, listing_for(URLS))
    report = run(spec(), pages)
    assert report.accepted, report.problems
    assert report.listing_pages == 3
    assert pages.listings == [f"{BASE}/list?page=2", f"{BASE}/list?page=3"]


def test_next_page_link_leading_to_the_same_items_is_rejected() -> None:
    # Like a pager whose first link is "Next" on page 1 and "Previous" on page 2: a
    # page that wasn't visited yet, under another URL, listing the same items.
    more = MORE | {f"{BASE}/list?page=3": listing_for(URLS[:25] + MORE_URLS[20:25], None)}
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more))
    assert report.problems == [
        "the next-page link on listing page 2 leads to a page that repeats 25 of 30 items "
        "already seen"
    ]


def test_some_items_on_several_pages_are_fine() -> None:
    # A pinned item, or one pushed onto the next page while crawling.
    more = MORE | {f"{BASE}/list?page=3": listing_for(URLS[:2] + MORE_URLS[10:18], None)}
    assert run(spec(), FakePages(DETAILS, listing_for(URLS), more)).accepted


def test_next_page_link_to_a_visited_page_is_rejected() -> None:
    more = MORE | {f"{BASE}/list?page=2": listing_for(MORE_URLS[:10], "?page=2")}
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more))
    assert report.problems == ["the next-page link on listing page 2 leads back to an earlier page"]


def test_pagination_ending_early_is_rejected() -> None:
    # Like a link hard-coded to page 2: page 2 has no link onwards.
    more = {f"{BASE}/list?page=2": listing_for(MORE_URLS[:10], None)}
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more))
    assert report.problems == ["pagination ends after 2 listing pages; the last good run had 5"]
    two_pages = BASELINE.model_copy(update={"listing_pages": 2})
    had = {k: v for k, v in KNOWN.items() if k in URLS + MORE_URLS[:10]}  # its 40 records
    pages = FakePages(DETAILS, listing_for(URLS), more)
    assert run(spec(), pages, baseline=two_pages, known=had).accepted


def test_next_listing_page_must_load() -> None:
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more={}))
    assert report.problems == [f"listing page 2 could not be loaded: {BASE}/list?page=2"]


def test_next_listing_page_must_have_items() -> None:
    more = MORE | {f"{BASE}/list?page=2": "<p>Nothing here</p>"}
    report = run(spec(), FakePages(DETAILS, listing_for(URLS), more))
    assert report.problems == ["the item container matches nothing on listing page 2"]


# --- listing-only specs: no detail pages ------------------------------------------------


def listing_spec(**fields: dict[str, Any]) -> SourceSpec:
    default: dict[str, dict[str, Any]] = {
        "ref": {"required": True, "strategies": [{"css": ".ref"}]},
        "name": {"strategies": [{"css": ".name"}]},
    }
    return SourceSpec.model_validate(
        {
            "id": "demo",
            "fetch": {"start": f"{BASE}/list", "paginate": {"next": {"css": "a.next"}}},
            "item": {"container": {"css": "li"}, "key": "ref"},
            "fields": default | fields,
        }
    )


def rows(numbers: range, next_page: str | None) -> str:
    items = "".join(
        f"<li><span class='ref'>R{i}</span> <span class='name'>Name {i}</span></li>"
        for i in numbers
    )
    pager = f"<a class='next' href='{next_page}'>Next</a>" if next_page else ""
    return f"<ul>{items}</ul>{pager}"


ROWS = {
    f"{BASE}/list?page=2": rows(range(20, 40), "?page=3"),
    f"{BASE}/list?page=3": rows(range(40, 60), "?page=4"),
    f"{BASE}/list?page=4": rows(range(60, 80), None),
}


def row_anchor(i: int, page: int) -> Anchor:
    url = f"{BASE}/list" if page == 1 else f"{BASE}/list?page={page}"
    return Anchor(
        key=f"R{i}",
        url=url,
        values={"ref": f"R{i}", "name": f"Name {i}"},
        raw={},
        strategies={},
        snapshot_id=None,
        run_id="good",
    )


ROW_ANCHORS = [row_anchor(3, 1), row_anchor(17, 1), row_anchor(25, 2), row_anchor(66, 4)]


ROW_KNOWN = {f"R{i}": {"ref": f"R{i}", "name": f"Name {i}"} for i in range(80)}


def run_rows(candidate: SourceSpec, anchors: list[Anchor] = ROW_ANCHORS) -> Any:
    pages = FakePages({}, rows(range(20), "?page=2"), ROWS)
    return verify(candidate, anchors, pages, baseline=BASELINE, known=ROW_KNOWN), pages


def test_listing_only_anchors_are_found_by_their_key() -> None:
    report, pages = run_rows(listing_spec())
    assert report.accepted, report.problems
    assert (report.anchors_checked, report.score) == (4, 1.0)
    assert report.sample == 10  # other rows of the first listing page
    # Pages 2 and 3 along the next link, then the last anchor's own listing page.
    assert pages.listings == [f"{BASE}/list?page={n}" for n in (2, 3, 4)]
    assert pages.requested == []  # no detail pages


def test_listing_only_anchors_that_moved_or_went_are_tolerated() -> None:
    # R25 is now listed on page 2, not where it was found; R99 is gone.
    anchors = [*ROW_ANCHORS[:3], row_anchor(25, 1), row_anchor(99, 2)]
    report, _ = run_rows(listing_spec(), anchors)
    assert report.accepted, report.problems
    assert report.anchors_checked == 4


def test_listing_only_wrong_field_is_rejected() -> None:
    report, _ = run_rows(listing_spec(name={"strategies": [{"css": ".ref"}]}))
    assert not report.accepted
    assert (report.fields["ref"].rate, report.fields["name"].rate) == (1.0, 0.0)


def test_listing_only_items_must_have_a_key() -> None:
    pages = FakePages({}, "<ul><li>Ref - Name</li></ul>" + rows(range(20), "?page=2"), ROWS)
    report = verify(listing_spec(), ROW_ANCHORS, pages, baseline=BASELINE)
    assert report.problems == ["only 20 of 21 items have a key (ref)"]


def test_listing_only_shadow_run_compares_every_item() -> None:
    # R15 is on the first listing page, but neither an anchor nor in the sample. This
    # replaces a test of the stored-pages step, which compared the first page's rows.
    first = rows(range(20), "?page=2").replace("Name 15", "Name 15b")
    pages = FakePages({}, first, ROWS)
    report = verify(listing_spec(), ROW_ANCHORS, pages, baseline=BASELINE, known=ROW_KNOWN)
    assert report.shadow is not None
    assert (report.shadow.listing_pages, report.shadow.compared) == (4, 80)
    assert report.problems == [
        "name reproduces 79/80 values of the last good run (needs 100%); "
        "expected 'Name 15', got 'Name 15b'"
    ]
    assert pages.requested == []


def test_listing_only_broken_key_finds_no_anchors() -> None:
    report, _ = run_rows(listing_spec(ref={"required": True, "strategies": [{"css": ".nope"}]}))
    assert "only 0 of 4 anchors were found on their listing pages" in report.problems


def test_needs_the_failed_runs_listing_page() -> None:
    # E.g. `scrapemend promote` pointed at the wrong snapshot directory: without a
    # listing page, no listing check could run, so there is no verdict.
    report = run(spec(), FakePages(DETAILS, listing=None))
    assert not report.accepted
    assert not_run(report) == {
        "listing": "no listing page of the failed run to verify against",
        "shadow run": "skipped: no listing page to start from",
    }


def test_needs_anchors() -> None:
    report = run(spec(), anchors=[])
    assert not report.accepted
    assert not_run(report)["anchors"] == (
        "no anchors to verify against: the source has had no good run yet"
    )


def test_needs_enough_anchor_pages() -> None:
    reachable = {u: h for i, (u, h) in enumerate(DETAILS.items()) if i in (0, 1) or i >= 10}
    report = run(spec(), FakePages(reachable, listing_for(URLS)))
    assert not report.accepted
    assert not_run(report)["anchors"] == "only 2 of 10 anchor pages could be loaded"


def test_samples_skip_anchor_pages() -> None:
    pages = FakePages(DETAILS, listing_for(URLS))
    run(spec(), pages)
    assert pages.requested[:10] == URLS[:10]  # anchors first
    assert pages.requested[10:20] == URLS[10:20]  # then 10 pages that aren't anchors
    assert pages.requested[20:] == [*URLS, *MORE_URLS[:20]]  # then the shadow run's crawl


# --- regexes ----------------------------------------------------------------------------


def test_a_regex_that_takes_too_long_rejects_the_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    # Like review attack R: every value is right, through the fallback, but the regex
    # backtracks catastrophically on the note's text, and every run would pay for it.
    monkeypatch.setattr(patterns, "TIME_LIMIT", 0.25)
    sentence = "The contract covers supply and installation over twelve long months, in Dunmere."
    noted = {u: html + f"<p class='note'>{sentence}</p>" for u, html in DETAILS.items()}
    slow = {"regex": r"^(\w+\s?)+$", "within": {"css": ".note"}}
    candidate = spec(city={"strategies": [slow, {"css": ".city"}]})
    report = run(candidate, FakePages(noted, listing_for(URLS)))
    assert report.fields["city"].rate == 1.0
    assert report.problems == [r"city: regex '^(\\w+\\s?)+$' took longer than 0.25s on 20 pages"]
    assert statuses(report)["regexes"] == "failed"
    assert not_run(report) == {"shadow run": "skipped: a quicker check failed"}


def test_the_regexes_check_says_which_engine_each_regex_runs_on() -> None:
    title = {"required": True, "strategies": [{"regex": "^(Title .+)$", "within": {"css": "h1"}}]}
    city = {"strategies": [{"regex": "(City .+)", "within": {"css": ".city"}, "engine": "re2"}]}
    report = run(spec(title=title, city=city))
    assert report.accepted, report.problems
    assert report.check("regexes").reason == (
        "2 regexes: 1 on RE2, 1 on Python's re (at most 1s a search)"
    )
    assert run(spec()).check("regexes").reason == "no regexes"
