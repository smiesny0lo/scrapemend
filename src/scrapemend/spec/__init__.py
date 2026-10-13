"""Source spec models, YAML loading, and version history."""

from scrapemend.spec.loader import Problem, SpecError, load_spec, parse_spec
from scrapemend.spec.models import (
    CssSpec,
    FieldSpec,
    LabelSpec,
    RegexSpec,
    SelectorSpec,
    SourceSpec,
    StrategySpec,
    XPathSpec,
)

__all__ = [
    "CssSpec",
    "FieldSpec",
    "LabelSpec",
    "Problem",
    "RegexSpec",
    "SelectorSpec",
    "SourceSpec",
    "SpecError",
    "StrategySpec",
    "XPathSpec",
    "load_spec",
    "parse_spec",
]
