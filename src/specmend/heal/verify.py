"""The verification gate: may a candidate spec replace the current one?

A candidate is judged on pages under the current layout, never on its own say-so.
Quick checks on a few pages come first, so most wrong repairs are turned down
without crawling the site:

0. Regexes: none may take longer than its engine allows (see
   specmend.extract.patterns). One that backtracks catastrophically on these
   pages would do so on every run.
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

- the shadow run must find heal.shadow_min_found (90%) of the last good run's
  records, and of each kind of record (the records where a field was empty, those
  of each value of a field with a small set of values), unless chance explains
  what one kind is missing;
- on every record both runs have, a stable field must be identical (on
  heal.threshold of them: by default all);
- a volatile field may have changed on at most heal.volatile_change of them, and a
  value that became empty is never a change: it is a mismatch, judged like a
  stable field's;
- a field with a small set of values must mostly read values from that set, and
  required fields must be filled on records the last good run didn't have;
- a number field must follow the same rules on its 10 largest and 10 smallest
  values of the last good run, unless chance explains the difference: a pattern
  too narrow for long or short numbers misreads exactly those.

When the last good run had more records than heal.shadow_max_items (500 unless
it is `all`), the shadow run still reads every listing page along the candidate's
next-page link, and must find heal.shadow_min_found of the last good run's items
there, but only a sample of their detail pages: the records the rules judge on
their own (the extremes of each number field, a record of every kind), then others
at random, with a fixed seed, from the items of every page. Before it starts, it
says what it will read and about how many requests that takes (`on_shadow`).

Every check ends passed, failed or not run, with the reason. A candidate is accepted
only if every check passed. One that failed a check is rejected. One where a check
couldn't run (a page that didn't load, no records of a good run to compare with) is
unverified: fail closed. It is never promoted automatically, and only `promote
--force` can promote it, never past a check that failed.
"""

import math
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Literal

from lxml import etree
from pydantic import BaseModel, Field

from specmend.extract import Document, Extractor, RecordResult, patterns
from specmend.extract.select import find_link, select_all
from specmend.extract.strategies import Locating, Strategy, StrategyTimeout, build
from specmend.heal.anchors import (
    Anchor,
    AnchorValue,
    KnownRecords,
    encode,
    key_of,
    rank,
)
from specmend.heal.pages import Pages
from specmend.pipeline import RunSummary
from specmend.spec import RegexSpec, SourceSpec

MIN_ANCHORS = 3  # anchor pages that must load for a verdict (all of them, if fewer)
SAMPLE_PAGES = 10  # non-anchor detail pages checked for plausibility
PAGINATION_HOPS = 2  # next-page links followed from the first listing page
MAX_REPEATED_SHARE = 0.5  # of a listing page's items that may already have been seen
MIN_VALUES = 3  # values a field must read before they are checked against known ones
MAX_UNKNOWN_SHARE = 0.5  # of those, the share that may be new since the last good run
MIN_SHADOW_RECORDS = 20  # records to compare (all of them, if the last good run had fewer)
SHADOW_SEED = "specmend-shadow"  # draws the shadow run's sample, when it samples
EXAMPLES = 3  # mismatches kept per field (and kind, in the shadow run)
EXTREMES = 10  # records at each end of a number field that the shadow run judges on their own
CHANCE = 0.001  # what happens less often than this by chance alone is no chance

CheckStatus = Literal["passed", "failed", "not run"]


class Mismatch(BaseModel):
    url: str | None
    expected: AnchorValue
    got: AnchorValue


class FieldCheck(BaseModel):
    matched: int = 0
    compared: int = 0
    emptied: int = 0  # of the mismatches, those that read nothing
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

    scope: Literal["full", "sample"]  # a sample reads every listing page, some detail pages
    listing_pages: int = 0  # listing pages read: every one along the next-page link
    items: int = 0  # items the item container matched on them
    drawn: int = 0  # in a sample: the items whose detail pages it read
    records: int = 0  # records read, each key once
    known: int = 0  # records the last good run had
    compared: int = 0  # records read whose key the last good run had
    new: int = 0  # records read whose key it didn't: only checked for required fields...
    found: int = 0  # of the last good run's records, those read (or in a sample, listed)
    items_before: int = 0  # items the last good run found on its listing pages
    stopped: int | None = None  # stopped at a page: the most records it could have compared
    values: dict[str, int] = Field(default_factory=dict)  # small sets: field -> values judged
    empties: list[str] = Field(default_factory=list)  # fields whose empty records were judged
    extremes: list[str] = Field(default_factory=list)  # number fields judged at their ends
    fields: dict[str, ShadowField] = Field(default_factory=dict)


class ShadowPlan(BaseModel):
    """What a shadow run is about to read, said before it starts: an estimate from the
    last good run's counts and the pages already stored."""

    scope: Literal["full", "sample"]
    records: int  # the records it means to read: the last good run's, or a sample's
    known: int  # the last good run's records
    listing_pages: int  # listing pages to read: as many as the last good run's
    detail_pages: int  # detail pages to read (none without item.follow)
    stored: int  # of those pages, the ones at hand already
    requests: int  # the others: about one request each
    rps: float  # the spec's rate limit, requests a second


class Check(BaseModel):
    """One step of the gate. `reason` says what it covered, or why it didn't run."""

    name: str
    status: CheckStatus
    reason: str
    problems: list[str] = Field(default_factory=list)  # when it failed


class VerificationReport(BaseModel):
    checks: list[Check] = Field(default_factory=list)  # in the order they ran
    threshold: float = 1.0  # share of anchor values a stable field must reproduce
    volatile_match: float = 0.6  # ... and a volatile one
    volatile_change: float = 0.1  # share of shadow-run records a volatile field may change on
    min_found: float = 0.9  # share of the last good run's records the shadow run must find
    anchors: int = 0  # anchors available
    anchors_checked: int = 0  # anchor pages loaded and compared
    fields: dict[str, FieldCheck] = Field(default_factory=dict)
    listing_items: int | None = None  # items found on the run's first listing page
    listing_links: int | None = None  # of those, items with a key (their detail link)
    listing_pages: int | None = None  # listing pages seen: the first, then along the next link
    sample: int = 0  # non-anchor pages checked
    shadow: ShadowRun | None = None  # None: it didn't start

    @property
    def verdict(self) -> CheckStatus:
        """Failed if any check failed; else not run if any check didn't run (or none
        did): only every check passing passes. Fail closed."""
        statuses = {check.status for check in self.checks}
        if "failed" in statuses:
            return "failed"
        if "not run" in statuses or not statuses:
            return "not run"
        return "passed"

    @property
    def accepted(self) -> bool:
        return self.verdict == "passed"

    @property
    def problems(self) -> list[str]:
        """What is wrong with the candidate: the problems of the checks that failed."""
        return [
            problem
            for check in self.checks
            if check.status == "failed"
            for problem in check.problems
        ]

    @property
    def not_run(self) -> list[Check]:
        return [check for check in self.checks if check.status == "not run"]

    def check(self, name: str) -> Check:
        return next(check for check in self.checks if check.name == name)

    @property
    def score(self) -> float:
        """The weakest field's share of reproduced anchor values."""
        return min((f.rate for f in self.fields.values()), default=0.0)

    def evidence(self) -> list[str]:
        """What the gate compared, field by field, and what the rules tolerated, for
        every verdict: whoever promotes a repair sees what it let through. From the
        shadow run if it compared records, else from the anchors."""
        if self.shadow is not None and self.shadow.compared:
            return _shadow_evidence(self, self.shadow)
        if self.anchors_checked:
            return _anchor_evidence(self)
        return []


def verify(
    spec: SourceSpec,
    anchors: list[Anchor],
    pages: Pages,
    *,
    threshold: float | None = None,
    baseline: RunSummary | None = None,
    known: KnownRecords | None = None,
    on_shadow: Callable[[ShadowPlan], None] | None = None,
) -> VerificationReport:
    """`known` holds the values of the last good run's records, by key (see
    AnchorStore.records): what the shadow run is compared with. `on_shadow` is told
    what the shadow run will read, before it starts."""
    threshold = spec.heal.threshold if threshold is None else threshold
    volatile_match = spec.heal.volatile_match
    extractor = Extractor(spec.fields)
    strategies = {name: [build(s) for s in f.strategies] for name, f in spec.fields.items()}

    # 1. listing
    problems: list[str] = []
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
        seen, walk_problems, unreachable = _walk(spec, pages, first, items, baseline)
        listing_pages = len(seen)
        problems += walk_problems
        for page in seen:
            rows.add(page)
    else:
        unreachable = "no listing page of the failed run to verify against"
    listing = _check(
        "listing",
        problems,
        unreachable,
        f"{listing_items} items on the first listing page, "
        f"{listing_pages} listing pages along the next-page link",
    )

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
    problems = _rate_problems(checks, threshold, volatile_match, "anchor values")
    for (a, b), count in shared.items():
        problems.append(
            f"{a} and {b} read the same element on {count} of {len(checked)} anchor pages"
        )
    unloaded = None
    if not anchors:
        unloaded = "no anchors to verify against: the source has had no good run yet"
    elif len(checked) < min(MIN_ANCHORS, len(anchors)):
        if spec.item.follow:  # the pages didn't load: nothing is known about them
            unloaded = f"only {len(checked)} of {len(anchors)} anchor pages could be loaded"
        else:  # the candidate's container or key can't find them
            problems.append(
                f"only {len(checked)} of {len(anchors)} anchors were found on their listing pages"
            )
    what = "anchor pages" if spec.item.follow else "anchors"
    anchor_check = _check(
        "anchors",
        problems,
        unloaded,
        f"{len(checked)} of {len(anchors)} {what} compared; "
        f"weakest field {min((c.rate for c in checks.values()), default=0.0):.0%}",
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
    tried = len(others[:SAMPLE_PAGES])
    sample_check = _check(
        "sample",
        _sample_problems(spec, anchors, sample, unit),
        f"none of the {tried} other {unit} could be loaded" if tried and not sample else None,
        f"{len(sample)} other {unit}",
    )

    # 4. known values
    values = _KnownValues(spec, baseline)
    for result in checked + sample:
        values.add(result)
    names = ", ".join(values.known) or "no field takes only a few values"
    values_check = _check(
        "known values",
        values.problems(unit),
        None,
        f"{names} on {len(checked) + len(sample)} {unit}" if values.known else names,
    )

    # 5. the shadow run, unless a quicker check already failed
    shadow = None
    quick = [listing, anchor_check, sample_check, values_check]
    if extractor.timeouts or any(check.status == "failed" for check in quick):
        shadow_check = _check("shadow run", [], "skipped: a quicker check failed", "")
    elif first is None:
        shadow_check = _check("shadow run", [], "skipped: no listing page to start from", "")
    else:
        shadow, problems, unavailable = _shadow(
            spec, pages, extractor, first, known or {}, baseline, threshold, on_shadow
        )
        shadow_check = _check("shadow run", problems, unavailable, _covered(shadow))

    return VerificationReport(
        checks=[_regexes(spec, extractor), *quick, shadow_check],
        threshold=threshold,
        volatile_match=volatile_match,
        volatile_change=spec.heal.volatile_change,
        min_found=spec.heal.shadow_min_found,
        anchors=len(anchors),
        anchors_checked=len(checked),
        fields=checks,
        listing_items=listing_items,
        listing_links=listing_links,
        listing_pages=listing_pages,
        sample=len(sample),
        shadow=shadow,
    )


def _allowed(share: float, out_of: int) -> int:
    """How many of `out_of` records a rule that allows `share` of them lets through."""
    return math.floor(share * out_of + 1e-9)


def _needed(share: float, out_of: int) -> int:
    """How many of `out_of` records a rule that requires `share` of them needs."""
    return math.ceil(share * out_of - 1e-9)


def _shadow_evidence(report: VerificationReport, shadow: ShadowRun) -> list[str]:
    if shadow.stopped is None:
        lines = [f"fields, on the {shadow.compared} records of the last good run it compared:"]
    else:
        lines = [
            f"fields, on the {shadow.compared} records of the last good run it compared before "
            f"it stopped, of the {shadow.stopped} it would have:"
        ]
    for name, f in shadow.fields.items():
        if not f.compared:
            continue
        base = f.compared if shadow.stopped is None else max(shadow.stopped, f.compared)
        of = "" if shadow.stopped is None else f" of {base}"
        empty = _allowed(1 - report.threshold, base)
        if f.volatile:
            change = _allowed(report.volatile_change, base)
            emptied = "none may become empty" if not empty else f"up to {empty} may become empty"
            rule = f"volatile: up to {change}{of} may change, {emptied}"
            over = f.changed > change or f.emptied > empty
        else:
            rule = (
                "stable: none may differ" if not empty else f"stable: up to {empty}{of} may differ"
            )
            over = f.changed + f.emptied > empty
        differ = f.changed + f.emptied
        outcome = "more than allowed" if over else f"tolerated {differ}" if differ else "none"
        lines.append(
            f"  {name}: {f.compared} compared, {f.changed} changed, {f.emptied} emptied ({rule}): "
            f"{outcome}"
        )
    known = shadow.known
    if shadow.stopped is not None:
        lines.append(
            f"records: found {shadow.found} of the last good run's {known} before it stopped: "
            "not judged"
        )
        return lines
    if shadow.scope == "sample":
        lines.append(
            f"records: {shadow.items} items on {shadow.listing_pages} listing pages, where the "
            f"last good run found {shadow.items_before} (at least "
            f"{_needed(report.min_found, shadow.items_before)} needed); the detail pages of "
            f"{shadow.drawn} of them read, {shadow.compared} of them known from the last good run"
        )
    else:
        lines.append(
            f"records: found {shadow.found} of the last good run's {known} (at least "
            f"{_needed(report.min_found, known)} needed)"
        )
    if shadow.new:
        lines.append(
            f"new: {shadow.new} record{'s' if shadow.new > 1 else ''} the last good run didn't "
            "have: only their required fields, and fields with a small set of values, were "
            "checked"
        )
    values = [f"{name} ({count})" for name, count in shadow.values.items()]
    judged = [f"every value of {_and(values)}"] if values else []
    judged += [f"the records where {name} was empty" for name in shadow.empties]
    judged += [f"the 10 largest and the 10 smallest values of {name}" for name in shadow.extremes]
    if judged:
        lines.append(f"judged on their own: {'; '.join(judged)}")
    return lines


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _anchor_evidence(report: VerificationReport) -> list[str]:
    lines = [f"fields, on the {report.anchors_checked} anchor pages it compared:"]
    for name, f in report.fields.items():
        if not f.compared:
            continue
        must = _needed(report.volatile_match if f.volatile else report.threshold, f.compared)
        kind = "volatile" if f.volatile else "stable"
        rule = f"all {must}" if must == f.compared else f"{must} of {f.compared}"
        differ = f.compared - f.matched
        outcome = (
            "too few match" if f.matched < must else f"tolerated {differ}" if differ else "none"
        )
        lines.append(
            f"  {name}: {f.compared} compared, {differ - f.emptied} changed, {f.emptied} emptied "
            f"({kind}: {rule} must match): {outcome}"
        )
    return lines


def _check(name: str, problems: list[str], not_run: str | None, covered: str) -> Check:
    """A check's verdict: failed if it found problems, even when part of it couldn't
    run; else not run if it couldn't; else passed."""
    if problems:
        return Check(name=name, status="failed", reason=covered, problems=problems)
    if not_run is not None:
        return Check(name=name, status="not run", reason=not_run)
    return Check(name=name, status="passed", reason=covered)


def _regexes(spec: SourceSpec, extractor: Extractor) -> Check:
    """Every regex timeout the verification ran into, on any page."""
    regexes = [s for f in spec.fields.values() for s in f.strategies if isinstance(s, RegexSpec)]
    if not regexes:
        return _check("regexes", [], None, "no regexes")
    on_re2 = sum(r.engine == "re2" for r in regexes)
    limit = f"{patterns.TIME_LIMIT:g}s"
    engines = [f"{on_re2} on RE2"] if on_re2 else []
    if on_re2 < len(regexes):
        engines.append(f"{len(regexes) - on_re2} on Python's re (at most {limit} a search)")
    counted = f"{len(regexes)} regex{'es' if len(regexes) > 1 else ''}"
    problems = []
    for (name, index), pages in extractor.timeouts.items():
        strategy = spec.fields[name].strategies[index]
        what = (
            f"regex {strategy.regex!r}" if isinstance(strategy, RegexSpec) else f"strategy {index}"
        )
        problems.append(f"{name}: {what} took longer than {limit} on {pages} pages")
    return _check("regexes", problems, None, f"{counted}: {', '.join(engines)}")


def _covered(shadow: ShadowRun) -> str:
    if shadow.scope == "sample":
        read = (
            f"every listing page and a sample of detail pages: {shadow.listing_pages} listing "
            f"pages, {shadow.items} items, {shadow.records} records"
        )
    else:
        read = f"the whole source: {shadow.listing_pages} listing pages, {shadow.records} records"
    if shadow.compared == shadow.known:
        return f"{read}; all {shadow.known} of the last good run's records compared"
    return f"{read}; {shadow.compared} of the last good run's {shadow.known} records compared"


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
            continue
        check.emptied += got is None
        if len(check.mismatches) < EXAMPLES:
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
) -> tuple[list[Document], list[str], str | None]:
    """Follow the next-page link from the first listing page for PAGINATION_HOPS
    pages, or until it ends. Each page must load and bring items not seen on the
    pages before, every item on it must have its key, and the link must not end
    before the last good run's did. Returns the listing pages seen, the first
    included, the problems found, and why it stopped if a page couldn't be fetched:
    then nothing is known about it, and the link may be fine."""
    walked = [first]
    paginate = spec.fetch.paginate
    if paginate is None:
        return walked, [], None
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
            return walked, [*problems, problem], None
        visited.add(link)
        page = pages.listing(link)
        if page is None:
            failure = pages.failure(link)
            if failure is None or failure.unreachable:
                reason = failure.reason if failure else "it did not load"
                unreachable = f"listing page {number + 1} could not be loaded ({reason}): {link}"
                return walked, problems, unreachable
            return (
                walked,
                [*problems, f"listing page {number + 1} could not be loaded: {link}"],
                None,
            )
        walked.append(page)
        items = _items(spec, page)
        keys = [key for key, _ in items if key]
        if not keys:
            problem = f"the item container matches nothing on listing page {number + 1}"
            return walked, [*problems, problem], None
        repeated = sum(key in seen for key in keys)
        if repeated > MAX_REPEATED_SHARE * len(keys):
            problem = (
                f"the next-page link on listing page {number} leads to a page that repeats "
                f"{repeated} of {len(keys)} items already seen"
            )
            return walked, [*problems, problem], None
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
    return walked, problems, None


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
        if not isinstance(strategy, Locating):
            continue
        try:
            element = strategy.locate(doc)
        except StrategyTimeout:  # the extractor counted it already
            continue
        if element is not None:
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
    on_shadow: Callable[[ShadowPlan], None] | None = None,
) -> tuple[ShadowRun, list[str], str | None]:
    """Crawl the source with the candidate and compare its records with the last good
    run's. Returns what it found, the problems, and why it couldn't run, if it couldn't."""
    limit = spec.heal.shadow_max_items
    # all: the whole source. A spec without detail pages has its records on the listing
    # pages, which a sample reads too, so there is nothing to save.
    cap = limit if isinstance(limit, int) and spec.item.follow is not None else None
    sampled = cap is not None and len(known) > cap
    run = ShadowRun(scope="sample" if sampled else "full", known=len(known))
    if not known:
        return run, [], "the last good run's records are not stored"
    if on_shadow is not None:
        on_shadow(_plan(spec, pages, first, known, baseline, cap if sampled else None))
    crawl = _Crawl(spec, pages, extractor, run)
    comparison = _Comparison(spec, known, baseline, run)
    if cap is not None and sampled:
        records = crawl.sample(first, cap, known, baseline)
    else:
        records = crawl.all(first)
    for key, result in records:
        comparison.add(key, result)
    run.new, run.found = comparison.new, run.compared
    run.items_before = baseline.items if baseline is not None else 0
    if crawl.unavailable is not None:
        # Stopped short: what it compared may still prove the candidate wrong.
        most = run.drawn if sampled else len(known)
        run.stopped = most
        problems = comparison.problems(threshold, spec.heal.volatile_change, out_of=most)
        stopped = crawl.unavailable
        if run.listing_pages:
            stopped += (
                f"; it compared {run.compared} of the last good run's {len(known)} records "
                "before it stopped"
            )
        return run, crawl.problems + problems, stopped

    problems = crawl.problems
    min_found = spec.heal.shadow_min_found
    before = len(problems)
    if sampled:
        # Every listing page was read: their items are what a run would find.
        had = baseline.items if baseline is not None else 0
        if run.items < min_found * had:
            problems.append(
                f"the shadow run found {run.items} items on {run.listing_pages} listing pages, "
                f"where the last good run found {had} (needs {min_found:.0%}): the item "
                "container or the pagination miss some"
            )
        if run.compared < min_found * run.drawn:
            problems.append(
                f"only {run.compared} of the {run.drawn} sampled records are known from the "
                f"last good run (needs {min_found:.0%}): the detail link or the key reads "
                "something else"
            )
    elif run.compared < min_found * len(known):
        problems.append(
            f"the shadow run found {run.compared} of the last good run's {len(known)} records "
            f"(needs {min_found:.0%}): the item container, the detail links or the "
            "pagination miss some"
        )
    if sampled and spec.item.key == "url":  # the links on the listing pages are the keys
        run.found = len(crawl.listed & known.keys())
    if len(problems) == before and (not sampled or spec.item.key == "url"):
        # Most records were found: most of every kind must be.
        found = set(comparison.compared) if not sampled else crawl.listed & known.keys()
        kinds = _kinds(spec, known, baseline, known)
        problems += _kind_problems(kinds, known, found, min_found)
        run.values = dict(Counter(name for name, value in kinds if value is not None))
        run.empties = [name for name, value in kinds if value is None]
    problems += comparison.problems(threshold, spec.heal.volatile_change)
    needed = min(MIN_SHADOW_RECORDS, len(known))
    if not problems and run.compared < needed:
        return run, [], f"only {run.compared} records can be compared (needs {needed})"
    return run, problems, None


def _kind_problems(
    kinds: Mapping[tuple[str, AnchorValue], list[str]],
    known: KnownRecords,
    found: set[str],
    min_found: float,
) -> list[str]:
    """The kinds of record (_kinds) of which the shadow run found fewer than
    `min_found` of the last good run's, where chance can't explain it: a container,
    a link or a pagination that misses one kind of record misses every one of them,
    in every run. `found`: the keys of the last good run's records it found."""
    missing = (len(known) - len(found)) / len(known)
    problems = []
    for (name, value), members in kinds.items():
        got = sum(key in found for key in members)
        if got >= min_found * len(members):
            continue
        if not _improbable(len(members) - got, len(members), missing):
            continue  # as many gone as anywhere else, like a source that churns
        what = "was empty" if value is None else f"was {value!r}"
        problems.append(
            f"the shadow run found {got} of the {len(members)} records of the last good run "
            f"whose {name} {what} (needs {min_found:.0%}), against {len(found) - got} of the "
            f"other {len(known) - len(members)}: the item container, the detail links or the "
            "pagination miss that kind of record"
        )
    return problems


def _plan(
    spec: SourceSpec,
    pages: Pages,
    first: Document,
    known: KnownRecords,
    baseline: RunSummary | None,
    cap: int | None,
) -> ShadowPlan:
    """What the shadow run will read, for the whole source or every listing page and
    a sample of `cap` detail pages: as many listing pages as the last good run read,
    less the pages stored. Which records a sample draws is known only once its
    listing pages are read: if they are all stored, it is counted exactly (as when
    promote verifies again); otherwise its detail pages are counted as not stored."""
    follow = spec.item.follow is not None
    listings, details = pages.stored("listing"), pages.stored("detail") if follow else set()
    paginate = spec.fetch.paginate
    most = paginate.max_pages if paginate else 1
    listing_pages = max(1, min(baseline.listing_pages if baseline else 1, most))
    if cap is None:
        records = len(known)
        detail_pages = records if follow else 0
        stored = min(len(listings), listing_pages) + min(len(details), detail_pages)
    else:
        walked = _stored_walk(spec, pages, first)
        if walked is not None:
            keys = [key for doc in walked for key, _ in _items(spec, doc) if key is not None]
            chosen = _choose(spec, keys, known, baseline, cap)
            listing_pages, records = len(walked), len(chosen)
            stored = len(walked) + sum(key in details for key in chosen)
        else:  # as if every record of the last good run were still listed
            records = len(_choose(spec, known, known, baseline, cap))
            stored = min(len(listings), listing_pages)
        detail_pages = records
    return ShadowPlan(
        scope="full" if cap is None else "sample",
        records=records,
        known=len(known),
        listing_pages=listing_pages,
        detail_pages=detail_pages,
        stored=stored,
        requests=listing_pages + detail_pages - stored,
        rps=spec.fetch.rate.rps,
    )


def _stored_walk(spec: SourceSpec, pages: Pages, first: Document) -> list[Document] | None:
    """The listing pages along the next-page link, if every one is stored: then what
    a sample will read is known before it starts. None if one isn't."""
    paginate = spec.fetch.paginate
    walked, visited, stored = [first], {first.url}, pages.stored("listing")
    while paginate is not None and len(walked) < paginate.max_pages:
        link = find_link(paginate.next, walked[-1])
        if link is None or link in visited:
            break
        page = pages.listing(link) if link in stored else None
        if page is None:
            return None
        visited.add(link)
        walked.append(page)
    return walked


def _choose(
    spec: SourceSpec,
    keys: Iterable[str],
    known: KnownRecords,
    baseline: RunSummary | None,
    cap: int,
) -> list[str]:
    """The records a sample reads, from the items of every listing page. First, of
    the last good run's records among them, those a rule judges on their own: the
    EXTREMES with the largest and the smallest values of each number field, and a
    record of every kind (_kinds). Then others at random, with a fixed seed, up to
    `cap` in all (or more, if those are more). In that random order."""
    ranked = sorted(dict.fromkeys(keys), key=lambda key: rank(key, SHADOW_SEED))
    had = [key for key in ranked if key in known]
    chosen = {key for members in _extremes(spec, known, had).values() for key in members}
    for members in _kinds(spec, known, baseline, had).values():
        if chosen.isdisjoint(members):
            chosen.add(members[0])
    for key in ranked:
        if len(chosen) >= cap:
            break
        chosen.add(key)
    return [key for key in ranked if key in chosen]


def _extremes(
    spec: SourceSpec, known: KnownRecords, keys: Iterable[str]
) -> dict[tuple[str, str], list[str]]:
    """For each number field, the EXTREMES records among `keys` (the last good run's)
    where its value was largest then, and those where it was smallest: (field,
    "largest" or "smallest") -> their keys, the most extreme first. A pattern that is
    too narrow for long or short numbers misreads exactly those."""
    out: dict[tuple[str, str], list[str]] = {}
    keys = list(keys)
    for name, field in spec.fields.items():
        if field.type not in ("int", "decimal"):
            continue
        valued = [(n, key) for key in keys if (n := _number(known[key].get(name))) is not None]
        if valued:
            ordered = sorted(valued, key=lambda pair: pair[0])
            out[name, "largest"] = [key for _, key in ordered[::-1][:EXTREMES]]
            out[name, "smallest"] = [key for _, key in ordered[:EXTREMES]]
    return out


def _number(value: AnchorValue) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:  # stored when the field had another type
        return None


def _kinds(
    spec: SourceSpec, known: KnownRecords, baseline: RunSummary | None, keys: Iterable[str]
) -> dict[tuple[str, AnchorValue], list[str]]:
    """The kinds of record among `keys` (the last good run's): the records where a field
    was empty in the last good run, and, for each field that took only a few values
    then (a status, a category), the records of each of its values. (field, value, or
    None for empty) -> their keys, in that order."""
    stats = baseline.fields if baseline is not None else {}
    few = {n for n in spec.fields if n in stats and stats[n].known_values is not None}
    kinds: dict[tuple[str, AnchorValue], list[str]] = {}
    for key in keys:
        values = known[key]
        for name in spec.fields:
            if name in values and (values[name] is None or name in few):
                kinds.setdefault((name, values[name]), []).append(key)
    return kinds


def _improbable(count: int, n: int, p: float) -> bool:
    """Whether `count` or more of `n` records would break a rule less often than
    CHANCE, if each broke it with probability `p`, the share of all records that do:
    then what those records have in common breaks it, not chance."""
    if count <= 0 or p >= 1:
        return False
    if p <= 0:
        return True
    log_p, log_q, log_n = math.log(p), math.log1p(-p), math.lgamma(n + 1)
    tail = sum(
        math.exp(log_n - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * log_p + (n - k) * log_q)
        for k in range(count, n + 1)
    )
    return tail < CHANCE


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
        self.listed: set[str] = set()  # in a sample: the keys of every listing page's items
        self._links: set[str] = set()

    def all(self, first: Document) -> Iterator[tuple[str, RecordResult]]:
        """Like a run: every item of every listing page, and its record."""
        for page, number in self._walk(first):
            yield from self._records(page, number)

    def sample(
        self, first: Document, cap: int, known: KnownRecords, baseline: RunSummary | None
    ) -> Iterator[tuple[str, RecordResult]]:
        """Every listing page, like a run, then the records of `cap` of their items."""
        pool: dict[str, Document] = {}
        for page, number in self._walk(first):
            for key, item in self._listed(page, number):
                if key is not None:
                    pool.setdefault(key, item)
        if self.unavailable is not None:
            return
        self.listed = set(pool)
        chosen = _choose(self.spec, pool, known, baseline, cap)
        self.run.drawn = len(chosen)
        for key in chosen:
            record = self._record(key, pool[key])
            if self.unavailable is not None:
                return
            if record is not None:
                yield record

    def _walk(self, first: Document) -> Iterator[tuple[Document, int]]:
        """The listing pages along the candidate's next-page link, from the first until
        the link ends, up to max_pages: what a run reads. Each with its number."""
        paginate = self.spec.fetch.paginate
        max_pages = paginate.max_pages if paginate else 1
        page, number, visited = first, 1, {first.url}
        while True:
            yield page, number
            if self.unavailable is not None or paginate is None or number >= max_pages:
                return
            link = find_link(paginate.next, page)
            if link is None or link in visited:
                return
            visited.add(link)
            loaded = self._listing(link, number + 1)
            if loaded is None:
                return
            page, number = loaded, number + 1

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

    def _listing(self, url: str, number: int) -> Document | None:
        page = self.pages.listing(url)
        if page is None:
            failure = self.pages.failure(url)
            reason = failure.reason if failure else "it did not load"
            if failure is not None and not failure.unreachable:
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
        self.compared: list[str] = []  # the keys of the last good run's records read
        self.differs: dict[str, dict[str, Mismatch]] = {name: {} for name in spec.fields}
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
        self.compared.append(key)
        for name, check in self.run.fields.items():
            if name in expected:
                want, got = expected[name], encode(result.fields[name].value)
                check.add(result.url, want, got)
                if want != got:
                    self.differs[name][key] = Mismatch(url=result.url, expected=want, got=got)

    def problems(
        self, threshold: float, volatile_change: float, out_of: int | None = None
    ) -> list[str]:
        """The fields that break the rules. `out_of`: the crawl stopped short, and
        could have compared at most that many records; then only mismatches beyond what
        the rules allow even if every other record matched are problems."""
        problems: list[str] = []
        failing: set[str] = set()
        for name, f in self.run.fields.items():
            if not f.compared:
                continue
            base = f.compared if out_of is None else max(out_of, f.compared)
            found = len(problems)
            if not f.volatile:
                if f.compared - f.matched > _allowed(1 - threshold, base):
                    e = f.mismatches[0]
                    problems.append(
                        f"{name} reproduces {f.matched}/{f.compared} values of the last good run "
                        f"(needs {threshold:.0%}); expected {e.expected!r}, got {e.got!r}"
                    )
            else:
                if f.emptied > _allowed(1 - threshold, base):
                    e = f.example(emptied=True)
                    problems.append(
                        f"{name} became empty on {f.emptied} of {f.compared} records that had a "
                        "value in the last good run (a volatile field may change, but not become "
                        f"empty); expected {e.expected!r}, got None"
                    )
                if f.changed > _allowed(volatile_change, base):
                    e = f.example(emptied=False)
                    problems.append(
                        f"{name} changed on {f.changed} of {f.compared} records since the last "
                        f"good run (needs at most {volatile_change:.0%} for a volatile field); "
                        f"expected {e.expected!r}, got {e.got!r}"
                    )
            if len(problems) > found:
                failing.add(name)
        for name, empty in self.new_empty.items():
            problems.append(
                f"required field {name} is empty on {empty} of {self.new} records "
                "the last good run didn't have"
            )
        if out_of is not None:
            return problems  # a share of values not yet read could still go either way
        problems += self._extreme_problems(failing, threshold, volatile_change)
        return problems + self.values.problems("records of the shadow run")

    def _extreme_problems(
        self, failing: set[str], threshold: float, volatile_change: float
    ) -> list[str]:
        """Number fields that pass the rules over all records, but not on their
        EXTREMES largest or smallest values, where chance can't explain the difference:
        prices that change don't pick the largest, but a pattern too narrow for them
        misreads every one."""
        problems = []
        for (name, end), members in _extremes(self.spec, self.known, self.compared).items():
            f = self.run.fields[name]
            if name in failing or not f.compared:
                continue
            if name not in self.run.extremes:
                self.run.extremes.append(name)
            differs = self.differs[name]
            wrong = [key for key in members if key in differs]
            allowed = volatile_change if f.volatile else 1 - threshold
            overall = f.compared - f.matched
            if len(wrong) <= _allowed(allowed, len(members)):
                continue
            if not _improbable(len(wrong), len(members), overall / f.compared):
                continue
            e = differs[wrong[0]]
            verb = "changed" if f.volatile else "reads another value"
            size = "large" if end == "largest" else "small"
            problems.append(
                f"{name} {verb} on {len(wrong)} of the {len(members)} records where it was "
                f"{end} in the last good run, against {overall - len(wrong)} of the other "
                f"{f.compared - len(members)}: a strategy may misread {size} numbers; "
                f"expected {e.expected!r}, got {e.got!r}"
            )
        return problems
