"""Fetchers (httpx, optional Playwright) and politeness controls."""

from specmend.fetch.base import (
    BlockedError,
    Fetcher,
    FetchError,
    Requests,
    Response,
    RobotsDisallowed,
    TransientError,
)
from specmend.fetch.http import HttpFetcher
from specmend.fetch.politeness import DEFAULT_USER_AGENT

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
