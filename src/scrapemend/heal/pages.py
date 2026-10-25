"""Pages under the current layout, for diagnosing and verifying repairs."""

from typing import Protocol

from scrapemend.extract import Document
from scrapemend.fetch import BlockedError, Fetcher, FetchError
from scrapemend.snapshots import Snapshot, SnapshotStore


class Pages(Protocol):
    def listing_pages(self) -> list[Document]: ...

    def page(self, url: str) -> Document | None: ...


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
        self._details: dict[str, str] = {}  # url -> snapshot id
        self._docs: dict[str, Document | None] = {}
        for snapshot_id in snapshots.snapshots(source_id, run_id):
            snapshot = snapshots.get(snapshot_id)
            if snapshot.status != 200:
                continue
            if snapshot.kind == "listing":
                self._listings.append(snapshot_id)
            else:
                self._details.setdefault(snapshot.url, snapshot_id)

    def listing_pages(self) -> list[Document]:
        return [self.snapshots.get(i).document() for i in self._listings]

    def page(self, url: str) -> Document | None:
        """A detail page: stored if the run has it, else fetched now; None if it fails."""
        if url not in self._docs:
            self._docs[url] = self._load(url)
        return self._docs[url]

    def _load(self, url: str) -> Document | None:
        if url in self._details:
            return self.snapshots.get(self._details[url]).document()
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
            kind="detail",
            url=response.url,
            status=response.status,
            headers=response.headers,
            body=response.body,
        )
        self._details[url] = self.snapshots.put(snapshot)
        return snapshot.document() if response.status == 200 else None
