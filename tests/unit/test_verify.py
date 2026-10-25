"""The verification gate's rules, on small hand-built pages."""

from typing import Any

import pytest

from scrapemend.extract import Document
from scrapemend.heal.anchors import Anchor
from scrapemend.heal.verify import verify
from scrapemend.pipeline import RunSummary
from scrapemend.spec import SourceSpec

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


class FakePages:
    def __init__(self, details: dict[str, str], listing: str | None = None) -> None:
        self.details = details
        self.listing = listing
        self.requested: list[str] = []

    def listing_pages(self) -> list[Document]:
        return (
            [Document.parse(self.listing, url=f"{BASE}/list")] if self.listing is not None else []
        )

    def page(self, url: str) -> Document | None:
        self.requested.append(url)
        html = self.details.get(url)
        return Document.parse(html, url=url) if html is not None else None


def listing_for(urls: list[str], next_link: bool = True) -> str:
    items = "".join(f"<li><a href='{u}'>x</a></li>" for u in urls)
    return f"<ul>{items}</ul>" + ("<a class='next' href='?page=2'>Next</a>" if next_link else "")


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
ANCHORS = anchors_for(DETAILS, URLS[:10])
BASELINE = RunSummary(source_id="demo", spec_version=1, run_id="good", listing_pages=5)


def run(candidate: SourceSpec, pages: FakePages | None = None, **kwargs: Any) -> Any:
    pages = pages or FakePages(DETAILS, listing_for(URLS))
    return verify(candidate, kwargs.pop("anchors", ANCHORS), pages, baseline=BASELINE, **kwargs)


def test_a_working_spec_is_accepted() -> None:
    report = run(spec())
    assert report.accepted, report.problems
    assert (report.anchors_checked, report.sample, report.listing_items) == (10, 10, 30)
    assert report.score == 1.0


def test_volatile_fields_only_need_to_be_present() -> None:
    changed = {
        u: html.replace("<p class='price'>", "<p class='price'>9") for u, html in DETAILS.items()
    }
    assert run(spec(), FakePages(changed, listing_for(URLS))).accepted


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
    report = run(spec(), FakePages(DETAILS, listing_for(URLS, next_link=False)))
    assert "no next-page link on the first listing page" in report.problems


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
