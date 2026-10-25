"""The verification gate: may a candidate spec replace the current one?

A candidate is judged on pages under the current layout, never on its own say-so:

1. Listing: does the item container still find items, and does every item it
   matches have its key (its detail link, or the key field), once? A container
   that also matches a header row fails here. The next-page link is followed for
   two pages (or until it ends, if the last good run had fewer): each page must
   load, pass the same item check and bring new items, so a link back to an
   earlier page, or one hard-coded to page 2, is caught.
2. Anchors: on the anchor pages (fetched again under the new layout), does every
   field reproduce the known-good values? Stable fields must match exactly on
   heal.threshold of the anchors: by default all of them. Only anchor pages that
   fail to load are left out, and at least three (or all, if fewer) must load.
   Volatile ones (prices, statuses) may have
   changed since, but most must not: at least heal.volatile_match of them must
   still match exactly. Every field is judged on its own, so one wrongly mapped
   field can't hide behind nine correct ones. And no two fields may read the same
   text from the same element where their known values differ: one of them reads
   the wrong thing.
3. The failed run's pages: every record on the pages the failed run stored (its
   detail pages, or without them the items of its listing pages) whose key the last
   good run had too must reproduce that run's values, by the anchors' rules: stable
   fields on heal.threshold of them, volatile ones on heal.volatile_match, and
   volatile ones with few values (a status) mostly reading values from that set.
   Nothing is fetched for this. With fewer than MIN_RUN_RECORDS such records, as
   when the run never reached the detail pages, only the anchors are compared, and
   the report notes it.
4. Sample: on other detail pages, which nobody has known values for, are required
   fields filled, and do fields that differ between anchors still differ? A
   strategy that only matches the examples it was shown leaves fields empty or
   constant there.
5. Known values: a field that took only a few values in the last good run (a
   status, a category) must mostly read values from that set, on anchor and sample
   pages alike.

Accepted only if there is no problem at all.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from itertools import combinations

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
MIN_RUN_RECORDS = 20  # known records on the failed run's pages needed to compare them
SAMPLE_PAGES = 10  # non-anchor detail pages checked for plausibility
PAGINATION_HOPS = 2  # next-page links followed from the first listing page
MAX_REPEATED_SHARE = 0.5  # of a listing page's items that may already have been seen
MIN_VALUES = 3  # values a field must read before they are checked against known ones
MAX_UNKNOWN_SHARE = 0.5  # of those, the share that may be new since the last good run


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


class VerificationReport(BaseModel):
    accepted: bool
    threshold: float  # share of anchor values a stable field must reproduce
    volatile_match: float = 0.6  # ... and a volatile one
    anchors: int  # anchors available
    anchors_checked: int  # anchor pages loaded and compared
    fields: dict[str, FieldCheck]
    listing_items: int | None = None  # items found on the run's first listing page
    listing_links: int | None = None  # of those, items with a key (their detail link)
    listing_pages: int | None = None  # listing pages seen: the first, then along the next link
    sample: int = 0  # non-anchor pages checked
    run_records: int = 0  # records on the failed run's stored pages the last good run had
    run_fields: dict[str, FieldCheck] = Field(default_factory=dict)  # empty: not compared
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
    AnchorStore.records); without them, only the anchors' values are compared."""
    threshold = spec.heal.threshold if threshold is None else threshold
    volatile_match = spec.heal.volatile_match
    extractor = Extractor(spec.fields)
    strategies = {name: [build(s) for s in f.strategies] for name, f in spec.fields.items()}
    problems: list[str] = []

    # 1. listing
    listing_items = listing_links = listing_pages = None
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

    # 3. the failed run's pages: every record on them that the last good run had too
    unit = "pages" if spec.item.follow else "items"
    run = _run_records(spec, pages, extractor, known or {})
    run_fields: dict[str, FieldCheck] = {}
    notes: list[str] = []
    if not known:
        notes.append(
            "the last good run's records are not stored, "
            "so known values were compared on the anchors only"
        )
    elif len(run) < MIN_RUN_RECORDS:
        where = "pages" if spec.item.follow else "listing pages"
        notes.append(
            f"only {len(run)} records on the failed run's stored {where} are known from the "
            f"last good run (needs {MIN_RUN_RECORDS}), "
            "so known values were compared on the anchors only"
        )
    else:
        run_fields = {name: FieldCheck(volatile=f.volatile) for name, f in spec.fields.items()}
        for key, result in run:
            _compare(run_fields, known[key], result, result.url)
        what = "values of the last good run"
        problems += _rate_problems(run_fields, threshold, volatile_match, what)
        volatile = [name for name, f in spec.fields.items() if f.volatile]
        records = [result for _, result in run]
        problems += _value_problems(spec, baseline, records, f"stored {unit}", volatile)

    # 4. sample: other items of the first listing page (their detail pages, if any)
    anchored = {a.url for a in anchors} if spec.item.follow else {a.key for a in anchors}
    others = [(key, item) for key, item in items if key is not None and key not in anchored]
    sample: list[RecordResult] = []
    for key, item in others[:SAMPLE_PAGES]:
        doc = pages.page(key) if spec.item.follow else item
        if doc is not None:
            sample.append(extractor.extract(doc))
    problems += _sample_problems(spec, anchors, sample, unit)

    # 5. known values
    problems += _value_problems(spec, baseline, checked + sample, unit)

    return VerificationReport(
        accepted=not problems,
        threshold=threshold,
        volatile_match=volatile_match,
        anchors=len(anchors),
        anchors_checked=len(checked),
        fields=checks,
        listing_items=listing_items,
        listing_links=listing_links,
        listing_pages=listing_pages,
        sample=len(sample),
        run_records=len(run),
        run_fields=run_fields,
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
        elif len(check.mismatches) < 3:
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


def _run_records(
    spec: SourceSpec, pages: Pages, extractor: Extractor, known: KnownRecords
) -> list[tuple[str, RecordResult]]:
    """The records on the pages the failed run stored whose key the last good run had
    too, each once: from its detail pages, or without them the items of its listing
    pages. Every stored page is parsed, so none are when no records are known."""
    if not known:
        return []
    found: dict[str, RecordResult] = {}
    if spec.item.follow is not None:
        for doc in pages.detail_pages():
            result = extractor.extract(doc)
            key = key_of(result, spec.item.key)
            if key is not None and key in known:
                found.setdefault(key, result)
    else:
        for page in pages.listing_pages():
            for key, item in _items(spec, page):
                if key is not None and key in known and key not in found:
                    found[key] = extractor.extract(item)
    return list(found.items())


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


def _value_problems(
    spec: SourceSpec,
    baseline: RunSummary | None,
    records: list[RecordResult],
    unit: str,
    names: Iterable[str] | None = None,
) -> list[str]:
    """Fields that took only a few values in the last good run (a status, a category)
    must mostly read values from that set; a new one now and then is fine. Checks
    the fields in `names`, or all of them."""
    problems: list[str] = []
    if baseline is None:
        return problems
    for name in spec.fields if names is None else names:
        stats = baseline.fields.get(name)
        known = stats.known_values if stats is not None else None
        if known is None:
            continue
        values = [v for v in (encode(r.fields[name].value) for r in records) if v is not None]
        unknown = [v for v in values if v not in known]
        if len(values) >= MIN_VALUES and len(unknown) > MAX_UNKNOWN_SHARE * len(values):
            names = sorted(map(str, known))
            had = ", ".join(names[:8]) + (f" and {len(names) - 8} more" if len(names) > 8 else "")
            problems.append(
                f"{name} reads values the last good run never had on {len(unknown)} of "
                f"{len(values)} {unit} (e.g. {unknown[0]!r}; it had: {had})"
            )
    return problems
