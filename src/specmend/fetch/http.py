"""The default fetcher: plain HTTP with httpx, polite by default."""

import random
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from specmend.fetch.base import (
    BlockedError,
    FetchError,
    Response,
    RobotsDisallowed,
    TransientError,
)
from specmend.fetch.politeness import (
    BLOCKED_STATUSES,
    DEFAULT_USER_AGENT,
    RETRY_STATUSES,
    Throttle,
    parse_retry_after,
)
from specmend.spec.models import FetchSpec

MAX_REDIRECTS = 5
ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"


class HttpFetcher:
    """Fetches pages over HTTP, obeying robots.txt and a per-host rate limit.

    - robots.txt is checked before every request, redirect targets included. A
      missing one (4xx) means no rules; an unreachable one (5xx) means no crawling.
    - Timeouts, connection errors, 408/429/5xx are retried with exponential backoff
      and jitter, or after exactly as long as Retry-After asks.
    - 401/403 raise BlockedError at once: blocking is never retried or evaded.

    Note: urllib's robots.txt parser applies the first matching rule rather than the
    most specific one, which differs from RFC 9309 for some Allow/Disallow overlaps.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        rps: float = 1.0,
        timeout: float = 30.0,
        max_attempts: int = 4,
        backoff: float = 1.0,
        max_wait: float = 120.0,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_attempts = max_attempts
        self.backoff = backoff
        self.max_wait = max_wait
        self.requests = 0  # HTTP requests sent, retries and robots.txt included
        self.retries = 0
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept": ACCEPT},
            timeout=timeout,
            follow_redirects=False,  # redirects are followed by hand, to check robots.txt
            transport=transport,
        )
        self._throttle = Throttle(rps, clock, sleep)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._robots: dict[str, RobotFileParser] = {}

    @classmethod
    def for_spec(cls, fetch: FetchSpec, **kwargs: Any) -> Self:
        return cls(rps=fetch.rate.rps, timeout=fetch.timeout, **kwargs)

    def fetch(self, url: str) -> Response:
        return self._get(url, obey_robots=True)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # --- internals --------------------------------------------------------------------

    def _get(self, url: str, obey_robots: bool) -> Response:
        for _ in range(MAX_REDIRECTS + 1):
            _require_http(url)
            if obey_robots:
                self._check_robots(url)
            response = self._request(url)
            location = response.headers.get("location")
            if response.is_redirect and location:
                url = urljoin(url, location)
                continue
            return Response(url, response.status_code, dict(response.headers), response.content)
        raise FetchError(url, f"more than {MAX_REDIRECTS} redirects")

    def _request(self, url: str) -> httpx.Response:
        host = urlsplit(url).netloc
        problem = ""
        for attempt in range(1, self.max_attempts + 1):
            self._throttle.wait(host)
            self.requests += 1
            retry_after = None
            try:
                response = self._client.get(url)
            except httpx.TransportError as e:  # timeouts and connection errors
                problem = type(e).__name__
            else:
                status = response.status_code
                if status in BLOCKED_STATUSES:
                    raise BlockedError(url, f"HTTP {status}")
                if status not in RETRY_STATUSES:
                    return response
                problem = f"HTTP {status}"
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                if status == 429:
                    self._throttle.slow_down(host)
            if attempt == self.max_attempts:
                break
            delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
            if delay > self.max_wait:
                raise TransientError(url, f"{problem}, server asks to wait {delay:.0f}s")
            self.retries += 1
            self._sleep(delay)
        raise TransientError(url, f"{problem} after {self.max_attempts} attempts")

    def _backoff_delay(self, attempt: int) -> float:
        """1s, 2s, 4s, ... plus up to 1s of jitter, so retries from many runs don't align."""
        delay = min(self.backoff * 2.0 ** (attempt - 1), self.max_wait)
        return delay + self._rng.uniform(0, self.backoff)

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        if not self._robots[origin].can_fetch(self.user_agent, url):
            raise RobotsDisallowed(url, "disallowed by robots.txt")

    def _load_robots(self, origin: str) -> RobotFileParser:
        rules = RobotFileParser(f"{origin}/robots.txt")
        try:
            response = self._get(f"{origin}/robots.txt", obey_robots=False)
        except BlockedError:
            rules.parse([])  # RFC 9309: any 4xx, 401/403 included, means no rules
            return rules
        except TransientError as e:
            raise TransientError(e.url, f"robots.txt unreachable ({e.reason})") from e
        if response.status >= 500:
            raise TransientError(response.url, f"robots.txt unreachable (HTTP {response.status})")
        if response.status >= 300:
            rules.parse([])
            return rules
        rules.parse(response.body.decode("utf-8", errors="replace").splitlines())
        delay = rules.crawl_delay(self.user_agent)
        if delay is not None:
            self._throttle.respect_crawl_delay(urlsplit(origin).netloc, float(delay))
        return rules


def _require_http(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise FetchError(url, "not an http(s) URL")
