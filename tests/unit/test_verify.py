"""The verification gate's rules, on small hand-built pages."""

from typing import Any

import pytest

from scrapemend.extract import Document
from scrapemend.heal.anchors import Anchor
from scrapemend.heal.verify import verify
from scrapemend.pipeline import FieldStats, RunSummary
from scrapemend.spec import SourceSpec
from scrapemend.spec.models import HealSpec

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
    def __init__(
        self, details: dict[str, str], listing: str | None = None, more: dict[str, str] = MORE
    ) -> None:
        self.details = details
        self.first = listing
        self.more = more
        self.requested: list[str] = []
        self.listings: list[str] = []

    def listing_pages(self) -> list[Document]:
        return [Document.parse(self.first, url=f"{BASE}/list")] if self.first is not None else []

    def listing(self, url: str) -> Document | None:
        self.listings.append(url)
        html = self.more.get(url)
        return Document.parse(html, url=url) if html is not None else None

    def page(self, url: str) -> Document | None:
        self.requested.append(url)
        html = self.details.get(url)
        return Document.parse(html, url=url) if html is not None else None


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


DETAILS = page_set(30)
URLS = list(DETAILS)
MORE_URLS = [f"{BASE}/d/{i}" for i in range(30, 60)]  # items on later listing pages
ANCHORS = anchors_for(DETAILS, URLS[:10])
BASELINE = RunSummary(source_id="demo", spec_version=1, run_id="good", listing_pages=5)


def run(candidate: SourceSpec, pages: FakePages | None = None, **kwargs: Any) -> Any:
    pages = pages or FakePages(DETAILS, listing_for(URLS))
    anchors = kwargs.pop("anchors", ANCHORS)
    return verify(candidate, anchors, pages, baseline=kwargs.pop("baseline", BASELINE), **kwargs)


def test_a_working_spec_is_accepted() -> None:
    report = run(spec())
    assert report.accepted, report.problems
    assert (report.anchors_checked, report.sample, report.listing_items) == (10, 10, 30)
    assert report.score == 1.0


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
    assert run(spec(), FakePages(DETAILS, listing_for(URLS), more), baseline=two_pages).accepted


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


def run_rows(candidate: SourceSpec, anchors: list[Anchor] = ROW_ANCHORS) -> Any:
    pages = FakePages({}, rows(range(20), "?page=2"), ROWS)
    return verify(candidate, anchors, pages, baseline=BASELINE), pages


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


def test_listing_only_broken_key_finds_no_anchors() -> None:
    report, _ = run_rows(listing_spec(ref={"required": True, "strategies": [{"css": ".nope"}]}))
    assert "only 0 of 4 anchors were found on their listing pages" in report.problems


def test_needs_anchors() -> None:
    assert "no anchors to verify against" in run(spec(), anchors=[]).problems[0]


def test_needs_enough_anchor_pages() -> None:
    reachable = {u: h for i, (u, h) in enumerate(DETAILS.items()) if i in (0, 1) or i >= 10}
    report = run(spec(), FakePages(reachable, listing_for(URLS)))
    assert "only 2 of 10 anchor pages could be loaded" in report.problems


def test_samples_skip_anchor_pages() -> None:
    pages = FakePages(DETAILS, listing_for(URLS))
    run(spec(), pages)
    assert pages.requested[:10] == URLS[:10]  # anchors first
    assert pages.requested[10:] == URLS[10:20]  # then 10 pages that aren't anchors
