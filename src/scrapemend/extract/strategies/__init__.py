"""Extraction strategies: css, xpath, label, regex."""

from typing import assert_never

from scrapemend.extract.strategies.base import Strategy
from scrapemend.extract.strategies.css import CssStrategy
from scrapemend.extract.strategies.label import LabelStrategy
from scrapemend.extract.strategies.regex import RegexStrategy
from scrapemend.extract.strategies.xpath import XPathStrategy
from scrapemend.spec import CssSpec, LabelSpec, RegexSpec, StrategySpec, XPathSpec


def build(spec: StrategySpec) -> Strategy:
    """Turn a strategy from a spec file into one that can run."""
    match spec:
        case CssSpec():
            return CssStrategy(spec)
        case XPathSpec():
            return XPathStrategy(spec)
        case LabelSpec():
            return LabelStrategy(spec)
        case RegexSpec():
            return RegexStrategy(spec)
        case _:
            assert_never(spec)


__all__ = [
    "CssStrategy",
    "LabelStrategy",
    "RegexStrategy",
    "Strategy",
    "XPathStrategy",
    "build",
]
