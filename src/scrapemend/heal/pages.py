"""Pages under the current layout, for diagnosing and verifying repairs."""

from typing import Protocol

from scrapemend.extract import Document
from scrapemend.fetch import BlockedError, Fetcher, FetchError
from scrapemend.snapshots import Snapshot, SnapshotKind, SnapshotStore


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


class PageSource:
    """The pages a run stored, plus live fetches (politely, through the given fetcher)
    of pages it didn't fetch. Live pages are stored as snapshots of the same run, so
    a verification can be reproduced later without fetching again."""

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
        self._docs: dict[tuple[SnapshotKind, str], Document | None] = {}
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
        return self._get("listing", url)

    def page(self, url: str) -> Document | None:
        """A detail page: stored if the run has it, else fetched now; None if it fails."""
        return self._get("detail", url)

    def _get(self, kind: SnapshotKind, url: str) -> Document | None:
        if (kind, url) not in self._docs:
            self._docs[kind, url] = self._load(kind, url)
        return self._docs[kind, url]

    def _load(self, kind: SnapshotKind, url: str) -> Document | None:
        if (kind, url) in self._stored:
            return self.snapshots.get(self._stored[kind, url]).document()
        if self.fetcher is None:
            return None
        try:
            response = self.fetcher.fetch(url)
        except BlockedError:
            raise
        except FetchError:
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
        self._stored[kind, url] = self.snapshots.put(snapshot)
        return snapshot.document() if response.status == 200 else None
