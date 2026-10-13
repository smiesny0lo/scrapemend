"""Failure injection: the ways real sites misbehave towards scrapers.

- flaky:     a random share of requests fail with 503 (transient: retry with backoff)
- ratelimit: over RATE_LIMIT requests per RATE_WINDOW seconds get 429 + Retry-After
- blocked:   every request gets a 403 "automated traffic" page (stop: never evade)

Modes combine; blocked wins over ratelimit, which wins over flaky.
"""

import math
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from fastapi.responses import HTMLResponse, PlainTextResponse, Response

SEED = 2026
DEFAULT_FAILURE_RATE = 0.2
RATE_LIMIT = 10
RATE_WINDOW = 10.0  # seconds

UNAVAILABLE_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Service unavailable</title></head>
<body><h1>Service temporarily unavailable</h1><p>Please try again later.</p></body>
</html>
"""
BLOCKED_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Access denied</title></head>
<body>
  <h1>Access denied</h1>
  <p>Your request looks like automated traffic and has been blocked.</p>
  <p>If you believe this is a mistake, contact the site administrator.</p>
</body>
</html>
"""


class Mode(StrEnum):
    FLAKY = "flaky"
    RATELIMIT = "ratelimit"
    BLOCKED = "blocked"


def parse_modes(raw: str) -> frozenset[Mode]:
    """Parse "flaky,ratelimit" style input. Empty or "off" means no chaos."""
    names = {part.strip().lower() for part in raw.split(",")} - {"", "off"}
    try:
        return frozenset(Mode(name) for name in names)
    except ValueError:
        choices = ", ".join(Mode)
        raise ValueError(f"unknown chaos mode in {raw!r}; use {choices} or off") from None


@dataclass
class Chaos:
    modes: frozenset[Mode] = frozenset()
    failure_rate: float = DEFAULT_FAILURE_RATE
    clock: Callable[[], float] = time.monotonic
    rng: random.Random = field(default_factory=lambda: random.Random(SEED))
    _hits: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not 0.0 <= self.failure_rate <= 1.0:
            raise ValueError(f"failure rate must be between 0 and 1, got {self.failure_rate}")

    def describe(self) -> dict[str, object]:
        return {"modes": sorted(self.modes), "failure_rate": self.failure_rate}

    def intercept(self) -> Response | None:
        """Return a failure response for this request, or None to serve it normally."""
        if Mode.BLOCKED in self.modes:
            return HTMLResponse(BLOCKED_PAGE, status_code=403)
        if Mode.RATELIMIT in self.modes and (wait := self._rate_limit_wait()):
            return PlainTextResponse(
                "Too many requests", status_code=429, headers={"Retry-After": str(wait)}
            )
        if Mode.FLAKY in self.modes and self.rng.random() < self.failure_rate:
            return HTMLResponse(UNAVAILABLE_PAGE, status_code=503)
        return None

    def _rate_limit_wait(self) -> int:
        """Count this request; return whole seconds to wait if over the limit, else 0."""
        now = self.clock()
        while self._hits and now - self._hits[0] >= RATE_WINDOW:
            self._hits.popleft()
        if len(self._hits) >= RATE_LIMIT:
            return max(1, math.ceil(self._hits[0] + RATE_WINDOW - now))
        self._hits.append(now)
        return 0
