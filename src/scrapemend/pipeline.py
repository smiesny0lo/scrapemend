"""One run of one source: listing pages -> items -> detail pages -> records.

Every response is stored as a snapshot before it is parsed. A detail page that
fails is skipped and counted; a blocked site, a broken listing page or a streak of
failures aborts the run, because carrying on would only hammer a struggling site.
"""

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field

from scrapemend.extract import Document, Extractor, RecordResult
from scrapemend.extract.select import find_link, select_all
from scrapemend.fetch import BlockedError, Fetcher, FetchError
from scrapemend.runs import new_run_id
from scrapemend.snapshots import Snapshot, SnapshotKind, SnapshotStore
from scrapemend.spec import SourceSpec

MAX_CONSECUTIVE_FAILURES = 5


class RunAborted(Exception):
    """The run stopped early; the records yielded so far are still valid."""


@dataclass
class ItemFailure:
    url: str | None
    reason: str


@dataclass
class RunSummary:
    source_id: str
    spec_version: int
    run_id: str
    listing_pages: int = 0
    items: int = 0
    records: int = 0
    failures: list[ItemFailure] = field(default_factory=list)
    fallbacks: Counter[str] = field(default_factory=Counter)  # field -> records won by a fallback
    empty: Counter[str] = field(default_factory=Counter)  # field -> records without a value
    errors: Counter[str] = field(default_factory=Counter)  # field -> text found but unusable


class SourceRun:
    def __init__(
        self,
        spec: SourceSpec,
        fetcher: Fetcher,
        store: SnapshotStore,
        *,
        run_id: str | None = None,
        limit: int | None = None,
    ) -> None:
        self.spec = spec
        self.fetcher = fetcher
        self.store = store
        self.limit = limit
        self.summary = RunSummary(spec.id, spec.version, run_id or new_run_id())
        self._extractor = Extractor(spec.fields)
        self._failure_streak = 0

    def records(self) -> Iterator[RecordResult]:
        """Crawl the source, yielding each record as soon as it is extracted."""
        url: str | None = str(self.spec.fetch.start)
        paginate = self.spec.fetch.paginate
        max_pages = paginate.max_pages if paginate else 1
        visited: set[str] = set()

        while url and url not in visited and self.summary.listing_pages < max_pages:
            visited.add(url)
            listing = self._fetch(url, "listing")
            if listing.status != 200:
                raise RunAborted(f"listing page returned HTTP {listing.status}: {url}")
            self.summary.listing_pages += 1
            page = listing.document()

            for item in select_all(self.spec.item.container, page):
                if self.limit is not None and self.summary.items >= self.limit:
                    return
                self.summary.items += 1
                result = self._item(item)
                if result is not None:
                    self._count(result)
                    yield result

            url = find_link(paginate.next, page) if paginate else None

    def _item(self, item: Document) -> RecordResult | None:
        follow = self.spec.item.follow
        if follow is None:
            return self._extractor.extract(item)
        link = find_link(follow, item)
        if link is None:
            self._fail(None, "item has no detail link")
            return None
        try:
            detail = self._fetch(link, "detail")
        except BlockedError:
            raise
        except FetchError as e:
            self._fail(link, e.reason)
            return None
        if detail.status != 200:
            self._fail(link, f"HTTP {detail.status}")
            return None
        self._failure_streak = 0
        return self._extractor.extract(detail.document())

    def _fetch(self, url: str, kind: SnapshotKind) -> Snapshot:
        response = self.fetcher.fetch(url)
        snapshot = Snapshot(
            source_id=self.spec.id,
            run_id=self.summary.run_id,
            spec_version=self.spec.version,
            kind=kind,
            url=response.url,
            status=response.status,
            headers=response.headers,
            body=response.body,
        )
        self.store.put(snapshot)
        return snapshot

    def _fail(self, url: str | None, reason: str) -> None:
        self.summary.failures.append(ItemFailure(url, reason))
        self._failure_streak += 1
        if self._failure_streak >= MAX_CONSECUTIVE_FAILURES:
            raise RunAborted(f"{MAX_CONSECUTIVE_FAILURES} items in a row failed; last: {reason}")

    def _count(self, result: RecordResult) -> None:
        self.summary.records += 1
        for name, value in result.fields.items():
            if value.used_fallback:
                self.summary.fallbacks[name] += 1
            if value.value is None:
                self.summary.empty[name] += 1
            if value.error is not None:
                self.summary.errors[name] += 1
