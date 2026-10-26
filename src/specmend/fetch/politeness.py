"""Politeness: identifying ourselves, spacing out requests, and reading Retry-After."""

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from specmend import __version__

DEFAULT_USER_AGENT = f"specmend/{__version__} (+https://github.com/smiesny0lo/specmend)"

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
BLOCKED_STATUSES = frozenset({401, 403})
MAX_INTERVAL = 60.0  # seconds; the slowest a host is throttled to after repeated 429s


class Throttle:
    """Keeps at least `interval` seconds between requests to the same host.

    The interval starts at 1/rps, can only grow from a robots.txt Crawl-delay, and
    doubles after every 429 so a rate-limited run settles below the site's limit.
    """

    def __init__(self, rps: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self._base = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._interval: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def interval(self, host: str) -> float:
        return self._interval.get(host, self._base)

    def respect_crawl_delay(self, host: str, delay: float) -> None:
        self._interval[host] = max(self.interval(host), delay)

    def slow_down(self, host: str) -> None:
        self._interval[host] = min(self.interval(host) * 2, MAX_INTERVAL)

    def wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = last + self.interval(host) - self._clock()
            if remaining > 0:
                self._sleep(remaining)
        self._last[host] = self._clock()


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header: either "120" or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())
