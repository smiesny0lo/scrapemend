"""Structural fingerprints: what a page's markup looks like, ignoring its text.

A page is reduced to a set of features, one per element ("td.title") and one per
parent/child pair ("tr.notice>td.title"). In class names, every run of letters and
digits that contains a digit becomes "*", so generated names from different builds
("css-1a2b", "css-9z8y") still agree, as do utility classes ("mt-2", "mt-6").
Two pages are compared by the share of features they have in common (Jaccard
similarity), estimated with a fixed-size MinHash signature so a fingerprint stays
small no matter how big the page is.

Pages built from the same template score close to 1 even when their content
differs; a redesign scores far lower.
"""

import hashlib
import re
from collections.abc import Iterator

from lxml.html import HtmlElement

from scrapemend.extract.document import Document

SIGNATURE_SIZE = 64
CHANGED_BELOW = 0.6  # similarity under this means the page structure changed
_MASK = (1 << 61) - 1  # a Mersenne prime, the modulus for the hash permutations
_GENERATED = re.compile(r"[a-z]*\d[a-z\d]*")


def _permutations() -> list[tuple[int, int]]:
    out = []
    for i in range(SIGNATURE_SIZE):
        seed = hashlib.blake2b(f"scrapemend-minhash-{i}".encode(), digest_size=16).digest()
        a = int.from_bytes(seed[:8], "big") % _MASK or 1
        b = int.from_bytes(seed[8:], "big") % _MASK
        out.append((a, b))
    return out


_PERMUTATIONS = _permutations()


def features(doc: Document) -> set[str]:
    return set(_features(doc))


def _features(doc: Document) -> Iterator[str]:
    for element in doc.elements():
        own = _signature(element)
        yield own
        parent = element.getparent()
        if parent is not None:
            yield f"{_signature(parent)}>{own}"


def _signature(element: HtmlElement) -> str:
    classes = sorted({_GENERATED.sub("*", c.lower()) for c in (element.get("class") or "").split()})
    return ".".join([str(element.tag), *classes])


def fingerprint(doc: Document) -> str:
    """A MinHash signature of the page's structure, as a hex string."""
    hashes = [
        int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
        for f in features(doc)
    ]
    if not hashes:
        return "0" * 16 * SIGNATURE_SIZE
    signature = [min((a * h + b) % _MASK for h in hashes) for a, b in _PERMUTATIONS]
    return "".join(f"{value:016x}" for value in signature)


def similarity(a: str, b: str) -> float:
    """Estimated share of structural features two pages have in common, 0 to 1."""
    if len(a) != len(b) or len(a) != 16 * SIGNATURE_SIZE:
        raise ValueError("not a fingerprint")
    pairs = zip(_chunks(a), _chunks(b), strict=True)
    return sum(x == y for x, y in pairs) / SIGNATURE_SIZE


def _chunks(signature: str) -> Iterator[str]:
    for i in range(0, len(signature), 16):
        yield signature[i : i + 16]
