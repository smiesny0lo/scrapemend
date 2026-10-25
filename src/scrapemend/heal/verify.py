"""The verification gate: may a candidate spec replace the current one?

A candidate is judged on pages under the current layout, never on its own say-so:

1. Listing: does the item container still find items, do they still link to
   detail pages, and is the next-page link still there?
2. Anchors: on the anchor pages (fetched again under the new layout), does every
   field reproduce the known-good values? Stable fields must match exactly,
   volatile ones (prices, statuses) only in being present or absent. Every field
   must reach the threshold on its own, so one wrongly mapped field can't hide
   behind nine correct ones.
3. Sample: on other detail pages, which nobody has known values for, are required
   fields filled, and do fields that differ between anchors still differ? A
   strategy that only matches the examples it was shown leaves fields empty or
   constant there.

Accepted only if there is no problem at all.
"""

from pydantic import BaseModel, Field

from scrapemend.extract import Extractor, RecordResult
from scrapemend.extract.select import find_link, select_all
from scrapemend.heal.anchors import Anchor, AnchorValue, encode
from scrapemend.heal.pages import Pages
from scrapemend.pipeline import RunSummary
from scrapemend.spec import FieldSpec, SourceSpec

MIN_ANCHORS = 3  # anchor pages that must load for a verdict
SAMPLE_PAGES = 10  # non-anchor detail pages checked for plausibility
MIN_LINKED_SHARE = 0.8  # items that must still link to a detail page


class Mismatch(BaseModel):
    url: str | None
    expected: AnchorValue
    got: AnchorValue


class FieldCheck(BaseModel):
    matched: int = 0
    compared: int = 0
    mismatches: list[Mismatch] = Field(default_factory=list)  # the first few

    @property
    def rate(self) -> float:
        return self.matched / self.compared if self.compared else 0.0


class VerificationReport(BaseModel):
    accepted: bool
    threshold: float
    anchors: int  # anchors available
    anchors_checked: int  # anchor pages loaded and compared
    fields: dict[str, FieldCheck]
    listing_items: int | None = None  # items found on the run's first listing page
    listing_links: int | None = None  # of those, items with a detail link
    sample: int = 0  # non-anchor pages checked
    problems: list[str] = Field(default_factory=list)

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
) -> VerificationReport:
    threshold = spec.heal.threshold if threshold is None else threshold
    extractor = Extractor(spec.fields)
    problems: list[str] = []

    # 1. listing
    listing_items = listing_links = None
    links: list[str] = []
    listings = pages.listing_pages()
    if listings:
        first = listings[0]
        items = select_all(spec.item.container, first)
        listing_items = len(items)
        if not items:
            problems.append("the item container matches nothing on the listing page")
        if spec.item.follow is not None:
            links = [link for link in (find_link(spec.item.follow, i) for i in items) if link]
            listing_links = len(links)
            if items and len(links) < MIN_LINKED_SHARE * len(items):
                problems.append(f"only {len(links)} of {len(items)} items link to a detail page")
        paginate = spec.fetch.paginate
        stopped = paginate is not None and find_link(paginate.next, first) is None
        if stopped and baseline is not None and baseline.listing_pages > 1:
            problems.append("no next-page link on the first listing page")

    # 2. anchors
    checks = {name: FieldCheck() for name in spec.fields}
    checked = 0
    for anchor in anchors:
        doc = pages.page(anchor.url) if anchor.url else None
        if doc is None:
            continue
        checked += 1
        result = extractor.extract(doc)
        for name, check in checks.items():
            if name not in anchor.values:
                continue
            expected, got = anchor.values[name], encode(result.fields[name].value)
            check.compared += 1
            if _matches(spec.fields[name], expected, got):
                check.matched += 1
            elif len(check.mismatches) < 3:
                check.mismatches.append(Mismatch(url=anchor.url, expected=expected, got=got))
    if not anchors:
        problems.append("no anchors to verify against: the source has had no good run yet")
    elif checked < min(MIN_ANCHORS, len(anchors)):
        problems.append(f"only {checked} of {len(anchors)} anchor pages could be loaded")
    for name, check in checks.items():
        if check.compared and check.rate < threshold:
            example = check.mismatches[0]
            problems.append(
                f"{name} reproduces {check.matched}/{check.compared} anchor values "
                f"(needs {threshold:.0%}); expected {example.expected!r}, got {example.got!r}"
            )

    # 3. sample
    anchor_urls = {a.url for a in anchors}
    sample: list[RecordResult] = []
    for url in [link for link in links if link not in anchor_urls][:SAMPLE_PAGES]:
        doc = pages.page(url)
        if doc is not None:
            sample.append(extractor.extract(doc))
    problems += _sample_problems(spec, anchors, sample)

    return VerificationReport(
        accepted=not problems,
        threshold=threshold,
        anchors=len(anchors),
        anchors_checked=checked,
        fields=checks,
        listing_items=listing_items,
        listing_links=listing_links,
        sample=len(sample),
        problems=problems,
    )


def _matches(field: FieldSpec, expected: AnchorValue, got: AnchorValue) -> bool:
    if field.volatile:
        return (expected is None) == (got is None)
    return expected == got


def _sample_problems(
    spec: SourceSpec, anchors: list[Anchor], sample: list[RecordResult]
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
                f" of {len(values)} other pages"
            )
        elif len(values) >= 3 and not filled and usually_filled:
            problems.append(f"{name} is empty on all {len(values)} other pages")
        distinct_known = {v for v in known if v is not None}
        if (
            not field.volatile
            and len(distinct_known) >= 2
            and len(filled) >= 3
            and len(set(filled)) == 1
        ):
            problems.append(
                f"{name} reads {filled[0]!r} on every other page although it differs "
                "between anchors: the strategy may only fit the examples"
            )
    return problems
