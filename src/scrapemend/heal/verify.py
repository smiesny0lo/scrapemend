"""The verification gate: may a candidate spec replace the current one?

A candidate is judged on pages under the current layout, never on its own say-so.
Quick checks on a few pages come first, so most wrong repairs are turned down
without crawling the site:

1. Listing: does the item container still find items, and does every item it
   matches have its key (its detail link, or the key field), once? A container
   that also matches a header row fails here. The next-page link is followed for
   two pages (or until it ends, if the last good run had fewer): each page must
   load, pass the same item check and bring new items, so a link back to an
   earlier page, or one hard-coded to page 2, is caught.
2. Anchors: on the anchor pages (fetched again under the new layout), does every
   field reproduce the known-good values? Stable fields must match exactly on
   heal.threshold of the anchors: by default all of them. Volatile ones (prices,
   statuses) may have changed since, but at least heal.volatile_match of them must
   still match exactly. Every field is judged on its own, so one wrongly mapped
   field can't hide behind nine correct ones. Anchor pages that fail to load are
   left out, but at least three (or all, if fewer) must load. And no two fields may
   read the same text from the same element where their known values differ.
3. Sample: on other detail pages of the first listing page, are required fields
   filled, and do fields that differ between anchors still differ?
4. Known values: a field that took only a few values in the last good run (a
   status, a category) must mostly read values from that set, on anchor and sample
   pages alike.

Then the primary check, the shadow run. The candidate crawls the source as a run
would: every listing page along its next-page link (up to max_pages), every item,
every detail page. Pages the failed run or an earlier verification stored are
reused; the others are fetched politely and stored with the failed run, so the
same verdict can be reached again from the stored pages. Its records are compared
with the last good run's, by key:

- the shadow run must find MIN_COVERAGE of the last good run's records;
- on every record both runs have, a stable field must be identical (on
  heal.threshold of them: by default all);
- a volatile field may have changed on at most heal.volatile_change of them, and a
  value that became empty is never a change: it is a mismatch, judged like a
  stable field's;
- a field with a small set of values must mostly read values from that set, and
  required fields must be filled on records the last good run didn't have.

With heal.shadow_max_items, the shadow run reads a sample instead: listing pages
drawn at random (with a fixed seed) from the last good run's, the same pages'
item counts compared, and records drawn at random from their items.

Accepted only if there is no problem at all.
"""

import hashlib
import math
import random
from collections import Counter
from collections.abc import Iterator, Mapping
from itertools import combinations
from typing import Literal

from lxml import etree
from pydantic import BaseModel, Field

from scrapemend.extract import Document, Extractor, RecordResult
from scrapemend.extract.select import find_link, select_all
from scrapemend.extract.strategies import Locating, Strategy, build
from scrapemend.heal.anchors import Anchor, AnchorValue, KnownRecords, encode, key_of
from scrapemend.heal.pages import Pages
from scrapemend.pipeline import RunSummary
from scrapemend.spec import SourceSpec

MIN_ANCHORS = 3  # anchor pages that must load for a verdict (all of them, if fewer)
SAMPLE_PAGES = 10  # non-anchor detail pages checked for plausibility
PAGINATION_HOPS = 2  # next-page links followed from the first listing page
MAX_REPEATED_SHARE = 0.5  # of a listing page's items that may already have been seen
MIN_VALUES = 3  # values a field must read before they are checked against known ones
MAX_UNKNOWN_SHARE = 0.5  # of those, the share that may be new since the last good run
MIN_COVERAGE = 0.9  # of the last good run's records the shadow run must find again
MIN_SHADOW_RECORDS = 20  # records to compare (all of them, if the last good run had fewer)
SHADOW_SEED = "scrapemend-shadow"  # draws the shadow run's sample, when it samples
EXAMPLES = 3  # mismatches kept per field (and kind, in the shadow run)


class Mismatch(BaseModel):
    url: str | None
    expected: AnchorValue
    got: AnchorValue


class FieldCheck(BaseModel):
    matched: int = 0
    compared: int = 0
    volatile: bool = False
    mismatches: list[Mismatch] = Field(default_factory=list)  # the first few

    @property
    def rate(self) -> float:
        return self.matched / self.compared if self.compared else 0.0


class ShadowField(BaseModel):
    """One field over the records the shadow run and the last good run share."""

    volatile: bool = False
    compared: int = 0
    matched: int = 0
    changed: int = 0  # another value now
    emptied: int = 0  # a value in the last good run, none now: never a mere change
    mismatches: list[Mismatch] = Field(default_factory=list)  # the first few of each kind

    def add(self, url: str | None, expected: AnchorValue, got: AnchorValue) -> None:
        self.compared += 1
        if expected == got:
            self.matched += 1
            return
        emptied = got is None
        if emptied:
            self.emptied += 1
        else:
            self.changed += 1
        if sum((m.got is None) == emptied for m in self.mismatches) < EXAMPLES:
            self.mismatches.append(Mismatch(url=url, expected=expected, got=got))

    def example(self, emptied: bool) -> Mismatch:
        return next(m for m in self.mismatches if (m.got is None) == emptied)


class ShadowRun(BaseModel):
    """What the candidate's crawl of the source found."""

    scope: Literal["full", "sample"]
    listing_pages: int = 0  # listing pages read
    items: int = 0  # items the item container matched on them
    records: int = 0  # records read, each key once
    known: int = 0  # records the last good run had
    compared: int = 0  # records read whose key the last good run had
    sampled: list[str] = Field(default_factory=list)  # in a sample: the listing pages drawn
    fields: dict[str, ShadowField] = Field(default_factory=dict)


class VerificationReport(BaseModel):
    accepted: bool
    threshold: float  # share of anchor values a stable field must reproduce
    volatile_match: float = 0.6  # ... and a volatile one
    volatile_change: float = 0.1  # share of shadow-run records a volatile field may change on
    anchors: int  # anchors available
    anchors_checked: int  # anchor pages loaded and compared
    fields: dict[str, FieldCheck]
    listing_items: int | None = None  # items found on the run's first listing page
    listing_links: int | None = None  # of those, items with a key (their detail link)
    listing_pages: int | None = None  # listing pages seen: the first, then along the next link
    sample: int = 0  # non-anchor pages checked
    shadow: ShadowRun | None = None  # None: it didn't start
    problems: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)  # what limited the verdict, if anything

    @property
    def score(self) -> float:
        """The weakest field's share of reproduced anchor values."""
        return min((f.rate for f in self.fields.values()), default=0.0)


def verify(
    spec: SourceSpec,
    anchors: list[Anchor],
    pages: Pages,
    *,
    threshold: float | None = None,
    baseline: RunSummary | None = None,
    known: KnownRecords | None = None,
) -> VerificationReport:
    """`known` holds the values of the last good run's records, by key (see
    AnchorStore.records): what the shadow run is compared with."""
    threshold = spec.heal.threshold if threshold is None else threshold
    volatile_match = spec.heal.volatile_match
    extractor = Extractor(spec.fields)
    strategies = {name: [build(s) for s in f.strategies] for name, f in spec.fields.items()}
    problems: list[str] = []

    # 1. listing
    listing_items = listing_links = listing_pages = None
    first: Document | None = None
    items: list[tuple[str | None, Document]] = []
    rows = _Rows(spec, pages)  # listing-only specs: the items of listing pages, by key
    listings = pages.listing_pages()
    if listings:
        first = listings[0]
        items = _items(spec, first)
        listing_items = len(items)
        listing_links = sum(key is not None for key, _ in items)
        if not items:
            problems.append("the item container matches nothing on the listing page")
        problems += _item_problems(spec, items, 1)
        seen, walk_problems = _walk(spec, pages, first, items, baseline)
        listing_pages = len(seen)
        problems += walk_problems
        for page in seen:
            rows.add(page)
    else:
        problems.append("no listing page of the failed run to verify against")

    # 2. anchors
    checks = {name: FieldCheck(volatile=f.volatile) for name, f in spec.fields.items()}
    shared: Counter[tuple[str, str]] = Counter()  # fields read from one element -> pages
    checked: list[RecordResult] = []
    for anchor in anchors:
        if anchor.url is None:
            continue
        # Without detail pages, an anchor's URL is only the listing page it was on.
        doc = pages.page(anchor.url) if spec.item.follow else rows.find(anchor.key, anchor.url)
        if doc is None:
            continue
        result = extractor.extract(doc)
        checked.append(result)
        _compare(checks, anchor.values, result, anchor.url)
        shared.update(_shared_elements(strategies, anchor, result, doc))
    if not anchors:
        problems.append("no anchors to verify against: the source has had no good run yet")
    elif len(checked) < min(MIN_ANCHORS, len(anchors)):
        problems.append(
            f"only {len(checked)} of {len(anchors)} anchor pages could be loaded"
            if spec.item.follow
            else f"only {len(checked)} of {len(anchors)} anchors were found on their listing pages"
        )
    problems += _rate_problems(checks, threshold, volatile_match, "anchor values")
    for (a, b), count in shared.items():
        problems.append(
            f"{a} and {b} read the same element on {count} of {len(checked)} anchor pages"
        )

    # 3. sample: other items of the first listing page (their detail pages, if any)
    unit = "pages" if spec.item.follow else "items"
    anchored = {a.url for a in anchors} if spec.item.follow else {a.key for a in anchors}
    others = [(key, item) for key, item in items if key is not None and key not in anchored]
    sample: list[RecordResult] = []
    for key, item in others[:SAMPLE_PAGES]:
        doc = pages.page(key) if spec.item.follow else item
        if doc is not None:
            sample.append(extractor.extract(doc))
    problems += _sample_problems(spec, anchors, sample, unit)

    # 4. known values
    values = _KnownValues(spec, baseline)
    for result in checked + sample:
        values.add(result)
    problems += values.problems(unit)

    # 5. the shadow run
    notes: list[str] = []
    shadow = None
    if problems:
        notes.append("the shadow run was skipped: a quicker check failed")
    elif first is not None:
        shadow, shadow_problems, unavailable = _shadow(
            spec, pages, extractor, first, known or {}, baseline, threshold
        )
        problems += shadow_problems
        if unavailable is not None:
            notes.append(f"the shadow run could not run: {unavailable}")

    return VerificationReport(
        accepted=not problems,
        threshold=threshold,
        volatile_match=volatile_match,
        volatile_change=spec.heal.volatile_change,
        anchors=len(anchors),
        anchors_checked=len(checked),
        fields=checks,
        listing_items=listing_items,
        listing_links=listing_links,
        listing_pages=listing_pages,
        sample=len(sample),
        shadow=shadow,
        problems=problems,
        notes=notes,
    )


def _compare(
    checks: Mapping[str, FieldCheck],
    expected: Mapping[str, AnchorValue],
    result: RecordResult,
    url: str | None,
) -> None:
    """Count, per field, whether a record reproduces its known values."""
    for name, check in checks.items():
        if name not in expected:
            continue
        want, got = expected[name], encode(result.fields[name].value)
        check.compared += 1
        if want == got:
            check.matched += 1
        elif len(check.mismatches) < EXAMPLES:
            check.mismatches.append(Mismatch(url=url, expected=want, got=got))


def _rate_problems(
    checks: Mapping[str, FieldCheck], threshold: float, volatile_match: float, what: str
) -> list[str]:
    """Fields that reproduce too few known values: stable ones need `threshold` of
    them, volatile ones `volatile_match`."""
    problems = []
    for name, check in checks.items():
        needed = volatile_match if check.volatile else threshold
        if check.compared and check.rate < needed:
            example = check.mismatches[0]
            kind = " for a volatile field" if check.volatile else ""
            problems.append(
                f"{name} reproduces {check.matched}/{check.compared} {what} "
                f"(needs {needed:.0%}{kind}); expected {example.expected!r}, got {example.got!r}"
            )
    return problems


def _items(spec: SourceSpec, page: Document) -> list[tuple[str | None, Document]]:
    """The items on a listing page, each with its key: the link to its detail page,
    or, without detail pages, the value of the key field."""
    follow = spec.item.follow
    keys = None if follow else Extractor({spec.item.key: spec.fields[spec.item.key]})
    found: list[tuple[str | None, Document]] = []
    for item in select_all(spec.item.container, page):
        if follow is not None:
            key = find_link(follow, item)
        else:
            assert keys is not None
            value = encode(keys.extract(item).fields[spec.item.key].value)
            key = None if value is None else str(value)
        found.append((key, item))
    return found


def _walk(
    spec: SourceSpec,
    pages: Pages,
    first: Document,
    items: list[tuple[str | None, Document]],
    baseline: RunSummary | None,
) -> tuple[list[Document], list[str]]:
    """Follow the next-page link from the first listing page for PAGINATION_HOPS
    pages, or until it ends. Each page must load and bring items not seen on the
    pages before, every item on it must have its key, and the link must not end
    before the last good run's did. Returns the listing pages seen, the first
    included, and the problems found."""
    walked = [first]
    paginate = spec.fetch.paginate
    if paginate is None:
        return walked, []
    problems: list[str] = []
    seen = {key for key, _ in items if key}
    visited = {first.url}
    while len(walked) <= PAGINATION_HOPS:
        number = len(walked)
        link = find_link(paginate.next, walked[-1])
        if link is None:
            break
        if link in visited:
            problem = f"the next-page link on listing page {number} leads back to an earlier page"
            return walked, [*problems, problem]
        visited.add(link)
        page = pages.listing(link)
        if page is None:
            return walked, [*problems, f"listing page {number + 1} could not be loaded: {link}"]
        walked.append(page)
        items = _items(spec, page)
        keys = [key for key, _ in items if key]
        if not keys:
            problem = f"the item container matches nothing on listing page {number + 1}"
            return walked, [*problems, problem]
        repeated = sum(key in seen for key in keys)
        if repeated > MAX_REPEATED_SHARE * len(keys):
            problem = (
                f"the next-page link on listing page {number} leads to a page that repeats "
                f"{repeated} of {len(keys)} items already seen"
            )
            return walked, [*problems, problem]
        problems += _item_problems(spec, items, number + 1)
        seen.update(keys)
    expected = 1 if baseline is None else min(PAGINATION_HOPS + 1, baseline.listing_pages)
    if len(walked) < expected:
        assert baseline is not None
        problems.append(
            "no next-page link on the first listing page"
            if len(walked) == 1
            else f"pagination ends after {len(walked)} listing pages; "
            f"the last good run had {baseline.listing_pages}"
        )
    return walked, problems


def _item_problems(
    spec: SourceSpec, items: list[tuple[str | None, Document]], number: int
) -> list[str]:
    """Every item the container matches on a listing page must have its key (its
    detail link, or the key field), once: a container that also matches a header
    row, or an element inside each item, fails items on every page of every crawl."""
    where = "" if number == 1 else f" on listing page {number}"
    keys = [key for key, _ in items if key is not None]
    problems = []
    if len(keys) < len(items):
        what = "link to a detail page" if spec.item.follow else f"have a key ({spec.item.key})"
        problems.append(f"only {len(keys)} of {len(items)} items{where} {what}")
    if repeated := len(keys) - len(set(keys)):
        problems.append(
            f"{repeated} of {len(items)} items{where} repeat another item of the same page"
        )
    return problems


class _Rows:
    """For a spec without detail pages: the items of the listing pages seen so far,
    by key, to find an anchor's item in. An item may have moved to another page since
    the good run, so every page seen is searched before the anchor's own is loaded."""

    def __init__(self, spec: SourceSpec, pages: Pages) -> None:
        self.spec = spec
        self.pages = pages
        self.items: dict[str, Document] = {}
        self.loaded: set[str | None] = set()

    def add(self, page: Document) -> None:
        if self.spec.item.follow is not None:
            return
        self.loaded.add(page.url)
        for key, item in _items(self.spec, page):
            if key is not None:
                self.items.setdefault(key, item)

    def find(self, key: str, url: str) -> Document | None:
        if key not in self.items and url not in self.loaded:
            self.loaded.add(url)
            page = self.pages.listing(url)
            if page is not None:
                self.add(page)
        return self.items.get(key)


def _shared_elements(
    strategies: Mapping[str, list[Strategy]], anchor: Anchor, result: RecordResult, doc: Document
) -> list[tuple[str, str]]:
    """Pairs of fields that read the same text from the same element of an anchor's
    page, although the anchor's known values for them differ. Two fields may read
    different parts of one element, like a category and a location from one line."""
    read: dict[str, tuple[etree._Element, str]] = {}
    for name, found in result.fields.items():
        if found.strategy is None or found.raw is None:
            continue
        strategy = strategies[name][found.strategy]
        if isinstance(strategy, Locating) and (element := strategy.locate(doc)) is not None:
            read[name] = (element, found.raw)
    known = anchor.values
    return [
        (a, b)
        for a, b in combinations(read, 2)
        if read[a][0] is read[b][0]
        and read[a][1] == read[b][1]
        and a in known
        and b in known
        and known[a] != known[b]
    ]


def _sample_problems(
    spec: SourceSpec, anchors: list[Anchor], sample: list[RecordResult], unit: str
) -> list[str]:
    problems: list[str] = []
    if not sample:
        return problems
    for name, field in spec.fields.items():
        values = [encode(r.fields[name].value) for r in sample]
        filled = [v for v in values if v is not None]
        known = [a.values.get(name) for a in anchors]
        usually_filled = sum(v is not None for v in known) > len(known) / 2
        if field.required and len(filled) < len(values):
            problems.append(
                f"required field {name} is empty on {len(values) - len(filled)}"
                f" of {len(values)} other {unit}"
            )
        elif len(values) >= 3 and not filled and usually_filled:
            problems.append(f"{name} is empty on all {len(values)} other {unit}")
        distinct_known = {v for v in known if v is not None}
        if (
            not field.volatile
            and len(distinct_known) >= 2
            and len(filled) >= 3
            and len(set(filled)) == 1
        ):
            problems.append(
                f"{name} reads {filled[0]!r} on every other {unit.removesuffix('s')} although it "
                "differs between anchors: the strategy may only fit the examples"
            )
    return problems


class _KnownValues:
    """Fields that took only a few values in the last good run (a status, a category)
    must mostly read values from that set; a new one now and then is fine."""

    def __init__(self, spec: SourceSpec, baseline: RunSummary | None) -> None:
        self.known: dict[str, set[AnchorValue]] = {}
        for name in spec.fields if baseline is not None else ():
            assert baseline is not None
            stats = baseline.fields.get(name)
            if stats is not None and (values := stats.known_values) is not None:
                self.known[name] = values
        self.read: Counter[str] = Counter()
        self.unknown: Counter[str] = Counter()
        self.example: dict[str, AnchorValue] = {}

    def add(self, record: RecordResult) -> None:
        for name, known in self.known.items():
            value = encode(record.fields[name].value)
            if value is None:
                continue
            self.read[name] += 1
            if value not in known:
                self.unknown[name] += 1
                self.example.setdefault(name, value)

    def problems(self, unit: str) -> list[str]:
        problems = []
        for name, known in self.known.items():
            read, unknown = self.read[name], self.unknown[name]
            if read >= MIN_VALUES and unknown > MAX_UNKNOWN_SHARE * read:
                names = sorted(map(str, known))
                had = ", ".join(names[:8]) + (
                    f" and {len(names) - 8} more" if len(names) > 8 else ""
                )
                problems.append(
                    f"{name} reads values the last good run never had on {unknown} of "
                    f"{read} {unit} (e.g. {self.example[name]!r}; it had: {had})"
                )
        return problems


# --- the shadow run ------------------------------------------------------------------------


def _shadow(
    spec: SourceSpec,
    pages: Pages,
    extractor: Extractor,
    first: Document,
    known: KnownRecords,
    baseline: RunSummary | None,
    threshold: float,
) -> tuple[ShadowRun, list[str], str | None]:
    """Crawl the source with the candidate and compare its records with the last good
    run's. Returns what it found, the problems, and why it couldn't run, if it couldn't."""
    cap = spec.heal.shadow_max_items
    sampled = cap is not None and len(known) > cap
    run = ShadowRun(scope="sample" if sampled else "full", known=len(known))
    if not known:
        return run, [], "the last good run's records are not stored"
    crawl = _Crawl(spec, pages, extractor, run)
    comparison = _Comparison(spec, known, baseline, run)
    records = crawl.sample(cap, baseline) if cap is not None and sampled else crawl.all(first)
    for key, result in records:
        comparison.add(key, result)
    if crawl.unavailable is not None:
        return run, crawl.problems, crawl.unavailable

    problems = crawl.problems
    if sampled:
        if run.compared < MIN_COVERAGE * crawl.drawn:
            problems.append(
                f"only {run.compared} of the {crawl.drawn} sampled records are known from the "
                f"last good run (needs {MIN_COVERAGE:.0%}): the detail link or the key reads "
                "something else"
            )
    elif run.compared < MIN_COVERAGE * len(known):
        problems.append(
            f"the shadow run found {run.compared} of the last good run's {len(known)} records "
            f"(needs {MIN_COVERAGE:.0%}): the item container, the detail links or the "
            "pagination miss some"
        )
    problems += comparison.problems(threshold, spec.heal.volatile_change)
    needed = min(MIN_SHADOW_RECORDS, len(known))
    if not problems and run.compared < needed:
        return run, [], f"only {run.compared} records can be compared (needs {needed})"
    return run, problems, None


def _sample_rank(key: str) -> int:
    digest = hashlib.sha256(f"{SHADOW_SEED}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class _Crawl:
    """The candidate's crawl of the source: listing pages, their items, and each
    item's record. `unavailable` says why it stopped short, if it did: a page that
    could not be fetched, so nothing is known about it."""

    def __init__(
        self, spec: SourceSpec, pages: Pages, extractor: Extractor, run: ShadowRun
    ) -> None:
        self.spec = spec
        self.pages = pages
        self.extractor = extractor
        self.run = run
        self.problems: list[str] = []
        self.unavailable: str | None = None
        self.drawn = 0  # in a sample: the items drawn
        self._links: set[str] = set()

    def all(self, first: Document) -> Iterator[tuple[str, RecordResult]]:
        """Like a run: every listing page along the next-page link, up to max_pages."""
        paginate = self.spec.fetch.paginate
        max_pages = paginate.max_pages if paginate else 1
        page, number, visited = first, 1, {first.url}
        while True:
            yield from self._records(page, number)
            if self.unavailable is not None or paginate is None or number >= max_pages:
                return
            link = find_link(paginate.next, page)
            if link is None or link in visited:
                return
            visited.add(link)
            loaded = self._listing(link, number + 1, missing_is_wrong=True)
            if loaded is None:
                return
            page, number = loaded, number + 1

    def sample(self, cap: int, baseline: RunSummary | None) -> Iterator[tuple[str, RecordResult]]:
        """Listing pages drawn from the last good run's, and up to `cap` of their items."""
        listings = baseline.listings if baseline is not None else []
        if not listings:
            self.unavailable = (
                "the last good run's listing pages are not recorded, so none can be drawn; "
                "set heal.shadow_max_items to null to read the whole source"
            )
            return
        per_page = sum(p.items for p in listings) / len(listings)
        count = min(len(listings), math.ceil(2 * cap / max(per_page, 1.0)))
        drawn = sorted(
            random.Random(f"{SHADOW_SEED}:{self.spec.id}").sample(range(len(listings)), count)
        )
        pool: dict[str, Document] = {}
        old = found = 0
        for index in drawn:
            listing = listings[index]
            page = self._listing(listing.url, index + 1, missing_is_wrong=False)
            if page is None:
                return
            self.run.sampled.append(listing.url)
            items = self._listed(page, index + 1)
            old += listing.items
            found += len(items)
            for key, item in items:
                if key is not None:
                    pool.setdefault(key, item)
        if found < MIN_COVERAGE * old:
            self.problems.append(
                f"the item container finds {found} items on {count} sampled listing pages, "
                f"where the last good run found {old} (needs {MIN_COVERAGE:.0%})"
            )
        chosen = sorted(pool, key=_sample_rank)[:cap]
        self.drawn = len(chosen)
        for key in chosen:
            record = self._record(key, pool[key])
            if self.unavailable is not None:
                return
            if record is not None:
                yield record

    def _records(self, page: Document, number: int) -> Iterator[tuple[str, RecordResult]]:
        for key, item in self._listed(page, number):
            if key is None:
                continue
            record = self._record(key, item)
            if self.unavailable is not None:
                return
            if record is not None:
                yield record

    def _listed(self, page: Document, number: int) -> list[tuple[str | None, Document]]:
        """A listing page's items, counted and checked like the first page's."""
        items = _items(self.spec, page)
        self.run.listing_pages += 1
        self.run.items += len(items)
        self.problems += _item_problems(self.spec, items, number)
        return items

    def _listing(self, url: str, number: int, *, missing_is_wrong: bool) -> Document | None:
        page = self.pages.listing(url)
        if page is None:
            failure = self.pages.failure(url)
            reason = failure.reason if failure else "it did not load"
            if missing_is_wrong and failure is not None and not failure.unreachable:
                self.problems.append(f"listing page {number} is missing ({reason}): {url}")
            else:
                self.unavailable = f"listing page {number} could not be loaded ({reason}): {url}"
        return page

    def _record(self, key: str, item: Document) -> tuple[str, RecordResult] | None:
        """An item's record and its key, or None if its detail page is gone."""
        if self.spec.item.follow is None:
            return key, self.extractor.extract(item)
        if key in self._links:
            return None
        self._links.add(key)
        doc = self.pages.page(key)
        if doc is None:
            failure = self.pages.failure(key)
            if failure is None or failure.unreachable:
                reason = failure.reason if failure else "it did not load"
                self.unavailable = f"{key} could not be loaded ({reason})"
            return None
        result = self.extractor.extract(doc)
        found = key_of(result, self.spec.item.key)
        return None if found is None else (found, result)


class _Comparison:
    """The shadow run's records against the last good run's, by key."""

    def __init__(
        self,
        spec: SourceSpec,
        known: KnownRecords,
        baseline: RunSummary | None,
        run: ShadowRun,
    ) -> None:
        self.spec = spec
        self.known = known
        self.run = run
        run.fields = {name: ShadowField(volatile=f.volatile) for name, f in spec.fields.items()}
        self.values = _KnownValues(spec, baseline)
        self.new = 0  # records the last good run didn't have
        self.new_empty: Counter[str] = Counter()  # ... with a required field empty
        self._seen: set[str] = set()

    def add(self, key: str, result: RecordResult) -> None:
        if key in self._seen:
            return
        self._seen.add(key)
        self.run.records += 1
        self.values.add(result)
        expected = self.known.get(key)
        if expected is None:
            self.new += 1
            for name, field in self.spec.fields.items():
                if field.required and result.fields[name].value is None:
                    self.new_empty[name] += 1
            return
        self.run.compared += 1
        for name, check in self.run.fields.items():
            if name in expected:
                check.add(result.url, expected[name], encode(result.fields[name].value))

    def problems(self, threshold: float, volatile_change: float) -> list[str]:
        problems = []
        for name, f in self.run.fields.items():
            if not f.compared:
                continue
            if not f.volatile:
                if f.matched < threshold * f.compared:
                    e = f.mismatches[0]
                    problems.append(
                        f"{name} reproduces {f.matched}/{f.compared} values of the last good run "
                        f"(needs {threshold:.0%}); expected {e.expected!r}, got {e.got!r}"
                    )
                continue
            if f.emptied > (1 - threshold) * f.compared:
                e = f.example(emptied=True)
                problems.append(
                    f"{name} became empty on {f.emptied} of {f.compared} records that had a "
                    "value in the last good run (a volatile field may change, but not become "
                    f"empty); expected {e.expected!r}, got None"
                )
            if f.changed > volatile_change * f.compared:
                e = f.example(emptied=False)
                problems.append(
                    f"{name} changed on {f.changed} of {f.compared} records since the last good "
                    f"run (needs at most {volatile_change:.0%} for a volatile field); "
                    f"expected {e.expected!r}, got {e.got!r}"
                )
        for name, empty in self.new_empty.items():
            problems.append(
                f"required field {name} is empty on {empty} of {self.new} records "
                "the last good run didn't have"
            )
        return problems + self.values.problems("records of the shadow run")
