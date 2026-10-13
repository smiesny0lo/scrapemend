"""Fetching: the Fetcher protocol, what it returns, and the ways it fails."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Response:
    url: str  # final URL, after redirects
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)


class FetchError(Exception):
    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"{reason}: {url}")
        self.url = url
        self.reason = reason


class TransientError(FetchError):
    """Timeouts, connection errors, 5xx, 429: may work later. Raised once retries run out."""


class BlockedError(FetchError):
    """401/403: the site refuses us. Never retried or worked around: stop and alert."""


class RobotsDisallowed(FetchError):
    """robots.txt asks crawlers like us not to fetch this URL."""


class Fetcher(Protocol):
    def fetch(self, url: str) -> Response:
        """Fetch a URL politely. Returns any response that isn't an error worth
        retrying (including 404); raises FetchError subclasses otherwise."""
        ...
