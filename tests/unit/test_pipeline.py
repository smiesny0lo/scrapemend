from pathlib import Path
from typing import Any

import pytest

from specmend.extract import Document
from specmend.extract.select import find_link, select_all
from specmend.fetch import BlockedError, FetchError, Response, TransientError
from specmend.pipeline import MAX_CONSECUTIVE_FAILURES, FieldStats, RunAborted, SourceRun
from specmend.snapshots import FilesystemSnapshotStore
from specmend.spec import CssSpec, SourceSpec, XPathSpec

BASE = "https://example.org"


class ScriptedFetcher:
    """Serves canned pages by URL; an exception in the script is raised instead."""

    def __init__(self, pages: dict[str, str | int | FetchError]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    def fetch(self, url: str) -> Response:
        self.fetched.append(url)
        page = self.pages.get(url, 404)
        if isinstance(page, FetchError):
            raise page
        if isinstance(page, int):
            return Response(url, page, {}, b"")
        return Response(url, 200, {"content-type": "text/html"}, page.encode())


def listing(*links: str | None, next_page: str | None = None) -> str:
    rows = "".join(
        f"<li class='item'><a href='{link}'>{link}</a></li>" if link else "<li class='item'></li>"
        for link in links
    )
    pager = f"<a class='next' href='{next_page}'>Next</a>" if next_page else ""
    return f"<ul>{rows}</ul>{pager}"


def detail(title: str) -> str:
    return f"<h1>{title}</h1>"


def spec(**overrides: Any) -> SourceSpec:
    data: dict[str, Any] = {
        "id": "test",
        "fetch": {"start": f"{BASE}/list", "paginate": {"next": {"css": "a.next"}}},
        "item": {"container": {"css": "li.item"}, "follow": {"css": "a"}},
        "fields": {"title": {"required": True, "strategies": [{"css": "h1"}]}},
    }
    return SourceSpec.model_validate(data | overrides)


def run(pages: dict[str, str | int | FetchError], tmp_path: Path, **kwargs: Any) -> SourceRun:
    return SourceRun(
        kwargs.pop("source", spec()),
        ScriptedFetcher(pages),
        FilesystemSnapshotStore(tmp_path),
        **kwargs,
    )


def titles(source_run: SourceRun) -> list[Any]:
    return [r.record["title"] for r in source_run.records()]


# --- crawling ---------------------------------------------------------------------------


def test_follows_items_and_pages(tmp_path: Path) -> None:
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a", "/b", next_page="/list?page=2"),
        f"{BASE}/list?page=2": listing("/c"),
        f"{BASE}/a": detail("A"),
        f"{BASE}/b": detail("B"),
        f"{BASE}/c": detail("C"),
    }
    source_run = run(pages, tmp_path)
    assert titles(source_run) == ["A", "B", "C"]
    summary = source_run.summary
    assert (summary.listing_pages, summary.items, summary.records) == (2, 3, 3)
    store = FilesystemSnapshotStore(tmp_path)
    kinds = [store.get(i).kind for i in store.snapshots("test", summary.run_id)]
    assert kinds == ["listing", "detail", "detail", "listing", "detail"]


def test_record_url_is_the_detail_page(tmp_path: Path) -> None:
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a"),
        f"{BASE}/a": detail("A"),
    }
    [result] = run(pages, tmp_path).records()
    assert result.record == {"url": f"{BASE}/a", "title": "A"}


def test_without_follow_fields_come_from_the_item(tmp_path: Path) -> None:
    source = spec(
        item={"container": {"css": "li.item"}, "key": "title"},
        fields={"title": {"required": True, "strategies": [{"css": "a"}]}},
    )
    pages: dict[str, str | int | FetchError] = {f"{BASE}/list": listing("/a", "/b")}
    assert titles(run(pages, tmp_path, source=source)) == ["/a", "/b"]


def test_stops_at_max_pages_and_on_loops(tmp_path: Path) -> None:
    loop: dict[str, str | int | FetchError] = {f"{BASE}/list": listing(next_page="/list")}
    looping = run(loop, tmp_path)
    list(looping.records())
    assert looping.summary.listing_pages == 1

    chain: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing(next_page="/2"),
        f"{BASE}/2": listing(next_page="/3"),
        f"{BASE}/3": listing(),
    }
    limited = run(
        chain,
        tmp_path,
        source=spec(
            fetch={"start": f"{BASE}/list", "paginate": {"next": {"css": "a.next"}, "max_pages": 2}}
        ),
    )
    list(limited.records())
    assert limited.summary.listing_pages == 2


def test_limit(tmp_path: Path) -> None:
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a", "/b", "/c"),
        **{f"{BASE}/{x}": detail(x) for x in "abc"},
    }
    source_run = run(pages, tmp_path, limit=2)
    assert titles(source_run) == ["a", "b"]
    assert f"{BASE}/c" not in source_run.fetcher.fetched  # type: ignore[attr-defined]


# --- failures -----------------------------------------------------------------------------------


def test_failed_details_are_skipped_and_counted(tmp_path: Path) -> None:
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a", "/gone", None, "/flaky", "/b"),
        f"{BASE}/a": detail("A"),
        f"{BASE}/flaky": TransientError(f"{BASE}/flaky", "HTTP 503 after 4 attempts"),
        f"{BASE}/b": detail("B"),
    }
    source_run = run(pages, tmp_path)
    assert titles(source_run) == ["A", "B"]
    assert [(f.url, f.reason) for f in source_run.summary.failures] == [
        (f"{BASE}/gone", "HTTP 404"),
        (None, "item has no detail link"),
        (f"{BASE}/flaky", "HTTP 503 after 4 attempts"),
    ]


def test_a_streak_of_failures_aborts(tmp_path: Path) -> None:
    links = ["/a"] + [f"/gone{i}" for i in range(MAX_CONSECUTIVE_FAILURES)] + ["/b"]
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing(*links),
        f"{BASE}/a": detail("A"),
    }
    source_run = run(pages, tmp_path)
    records = source_run.records()
    assert next(records).record["title"] == "A"  # what came before stays valid
    with pytest.raises(RunAborted, match="5 items in a row failed"):
        next(records)


def test_blocked_detail_aborts(tmp_path: Path) -> None:
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a"),
        f"{BASE}/a": BlockedError(f"{BASE}/a", "HTTP 403"),
    }
    with pytest.raises(BlockedError):
        list(run(pages, tmp_path).records())


def test_broken_listing_aborts(tmp_path: Path) -> None:
    with pytest.raises(RunAborted, match="HTTP 500"):
        list(run({f"{BASE}/list": 500}, tmp_path).records())


def test_counts_fallbacks_and_empty_fields(tmp_path: Path) -> None:
    source = spec(
        fields={
            "title": {"required": True, "strategies": [{"css": "h1"}, {"css": "h2"}]},
            "price": {"type": "decimal", "strategies": [{"css": ".price"}]},
        }
    )
    pages: dict[str, str | int | FetchError] = {
        f"{BASE}/list": listing("/a", "/b"),
        f"{BASE}/a": "<h1>A</h1><p class='price'>12</p>",
        f"{BASE}/b": "<h2>B</h2><p class='price'>ask us</p>",
    }
    source_run = run(pages, tmp_path, source=source)
    list(source_run.records())
    summary = source_run.summary
    assert (summary.fallbacks, summary.empty, summary.errors) == (
        {"title": 1},
        {"price": 1},
        {"price": 1},
    )


def test_keeps_the_values_of_fields_that_take_few(tmp_path: Path) -> None:
    # A status has a handful of values; a title has one per item. The verification
    # gate checks that a repaired spec still reads values from the handful.
    source = spec(
        fields={
            "title": {"required": True, "strategies": [{"css": "h1"}]},
            "status": {"strategies": [{"css": ".status"}]},
            "due": {"type": "date", "strategies": [{"css": ".due"}]},
        }
    )
    pages: dict[str, str | int | FetchError] = {f"{BASE}/list": listing(*map(str, range(25)))}
    for i in range(25):
        status = "Open" if i % 2 else "Closed"
        pages[f"{BASE}/{i}"] = f"<h1>T{i}</h1><p class='status'>{status}</p><p class='due'>1.2.2026"
    source_run = run(pages, tmp_path, source=source)
    list(source_run.records())
    fields = source_run.summary.fields
    assert fields["status"].known_values == {"Closed", "Open"}
    assert fields["due"].known_values == {"2026-02-01"}  # compared as anchors are: ISO
    assert (fields["title"].domain, fields["title"].known_values) == (None, None)  # 25 titles
    assert FieldStats(filled=5, domain=["a", "b"]).known_values is None  # too few repeats


# --- selection helpers ---------------------------------------------------------------


def test_select_all_css_and_xpath() -> None:
    doc = Document.parse("<ul><li>a</li><li>b</li></ul>")
    assert [d.text for d in select_all(CssSpec(css="li"), doc)] == ["a", "b"]
    assert [d.text for d in select_all(XPathSpec(xpath="//li"), doc)] == ["a", "b"]
    assert select_all(XPathSpec(xpath="count(//li)"), doc) == []


def test_find_link_resolves_and_defaults_to_href() -> None:
    doc = Document.parse("<a class='n' href='?page=2' data-x='/x'>Next</a>", url=f"{BASE}/list")
    assert find_link(CssSpec(css="a.n"), doc) == f"{BASE}/list?page=2"
    assert find_link(CssSpec(css="a.n::attr(data-x)"), doc) == f"{BASE}/x"
    assert find_link(XPathSpec(xpath="//a/@href"), doc) == f"{BASE}/list?page=2"
    assert find_link(CssSpec(css="a.missing"), doc) is None
