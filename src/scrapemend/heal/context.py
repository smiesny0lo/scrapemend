"""What a repair provider gets to see: the spec, what broke, and the pages then and now.

Pages are pruned before they are shown: scripts, styles and comments go, attributes
are reduced to the ones selectors use, and long texts are shortened. On listing pages,
long runs of similar siblings (table rows, cards: the items) are cut to a few with a
note of how many more there were. Detail pages keep every row, because there similar
rows are different fields. Structure, class names and labels survive; the bulk doesn't.
"""

import copy

import lxml.html
import yaml
from lxml import etree
from lxml.html import HtmlElement
from pydantic import BaseModel, Field

from scrapemend.detect.classify import Failure
from scrapemend.extract import Document
from scrapemend.heal.anchors import Anchor, AnchorValue
from scrapemend.spec import SourceSpec
from scrapemend.spec.patch import SpecPatch

KEEP_ATTRIBUTES = frozenset(
    {"class", "id", "href", "rel", "aria-label", "name", "type", "datetime", "title", "role"}
)
DROP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "iframe", "link", "meta"})
MAX_TEXT = 120  # characters kept of one text node
REPEATS_KEPT = 3  # similar siblings kept before the rest are summarised
MAX_PAGE = 20_000  # characters of pruned HTML per page


class Example(BaseModel):
    """A known-good record, shown so a provider can find where its values live now."""

    url: str | None
    values: dict[str, AnchorValue]  # as compared: dates ISO, decimals normalized
    shown_as: dict[str, str | None]  # the text each value was read from on the old page


class Attempt(BaseModel):
    """An earlier repair for the same failure, and why verification rejected it."""

    patch: SpecPatch
    problems: list[str]


class RepairContext(BaseModel):
    source_id: str
    spec_yaml: str
    failures: list[Failure]
    old_listing: str | None = None  # pruned HTML of a listing page from the last good run
    new_listing: str | None = None  # ... and of the listing page now
    old_detail: str | None = None  # a detail page from the last good run (an example's page)
    new_detail: str | None = None  # the same page now
    examples: list[Example] = Field(default_factory=list)
    attempts: list[Attempt] = Field(default_factory=list)

    @property
    def failing(self) -> list[str]:
        return [part for f in self.failures for part in f.fields]

    def with_attempt(self, patch: SpecPatch, problems: list[str]) -> "RepairContext":
        return self.model_copy(
            update={"attempts": [*self.attempts, Attempt(patch=patch, problems=problems)]}
        )


def spec_yaml(spec: SourceSpec) -> str:
    data = spec.model_dump(mode="json", exclude_defaults=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def examples(anchors: list[Anchor]) -> list[Example]:
    return [Example(url=a.url, values=a.values, shown_as=a.raw) for a in anchors]


def prune_html(
    html: str | bytes | None, *, listing: bool = False, max_chars: int = MAX_PAGE
) -> str | None:
    """Markup reduced to what helps find a value: structure, classes, labels, text."""
    if not html:
        return None
    return prune_document(Document.parse(html), listing=listing, max_chars=max_chars)


def prune_document(
    doc: Document | None, *, listing: bool = False, max_chars: int = MAX_PAGE
) -> str | None:
    """`listing`: collapse runs of similar items (only right where siblings are items)."""
    if doc is None:
        return None
    root = doc.root
    body = root.find("body")
    tree = copy.deepcopy(body if body is not None else root)
    _prune(tree, listing)
    out = str(lxml.html.tostring(tree, encoding="unicode", pretty_print=True))
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n<!-- cut: {len(out) - max_chars} more characters -->"
    return out


def _prune(element: HtmlElement, listing: bool) -> None:
    for child in list(element):
        if not isinstance(child.tag, str) or child.tag in DROP_TAGS:
            _drop(child)
            continue
        _prune(child, listing)
    for name in list(element.attrib):
        if name not in KEEP_ATTRIBUTES and not name.startswith("data-"):
            del element.attrib[name]
    href = element.get("href")
    if href and len(href) > 100:
        element.set("href", href[:100] + "...")
    element.text = _shorten(element.text)
    element.tail = _shorten(element.tail)
    if listing:
        _collapse_repeats(element)


def _drop(node: etree._Element) -> None:
    """Remove a node but keep the text that follows it."""
    parent = node.getparent()
    if parent is None:
        return
    tail = node.tail
    previous = node.getprevious()
    parent.remove(node)
    if tail and tail.strip():
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail


def _shorten(text: str | None) -> str | None:
    if text is None:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "..."


def _collapse_repeats(element: HtmlElement) -> None:
    children = [c for c in element if isinstance(c.tag, str)]
    if len(children) <= REPEATS_KEPT + 1:
        return
    signature = _signature(children[0])
    if sum(_signature(c) == signature for c in children) < len(children) * 0.8:
        return
    for child in children[REPEATS_KEPT:]:
        element.remove(child)
    element.append(
        etree.Comment(f" {len(children) - REPEATS_KEPT} more similar <{children[0].tag}> elements ")
    )


def _signature(element: HtmlElement) -> tuple[str, str]:
    return (str(element.tag), " ".join(sorted((element.get("class") or "").split())))
