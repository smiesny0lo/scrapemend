"""Extraction strategies: css, xpath, label, regex, and custom ones from plugins."""

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, assert_never

from scrapemend.extract.strategies.base import Locating, Strategy, StrategyTimeout
from scrapemend.extract.strategies.css import CssStrategy
from scrapemend.extract.strategies.label import LabelStrategy
from scrapemend.extract.strategies.regex import RegexStrategy
from scrapemend.extract.strategies.xpath import XPathStrategy
from scrapemend.spec import CssSpec, CustomSpec, LabelSpec, RegexSpec, StrategySpec, XPathSpec

ENTRY_POINT_GROUP = "scrapemend.strategies"

# name -> factory taking the spec's `options` and returning a Strategy
StrategyFactory = Callable[[dict[str, Any]], Strategy]
_registry: dict[str, StrategyFactory] = {}


class UnknownStrategy(ValueError):
    """A spec names a custom strategy that no installed plugin provides."""


def register_strategy(name: str, factory: StrategyFactory) -> None:
    """Make a custom strategy available as `custom: {name: ...}` in specs.

    Plugins can also register under the `scrapemend.strategies` entry point group,
    which needs no import: the entry point names the factory.
    """
    _registry[name] = factory


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
        case CustomSpec():
            return _factory(spec.custom.name)(spec.custom.options)
        case _:
            assert_never(spec)


def _factory(name: str) -> StrategyFactory:
    if name in _registry:
        return _registry[name]
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == name:
            factory: StrategyFactory = entry_point.load()
            _registry[name] = factory
            return factory
    raise UnknownStrategy(f"no installed plugin provides the custom strategy {name!r}")


__all__ = [
    "CssStrategy",
    "LabelStrategy",
    "Locating",
    "RegexStrategy",
    "Strategy",
    "StrategyFactory",
    "StrategyTimeout",
    "UnknownStrategy",
    "XPathStrategy",
    "build",
    "register_strategy",
]
