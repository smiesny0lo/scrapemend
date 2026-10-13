import math
import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from selectolax.lexbor import LexborHTMLParser

from examples.mutating_site.app import LAYOUTS, PAGE_SIZE, create_app
from examples.mutating_site.records import (
    COUNT,
    Notice,
    format_date,
    format_money,
    generate_notices,
)

NOTICES = generate_notices()
PAGES = math.ceil(COUNT / PAGE_SIZE)
DETAIL_LINK = re.compile(r'href="/notices/(TN-\d{4}-\d{4})"')


def page_text(html: str) -> str:
    body = LexborHTMLParser(html).body
    assert body is not None
    return " ".join(body.text(separator=" ").split())


def detail_values(n: Notice) -> list[str]:
    return [
        n.id,
        n.title,
        n.authority,
        n.category,
        n.location,
        format_date(n.published),
        format_date(n.deadline),
        format_money(n.value),
        n.status,
        n.description,
    ]


def listing_values(n: Notice) -> list[str]:
    return [n.title, n.authority, format_date(n.deadline), format_money(n.value)]


@pytest.fixture(params=LAYOUTS, ids=lambda layout: f"v{layout}")
def client(request: pytest.FixtureRequest) -> Iterator[TestClient]:
    with TestClient(create_app(layout=request.param)) as c:
        yield c


def listing_pages(client: TestClient) -> Iterator[str]:
    for page in range(1, PAGES + 1):
        response = client.get("/notices", params={"page": page})
        assert response.status_code == 200
        yield response.text


# --- records ---------------------------------------------------------------


def test_records_are_deterministic() -> None:
    assert generate_notices() == NOTICES
    assert len(NOTICES) == COUNT
    assert len({n.id for n in NOTICES}) == COUNT


def test_records_include_undisclosed_values() -> None:
    assert any(n.value is None for n in NOTICES)
    assert any(n.value is not None for n in NOTICES)


def test_deadline_follows_publication() -> None:
    assert all(n.deadline > n.published for n in NOTICES)


# --- same content under every layout -----------------------------------------


def test_detail_pages_show_every_field(client: TestClient) -> None:
    for notice in NOTICES:
        response = client.get(f"/notices/{notice.id}")
        assert response.status_code == 200
        text = page_text(response.text)
        for value in detail_values(notice):
            assert value in text, f"{notice.id}: {value!r} missing"


def test_listing_shows_every_record(client: TestClient) -> None:
    by_id = {n.id: n for n in NOTICES}
    seen: list[str] = []
    for html in listing_pages(client):
        text = page_text(html)
        ids = DETAIL_LINK.findall(html)
        for notice_id in ids:
            for value in listing_values(by_id[notice_id]):
                assert value in text, f"{notice_id}: {value!r} missing"
        seen.extend(ids)
    assert sorted(seen) == sorted(by_id)


def test_listing_order_is_identical_across_layouts() -> None:
    orders = []
    for layout in LAYOUTS:
        client = TestClient(create_app(layout=layout))
        orders.append([i for html in listing_pages(client) for i in DETAIL_LINK.findall(html)])
    assert all(order == orders[0] for order in orders)


def test_markup_differs_between_layouts() -> None:
    notice_id = NOTICES[0].id
    for path in ("/notices", f"/notices/{notice_id}"):
        pages = {TestClient(create_app(layout=layout)).get(path).text for layout in LAYOUTS}
        assert len(pages) == len(LAYOUTS), path


# --- routing -----------------------------------------------------------------


def test_unknown_notice_and_page_are_404(client: TestClient) -> None:
    assert client.get("/notices/TN-0000-0000").status_code == 404
    assert client.get("/notices", params={"page": PAGES + 1}).status_code == 404
    assert client.get("/notices", params={"page": 0}).status_code == 404


def test_robots_txt_disallows_admin(client: TestClient) -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /admin/" in response.text


# --- layout switching ----------------------------------------------------------


def test_admin_switches_layout() -> None:
    client = TestClient(create_app(layout=1))
    path = f"/notices/{NOTICES[0].id}"
    before = client.get(path).text

    response = client.post("/admin/layout/3")
    assert response.status_code == 200
    assert response.json() == {"layout": 3}
    assert client.get("/admin/layout").json() == {"layout": 3}
    assert client.get(path).text != before


def test_admin_rejects_unknown_layout() -> None:
    client = TestClient(create_app(layout=1))
    assert client.post("/admin/layout/4").status_code == 422
    assert client.get("/admin/layout").json() == {"layout": 1}


def test_layout_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYOUT", "2")
    client = TestClient(create_app())
    assert client.get("/admin/layout").json() == {"layout": 2}


def test_invalid_env_layout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAYOUT", "7")
    with pytest.raises(ValueError):
        create_app()
