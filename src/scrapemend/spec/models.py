"""Source spec models: the declarative description of how to scrape one source.

Specs are data, never code. They are frozen because a change to a spec is a new
version, never an in-place edit. Every model forbids unknown keys, so a typo in a
spec file is an error rather than a silently ignored setting.
"""

import re
from typing import Annotated, Any, Literal, Self

from lxml import etree
from lxml.cssselect import CSSSelector, SelectorError
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    HttpUrl,
    Tag,
    ValidationInfo,
    field_validator,
    model_validator,
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- strategies -------------------------------------------------------------------

_ATTR_SUFFIX = re.compile(r"\s*::attr\(([\w-]+)\)$")


class CssSpec(_Model):
    """A CSS selector. Append `::attr(name)` to read an attribute instead of the text."""

    css: str = Field(min_length=1)

    @field_validator("css")
    @classmethod
    def _compiles(cls, value: str) -> str:
        selector = _ATTR_SUFFIX.sub("", value)
        try:
            CSSSelector(selector, translator="html")
        except SelectorError as e:
            raise ValueError(f"invalid CSS selector {selector!r}: {e}") from None
        return value

    @property
    def selector(self) -> str:
        return _ATTR_SUFFIX.sub("", self.css)

    @property
    def attr(self) -> str | None:
        match = _ATTR_SUFFIX.search(self.css)
        return match.group(1) if match else None


class XPathSpec(_Model):
    """An XPath expression, e.g. `//h1` or `//a[@rel='next']/@href`."""

    xpath: str = Field(min_length=1)

    @field_validator("xpath")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            etree.XPath(value)
        except etree.XPathSyntaxError:
            raise ValueError(f"invalid XPath {value!r}") from None
        return value


class RegexSpec(_Model):
    """A regular expression over the text. The first capture group is the value if
    the pattern has one, otherwise the whole match."""

    regex: str = Field(min_length=1)

    @field_validator("regex")
    @classmethod
    def _compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as e:
            raise ValueError(f"invalid regex {value!r}: {e}") from None
        return value


class LabelOptions(_Model):
    text: str = Field(min_length=1)
    # next_sibling: the element after the label (<dt>Deadline</dt><dd>VALUE</dd>)
    # after:        the rest of the label's own element (<p>Deadline: VALUE</p>)
    relation: Literal["next_sibling", "after"] = "next_sibling"


class LabelSpec(_Model):
    """Find the element showing a label such as "Deadline" and read the value next to it.
    `label: Deadline` is shorthand for `label: {text: Deadline}`."""

    label: LabelOptions

    @field_validator("label", mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        return {"text": value} if isinstance(value, str) else value


_STRATEGY_TAGS: dict[type[BaseModel], str] = {
    CssSpec: "css",
    XPathSpec: "xpath",
    LabelSpec: "label",
    RegexSpec: "regex",
}


def _strategy_tag(value: Any) -> str | None:
    """Pick the strategy type from its single key, e.g. `{css: "h1"}` -> "css"."""
    if isinstance(value, dict):
        keys = [tag for tag in _STRATEGY_TAGS.values() if tag in value]
        return keys[0] if len(keys) == 1 else None
    return _STRATEGY_TAGS.get(type(value))


StrategySpec = Annotated[
    Annotated[CssSpec, Tag("css")]
    | Annotated[XPathSpec, Tag("xpath")]
    | Annotated[LabelSpec, Tag("label")]
    | Annotated[RegexSpec, Tag("regex")],
    Discriminator(
        _strategy_tag,
        custom_error_type="strategy_type",
        custom_error_message="expected exactly one of: css, xpath, label, regex",
    ),
]
"""Any extraction strategy, used for field values."""


def _selector_tag(value: Any) -> str | None:
    tag = _strategy_tag(value)
    return tag if tag in ("css", "xpath") else None


SelectorSpec = Annotated[
    Annotated[CssSpec, Tag("css")] | Annotated[XPathSpec, Tag("xpath")],
    Discriminator(
        _selector_tag,
        custom_error_type="selector_type",
        custom_error_message="expected exactly one of: css, xpath",
    ),
]
"""A structural selector (css or xpath), used to find elements and links."""


# --- fields -----------------------------------------------------------------------


def _snake_case(name: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise ValueError(f"field name {name!r} must be lowercase letters, digits and _")
    return name


FieldName = Annotated[str, AfterValidator(_snake_case)]
FieldType = Literal["str", "int", "decimal", "date", "url"]
Normalizer = Literal["strip", "collapse_whitespace", "lower", "strip_currency"]


class FieldSpec(_Model):
    type: FieldType = "str"
    required: bool = False
    volatile: bool = False  # value may legitimately change between runs (e.g. a price)
    strategies: tuple[StrategySpec, ...] = Field(min_length=1)  # tried in order
    normalize: tuple[Normalizer, ...] = ()  # applied in order before type conversion
    date_format: str | None = None  # strptime format; only for type: date
    null_values: tuple[str, ...] = ()  # texts meaning "no value", e.g. "N/A" (any case)

    @model_validator(mode="after")
    def _date_format_needs_date(self) -> Self:
        if self.date_format is not None and self.type != "date":
            raise ValueError("date_format only applies to fields with type: date")
        return self


# --- fetching and items -------------------------------------------------------------


class PaginateSpec(_Model):
    next: SelectorSpec  # resolves to the next page's URL
    max_pages: int = Field(default=50, ge=1)


class RateSpec(_Model):
    rps: float = Field(default=1.0, gt=0)  # requests per second to this source's domain


class FetchSpec(_Model):
    start: HttpUrl
    paginate: PaginateSpec | None = None
    rate: RateSpec = Field(default_factory=RateSpec)
    timeout: float = Field(default=30.0, gt=0)  # seconds per request


class ItemSpec(_Model):
    container: SelectorSpec  # one match per item on a listing page
    key: str = "url"  # field that identifies an item across runs; "url" = detail page URL
    follow: SelectorSpec | None = None  # link to the item's detail page, where fields are read


class ExpectSpec(_Model):
    min_items_per_run: int = Field(default=0, ge=0)
    max_null_rate: dict[str, Annotated[float, Field(ge=0, le=1)]] = Field(default_factory=dict)


class HealSpec(_Model):
    mode: Literal["off", "propose", "auto"] = "propose"
    anchors: int = Field(default=10, ge=0)  # known-good records kept for verification


# --- source ---------------------------------------------------------------------------


class SourceSpec(_Model):
    # Declaration order is validation order: `item` and `expect` are checked against
    # `fields`, so `fields` must come first.
    id: str
    version: int = Field(default=1, ge=1)
    fetch: FetchSpec
    fields: dict[FieldName, FieldSpec] = Field(min_length=1)
    item: ItemSpec
    expect: ExpectSpec = Field(default_factory=ExpectSpec)
    heal: HealSpec = Field(default_factory=HealSpec)

    @field_validator("id")
    @classmethod
    def _slug(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
            raise ValueError("id must be lowercase letters, digits, - and _ (like tenders-demo)")
        return value

    @field_validator("fields")
    @classmethod
    def _url_is_reserved(cls, fields: dict[str, FieldSpec]) -> dict[str, FieldSpec]:
        if "url" in fields:
            raise ValueError("'url' is reserved for the item's page URL; rename the field")
        return fields

    @field_validator("item")
    @classmethod
    def _key_exists(cls, item: ItemSpec, info: ValidationInfo) -> ItemSpec:
        fields: dict[str, FieldSpec] | None = info.data.get("fields")
        if item.key == "url":
            if item.follow is None:
                raise ValueError(
                    "key 'url' needs item.follow; without detail pages, "
                    "every item on a listing page shares the same URL"
                )
        elif fields is not None:
            if item.key not in fields:
                raise ValueError(f"key {item.key!r} is not a field (fields: {', '.join(fields)})")
            if not fields[item.key].required:
                raise ValueError(f"key field {item.key!r} must be required: true")
        return item

    @field_validator("expect")
    @classmethod
    def _null_rates_name_fields(cls, expect: ExpectSpec, info: ValidationInfo) -> ExpectSpec:
        fields: dict[str, FieldSpec] | None = info.data.get("fields")
        if fields is not None and (unknown := set(expect.max_null_rate) - set(fields)):
            raise ValueError(f"max_null_rate names unknown fields: {', '.join(sorted(unknown))}")
        return expect
