"""Spec patches: what a repair is allowed to change, and nothing more.

A repair, whether written by hand or proposed by an LLM, is a SpecPatch: new
selectors for items, links and pagination, and new strategies (with their
normalizing options) for existing fields. It cannot touch the start URL, the rate
limit, field types, required flags, expectations or heal settings, so a repair can
never point the scraper at another site, make it less polite, or loosen the checks
it is verified against. Applying a patch yields a new spec version, validated like
any spec file.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from scrapemend.spec.models import Normalizer, SelectorSpec, SourceSpec, StrategySpec


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldPatch(_Model):
    strategies: tuple[StrategySpec, ...] = Field(min_length=1)  # replaces the list, in order
    normalize: tuple[Normalizer, ...] | None = None  # None keeps the current setting
    date_format: str | None = None
    null_values: tuple[str, ...] | None = None


class SpecPatch(_Model):
    container: SelectorSpec | None = None
    follow: SelectorSpec | None = None
    next_page: SelectorSpec | None = None
    fields: dict[str, FieldPatch] = Field(default_factory=dict)
    rationale: str = ""  # why these changes: shown to the human reviewing the repair
    confidence: float = Field(default=0.5, ge=0, le=1)

    @property
    def is_empty(self) -> bool:
        return not (self.container or self.follow or self.next_page or self.fields)

    def changes(self) -> list[str]:
        """The spec parts this patch replaces, e.g. ["item.container", "fields.title"]."""
        parts = []
        if self.container:
            parts.append("item.container")
        if self.follow:
            parts.append("item.follow")
        if self.next_page:
            parts.append("fetch.paginate.next")
        return parts + [f"fields.{name}" for name in self.fields]

    def apply(self, spec: SourceSpec, version: int) -> SourceSpec:
        """A new spec version with this patch applied. Raises ValueError (pydantic's
        ValidationError included) if the patch doesn't fit the spec."""
        unknown = set(self.fields) - set(spec.fields)
        if unknown:
            raise ValueError(f"patch names unknown fields: {', '.join(sorted(unknown))}")
        if self.next_page is not None and spec.fetch.paginate is None:
            raise ValueError("patch sets next_page, but the spec has no pagination")
        data: dict[str, Any] = spec.model_dump(mode="json")
        data["version"] = version
        if self.container is not None:
            data["item"]["container"] = self.container.model_dump(mode="json")
        if self.follow is not None:
            data["item"]["follow"] = self.follow.model_dump(mode="json")
        if self.next_page is not None:
            data["fetch"]["paginate"]["next"] = self.next_page.model_dump(mode="json")
        for name, field in self.fields.items():
            target = data["fields"][name]
            target["strategies"] = [
                s.model_dump(mode="json", exclude_none=True) for s in field.strategies
            ]
            if field.normalize is not None:
                target["normalize"] = list(field.normalize)
            if field.date_format is not None:
                target["date_format"] = field.date_format
            if field.null_values is not None:
                target["null_values"] = list(field.null_values)
        return SourceSpec.model_validate(data)
