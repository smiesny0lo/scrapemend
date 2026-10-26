"""Fetchers (httpx, optional Playwright) and politeness controls."""

from scrapemend.fetch.base import (
    BlockedError,
    Fetcher,
    FetchError,
    Requests,
    Response,
    RobotsDisallowed,
    TransientError,
)
from scrapemend.fetch.http import HttpFetcher
from scrapemend.fetch.politeness import DEFAULT_USER_AGENT

__all__ = [
    "DEFAULT_USER_AGENT",
    "BlockedError",
    "FetchError",
    "Fetcher",
    "HttpFetcher",
    "Requests",
    "Response",
    "RobotsDisallowed",
    "TransientError",
]
