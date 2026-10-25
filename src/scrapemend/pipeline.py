"""One crawl of one source: listing pages -> items -> detail pages -> records.

Every response is stored as a snapshot before it is parsed. A detail page that
fails is skipped and counted; a blocked site, a broken listing page or a streak of
failures aborts the crawl, because carrying on would only hammer a struggling site.

What a crawl found is judged afterwards, by `scrapemend.runner`.
"""

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, Field

from scrapemend.detect.fingerprint import fingerprint
from scrapemend.extract import Document, Extractor, FieldResult, RecordResult, Value
from scrapemend.extract.select import find_link, select_all
from scrapemend.fetch import BlockedError, Fetcher, FetchError, TransientError
from scrapemend.ids import new_run_id
from scrapemend.snapshots import Snapshot, SnapshotKind, SnapshotStore
from scrapemend.spec import FieldSpec, SourceSpec
from scrapemend.validate import DISQUALIFYING, Violation, validate_record

MAX_CONSECUTIVE_FAILURES = 5
FINGERPRINTS_PER_KIND = 3  # pages per kind whose fingerprint the run summary keeps


class RunAborted(Exception):
    """The crawl stopped early; the records yielded so far are still valid."""

    def __init__(self, reason: str, listing_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.listing_status = listing_status  # set when a listing page answered with an error


class ItemFailure(BaseModel):
    url: str | None
    reason: str
    status: int | None = None  # HTTP status, when the page answered
    transient: bool = False  # the request kept failing (timeouts, 5xx, 429)


class FieldStats(BaseModel):
    filled: int = 0
    empty: int = 0
    unusable: int = 0  # text found but not convertible to the field's type
    invalid: int = 0  # record validation failures (missing required, wrong format)
    winners: dict[int, int] = Field(default_factory=dict)  # strategy index -> records won

    @property
    def fallback_wins(self) -> int:
        return sum(n for index, n in self.winners.items() if index > 0)

    def add(self, field: FieldResult, violations: Iterable[Violation]) -> None:
        if field.value is None:
            self.empty += 1
        else:
            self.filled += 1
        if field.error is not None:
            self.unusable += 1
        if any(v.kind in DISQUALIFYING for v in violations):
            self.invalid += 1
        if field.strategy is not None:
            self.winners[field.strategy] = self.winners.get(field.strategy, 0) + 1


class RunSummary(BaseModel):
    source_id: str
    spec_version: int
    run_id: str
    listing_pages: int = 0
    items: int = 0
    records: int = 0  # extracted, valid or not
    invalid_records: int = 0
    failures: list[ItemFailure] = Field(default_factory=list)
    fields: dict[str, FieldStats] = Field(default_factory=dict)
    fingerprints: dict[str, list[str]] = Field(default_factory=dict)  # kind -> first pages

    @property
    def valid_records(self) -> int:
        return self.records - self.invalid_records

    @property
    def fallbacks(self) -> dict[str, int]:
        return {n: s.fallback_wins for n, s in self.fields.items() if s.fallback_wins}

    @property
    def empty(self) -> dict[str, int]:
        return {n: s.empty for n, s in self.fields.items() if s.empty}

    @property
    def errors(self) -> dict[str, int]:
        return {n: s.unusable for n, s in self.fields.items() if s.unusable}

    def null_rate(self, name: str) -> float:
        return self.fields[name].empty / self.records if self.records else 0.0


@dataclass(frozen=True)
class ValidatedRecord:
    result: RecordResult
    violations: tuple[Violation, ...]
    snapshot_id: str | None = None  # the stored page the record was read from

    @property
    def url(self) -> str | None:
        return self.result.url

    @property
    def record(self) -> dict[str, Value]:
        return self.result.record

    @property
    def fields(self) -> Mapping[str, FieldResult]:
        return self.result.fields

    @property
    def valid(self) -> bool:
        return not any(v.kind in DISQUALIFYING for v in self.violations)


def tally(
    records: Iterable[ValidatedRecord], fields: Mapping[str, FieldSpec]
) -> dict[str, FieldStats]:
    """Per-field statistics over some records."""
    stats = {name: FieldStats() for name in fields}
    for record in records:
        for name, field in record.fields.items():
            stats[name].add(field, (v for v in record.violations if v.field == name))
    return stats


@dataclass(frozen=True)
class Page:
    snapshot: Snapshot
    doc: Document
    snapshot_id: str


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
        self.summary = RunSummary(
            source_id=spec.id,
            spec_version=spec.version,
            run_id=run_id or new_run_id(),
            fields={name: FieldStats() for name in spec.fields},
        )
        self.first_listing: Document | None = None
        self.extractor = Extractor(spec.fields)
        self._failure_streak = 0

    def records(self) -> Iterator[ValidatedRecord]:
        """Crawl the source, yielding each record (valid or not) as soon as it is extracted."""
        url: str | None = str(self.spec.fetch.start)
        paginate = self.spec.fetch.paginate
        max_pages = paginate.max_pages if paginate else 1
        visited: set[str] = set()

        while url and url not in visited and self.summary.listing_pages < max_pages:
            visited.add(url)
            listing = self._fetch(url, "listing")
            if listing.snapshot.status != 200:
                raise RunAborted(
                    f"listing page returned HTTP {listing.snapshot.status}: {url}",
                    listing_status=listing.snapshot.status,
                )
            self.summary.listing_pages += 1
            if self.first_listing is None:
                self.first_listing = listing.doc

            for item in select_all(self.spec.item.container, listing.doc):
                if self.limit is not None and self.summary.items >= self.limit:
                    return
                self.summary.items += 1
                record = self._item(item)
                if record is not None:
                    self._count(record)
                    yield record

            url = find_link(paginate.next, listing.doc) if paginate else None

    def probe(self, urls: Iterable[str]) -> list[tuple[str, ValidatedRecord]]:
        """Fetch and extract detail pages outside the crawl, e.g. pages known from an
        earlier run when this run's listing finds no items. Failures are skipped."""
        found = []
        for url in urls:
            try:
                page = self._fetch(url, "detail")
            except BlockedError:
                raise
            except FetchError:
                continue
            if page.snapshot.status == 200:
                record = self.validate(self.extractor.extract(page.doc), page.snapshot_id)
                found.append((url, record))
        return found

    def validate(self, result: RecordResult, snapshot_id: str | None = None) -> ValidatedRecord:
        violations = tuple(validate_record(result, self.spec.fields))
        return ValidatedRecord(result, violations, snapshot_id)

    def _item(self, item: Document) -> ValidatedRecord | None:
        follow = self.spec.item.follow
        if follow is None:
            return self.validate(self.extractor.extract(item))
        link = find_link(follow, item)
        if link is None:
            self._fail(ItemFailure(url=None, reason="item has no detail link"))
            return None
        try:
            detail = self._fetch(link, "detail")
        except BlockedError:
            raise
        except FetchError as e:
            transient = isinstance(e, TransientError)
            self._fail(ItemFailure(url=link, reason=e.reason, transient=transient))
            return None
        if detail.snapshot.status != 200:
            status = detail.snapshot.status
            self._fail(ItemFailure(url=link, reason=f"HTTP {status}", status=status))
            return None
        self._failure_streak = 0
        return self.validate(self.extractor.extract(detail.doc), detail.snapshot_id)

    def _fetch(self, url: str, kind: SnapshotKind) -> Page:
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
        doc = snapshot.document()
        snapshot = snapshot.model_copy(update={"fingerprint": fingerprint(doc)})
        if snapshot.status == 200:
            kept = self.summary.fingerprints.setdefault(kind, [])
            if len(kept) < FINGERPRINTS_PER_KIND and snapshot.fingerprint:
                kept.append(snapshot.fingerprint)
        return Page(snapshot, doc, self.store.put(snapshot))

    def _fail(self, failure: ItemFailure) -> None:
        self.summary.failures.append(failure)
        self._failure_streak += 1
        if self._failure_streak >= MAX_CONSECUTIVE_FAILURES:
            raise RunAborted(
                f"{MAX_CONSECUTIVE_FAILURES} items in a row failed; last: {failure.reason}"
            )

    def _count(self, record: ValidatedRecord) -> None:
        self.summary.records += 1
        if not record.valid:
            self.summary.invalid_records += 1
        for name, field in record.fields.items():
            self.summary.fields[name].add(field, (v for v in record.violations if v.field == name))
