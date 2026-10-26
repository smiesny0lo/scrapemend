"""Pages under the current layout, for diagnosing and verifying repairs."""

from dataclasses import dataclass
from typing import Protocol

from specmend.extract import Document
from specmend.fetch import BlockedError, Fetcher, FetchError, RobotsDisallowed
from specmend.snapshots import Snapshot, SnapshotKind, SnapshotStore


@dataclass(frozen=True)
class PageFailure:
    """Why a page didn't load."""

    reason: str  # e.g. "HTTP 404", "HTTP 503 after 4 attempts"
    # True: the page couldn't be fetched (timeouts, 5xx, no network), so nothing is
    # known about it. False: the site answered that it isn't there (404, 410, ...).
    unreachable: bool


class Pages(Protocol):
    def listing_pages(self) -> list[Document]:
        """The listing pages the failed run stored, its first page first."""
        ...

    def listing(self, url: str) -> Document | None:
        """A listing page by URL, or None if it doesn't load."""
        ...

    def page(self, url: str) -> Document | None:
        """A detail page by URL, or None if it doesn't load."""
        ...

    def failure(self, url: str) -> PageFailure | None:
        """Why a page that was asked for didn't load; None if it loaded."""
        ...

    def stored(self, kind: SnapshotKind) -> set[str]:
        """The URLs of the pages of a kind at hand without a request."""
        ...


class PageSource:
    """The pages a run stored, plus live fetches (politely, through the given fetcher)
    of pages it didn't fetch. Live pages are stored as snapshots of the same run, so
    a verification can be reproduced later without fetching again. A site that
    refuses us (BlockedError, or robots.txt: RobotsDisallowed) stops whoever asked:
    specmend never works around a block, so it isn't a page that merely failed.

    A page that fails to load isn't asked for again by the same source: a new
    PageSource tries it again."""

    def __init__(
        self,
        snapshots: SnapshotStore,
        source_id: str,
        run_id: str,
        *,
        fetcher: Fetcher | None = None,
        spec_version: int = 0,
    ) -> None:
        self.snapshots = snapshots
        self.source_id = source_id
        self.run_id = run_id
        self.fetcher = fetcher
        self.spec_version = spec_version
        self.fetched = 0  # live fetches made
        self._listings: list[str] = []
        self._stored: dict[tuple[SnapshotKind, str], str] = {}  # (kind, url) -> snapshot id
        self._listing_docs: dict[str, Document] = {}  # a crawl's few listing pages, parsed
        self._failures: dict[str, PageFailure] = {}
        for snapshot_id in snapshots.snapshots(source_id, run_id):
            snapshot = snapshots.get(snapshot_id)
            if snapshot.status != 200:
                continue
            if snapshot.kind == "listing":
                self._listings.append(snapshot_id)
            self._stored.setdefault((snapshot.kind, snapshot.url), snapshot_id)

    def listing_pages(self) -> list[Document]:
        """The listing pages stored for the run when this source was made, in the
        order they were fetched: the run's own, then any an earlier verification added."""
        return [self.snapshots.get(i).document() for i in self._listings]

    def listing(self, url: str) -> Document | None:
        """A listing page: stored if the run has it, else fetched now; None if it fails."""
        if url not in self._listing_docs:
            doc = self._load("listing", url)
            if doc is None:
                return None
            self._listing_docs[url] = doc
        return self._listing_docs[url]

    def page(self, url: str) -> Document | None:
        """A detail page: stored if the run has it, else fetched now; None if it fails.
        Parsed again on every call, so a crawl of thousands of pages keeps none."""
        return self._load("detail", url)

    def failure(self, url: str) -> PageFailure | None:
        return self._failures.get(url)

    def stored(self, kind: SnapshotKind) -> set[str]:
        return {url for stored_kind, url in self._stored if stored_kind == kind}

    def _load(self, kind: SnapshotKind, url: str) -> Document | None:
        if url in self._failures:
            return None
        if (kind, url) in self._stored:
            return self.snapshots.get(self._stored[kind, url]).document()
        if self.fetcher is None:
            self._failures[url] = PageFailure("not stored, and no fetcher", unreachable=True)
            return None
        try:
            response = self.fetcher.fetch(url)
        except (BlockedError, RobotsDisallowed):
            raise
        except FetchError as e:
            self._failures[url] = PageFailure(e.reason, unreachable=True)
            return None
        self.fetched += 1
        snapshot = Snapshot(
            source_id=self.source_id,
            run_id=self.run_id,
            spec_version=self.spec_version,
            kind=kind,
            url=response.url,
            status=response.status,
            headers=response.headers,
            body=response.body,
        )
        snapshot_id = self.snapshots.put(snapshot)
        if response.status != 200:
            self._failures[url] = PageFailure(f"HTTP {response.status}", unreachable=False)
            return None
        self._stored[kind, url] = snapshot_id
        return snapshot.document()
