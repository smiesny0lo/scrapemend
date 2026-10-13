import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from scrapemend import __version__
from scrapemend.fetch import (
    DEFAULT_USER_AGENT,
    BlockedError,
    FetchError,
    HttpFetcher,
    RobotsDisallowed,
    TransientError,
)
from scrapemend.fetch.politeness import parse_retry_after
from tests.support import FakeClock

Route = httpx.Response | list[httpx.Response] | Callable[[httpx.Request], httpx.Response]


class FakeSite:
    """A fake website: canned responses per URL (a list is served in order, the last
    one repeating), and a log of every request received."""

    def __init__(self, routes: dict[str, Route]) -> None:
        self.routes = routes
        self.log: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        route = self.routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        if callable(route):
            return route(request)
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route

    def paths(self) -> list[str]:
        return [request.url.path for request in self.log]


def fetcher(site: FakeSite, clock: FakeClock, **kwargs: Any) -> HttpFetcher:
    options: dict[str, Any] = {"rps": 1.0, "rng": random.Random(0)} | kwargs
    return HttpFetcher(
        transport=httpx.MockTransport(site), clock=clock, sleep=clock.sleep, **options
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


PAGE = "https://example.org/page"


def ok(text: str = "<h1>hi</h1>") -> httpx.Response:
    return httpx.Response(200, text=text, headers={"content-type": "text/html"})


# --- identity -------------------------------------------------------------------------


def test_identifies_itself(clock: FakeClock) -> None:
    site = FakeSite({PAGE: ok()})
    response = fetcher(site, clock).fetch(PAGE)
    assert (response.status, response.body) == (200, b"<h1>hi</h1>")
    assert site.log[-1].headers["user-agent"] == DEFAULT_USER_AGENT
    assert "text/html" in site.log[-1].headers["accept"]
    assert DEFAULT_USER_AGENT.startswith(f"scrapemend/{__version__} (+https://")


def test_custom_user_agent(clock: FakeClock) -> None:
    site = FakeSite({PAGE: ok()})
    fetcher(site, clock, user_agent="acme-bot/1.0 (+mailto:ops@example.org)").fetch(PAGE)
    assert site.log[-1].headers["user-agent"] == "acme-bot/1.0 (+mailto:ops@example.org)"


# --- statuses and retries -------------------------------------------------------------


def test_404_is_returned_not_retried(clock: FakeClock) -> None:
    site = FakeSite({})
    assert fetcher(site, clock).fetch(PAGE).status == 404
    assert site.paths() == ["/robots.txt", "/page"]


def test_retries_5xx_with_backoff(clock: FakeClock) -> None:
    site = FakeSite({PAGE: [httpx.Response(503), httpx.Response(502), ok()]})
    f = fetcher(site, clock)
    assert f.fetch(PAGE).status == 200
    assert f.retries == 2
    throttle, first, second = clock.sleeps
    assert throttle == 1.0  # rate limit after robots.txt
    assert 1 <= first < 2 and 2 <= second < 3  # backoff 1s, 2s, plus up to 1s jitter


def test_gives_up_after_max_attempts(clock: FakeClock) -> None:
    site = FakeSite({PAGE: httpx.Response(503)})
    with pytest.raises(TransientError, match="HTTP 503 after 4 attempts"):
        fetcher(site, clock).fetch(PAGE)
    assert site.paths().count("/page") == 4


def test_retries_connection_errors(clock: FakeClock) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    site = FakeSite({PAGE: refuse})
    with pytest.raises(TransientError, match="ConnectError after 4 attempts"):
        fetcher(site, clock).fetch(PAGE)


def test_429_waits_exactly_retry_after_then_slows_down(clock: FakeClock) -> None:
    site = FakeSite({PAGE: [httpx.Response(429, headers={"retry-after": "7"}), ok(), ok()]})
    f = fetcher(site, clock, rps=1.0)
    f.fetch(PAGE)
    assert clock.sleeps == [1.0, 7.0]  # 1s after robots.txt, then Retry-After
    f.fetch(PAGE)
    assert clock.sleeps[-1] == 2.0  # the host's interval doubled after the 429


def test_refuses_absurd_retry_after(clock: FakeClock) -> None:
    site = FakeSite({PAGE: httpx.Response(429, headers={"retry-after": "3600"})})
    with pytest.raises(TransientError, match="asks to wait 3600s"):
        fetcher(site, clock).fetch(PAGE)
    assert 3600 not in clock.sleeps


@pytest.mark.parametrize("status", [401, 403])
def test_blocked_is_never_retried(clock: FakeClock, status: int) -> None:
    site = FakeSite({PAGE: httpx.Response(status)})
    with pytest.raises(BlockedError, match=f"HTTP {status}"):
        fetcher(site, clock).fetch(PAGE)
    assert site.paths().count("/page") == 1


def test_parse_retry_after() -> None:
    now = datetime(2026, 10, 13, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("120") == 120
    assert parse_retry_after("Tue, 13 Oct 2026 12:00:30 GMT", now) == 30
    assert parse_retry_after("Tue, 13 Oct 2026 11:00:00 GMT", now) == 0  # in the past
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


# --- rate limiting --------------------------------------------------------------------


def test_spaces_requests_to_the_same_host(clock: FakeClock) -> None:
    site = FakeSite({PAGE: ok()})
    f = fetcher(site, clock, rps=2.0)
    for _ in range(3):
        f.fetch(PAGE)
    assert clock.sleeps == [0.5, 0.5, 0.5]  # robots.txt, then three pages


def test_hosts_are_limited_independently(clock: FakeClock) -> None:
    site = FakeSite({"https://a.example/x": ok(), "https://b.example/x": ok()})
    f = fetcher(site, clock, rps=1.0)
    f.fetch("https://a.example/x")
    f.fetch("https://b.example/x")
    assert clock.sleeps == [1.0, 1.0]  # each host only waits after its own robots.txt


# --- robots.txt -----------------------------------------------------------------------

ROBOTS = "https://example.org/robots.txt"


def robots(text: str) -> httpx.Response:
    return httpx.Response(200, text=text)


def test_obeys_disallow(clock: FakeClock) -> None:
    site = FakeSite({ROBOTS: robots("User-agent: *\nDisallow: /private/\n")})
    f = fetcher(site, clock)
    with pytest.raises(RobotsDisallowed):
        f.fetch("https://example.org/private/x")
    assert f.fetch("https://example.org/public").status == 404
    assert site.paths() == ["/robots.txt", "/public"]  # read once, private never requested


@pytest.mark.parametrize(
    ("rules", "allowed"),
    [
        ("User-agent: scrapemend\nDisallow: /\n", False),
        ("User-agent: otherbot\nDisallow: /\n", True),
    ],
)
def test_robots_groups_by_user_agent(clock: FakeClock, rules: str, allowed: bool) -> None:
    site = FakeSite({ROBOTS: robots(rules), PAGE: ok()})
    f = fetcher(site, clock)
    if allowed:
        assert f.fetch(PAGE).status == 200
    else:
        with pytest.raises(RobotsDisallowed):
            f.fetch(PAGE)


@pytest.mark.parametrize("status", [404, 403, 410])
def test_missing_robots_means_no_rules(clock: FakeClock, status: int) -> None:
    site = FakeSite({ROBOTS: httpx.Response(status), PAGE: ok()})
    assert fetcher(site, clock).fetch(PAGE).status == 200


def test_unreachable_robots_means_no_crawling(clock: FakeClock) -> None:
    site = FakeSite({ROBOTS: httpx.Response(503), PAGE: ok()})
    with pytest.raises(TransientError, match=r"robots\.txt unreachable"):
        fetcher(site, clock).fetch(PAGE)
    assert "/page" not in site.paths()


def test_crawl_delay_slows_us_down(clock: FakeClock) -> None:
    site = FakeSite({ROBOTS: robots("User-agent: *\nCrawl-delay: 5\n"), PAGE: ok()})
    f = fetcher(site, clock, rps=10.0)
    f.fetch(PAGE)
    f.fetch(PAGE)
    assert clock.sleeps == [5.0, 5.0]


# --- redirects ------------------------------------------------------------------------


def redirect(location: str) -> httpx.Response:
    return httpx.Response(301, headers={"location": location})


def test_follows_redirects(clock: FakeClock) -> None:
    site = FakeSite({PAGE: redirect("/moved"), "https://example.org/moved": ok("moved")})
    response = fetcher(site, clock).fetch(PAGE)
    assert (response.url, response.body) == ("https://example.org/moved", b"moved")


def test_redirect_targets_obey_robots(clock: FakeClock) -> None:
    site = FakeSite(
        {ROBOTS: robots("User-agent: *\nDisallow: /private/"), PAGE: redirect("/private/x")}
    )
    with pytest.raises(RobotsDisallowed):
        fetcher(site, clock).fetch(PAGE)
    assert "/private/x" not in site.paths()


def test_redirect_loop(clock: FakeClock) -> None:
    site = FakeSite({PAGE: redirect(PAGE)})
    with pytest.raises(FetchError, match="more than 5 redirects"):
        fetcher(site, clock).fetch(PAGE)


@pytest.mark.parametrize("url", ["ftp://example.org/x", "file:///etc/passwd", "/relative"])
def test_only_http_urls(clock: FakeClock, url: str) -> None:
    with pytest.raises(FetchError, match="not an http"):
        fetcher(FakeSite({}), clock).fetch(url)
