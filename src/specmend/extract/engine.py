"""The extraction engine: run each field's strategies in order and keep the first that works."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from specmend.extract.document import Document
from specmend.extract.normalize import Value, convert
from specmend.extract.strategies import Strategy, StrategyTimeout, build
from specmend.spec import FieldSpec


@dataclass(frozen=True)
class FieldResult:
    value: Value
    raw: str | None  # the text the strategy found, before normalizing
    strategy: int | None  # index of the winning strategy in the field's list; None if none won
    error: str | None = None  # why there's no value although text was found

    @property
    def used_fallback(self) -> bool:
        """A later strategy won: the field's first choice no longer works on this page."""
        return self.strategy is not None and self.strategy > 0


@dataclass(frozen=True)
class RecordResult:
    url: str | None
    fields: Mapping[str, FieldResult]

    @property
    def record(self) -> dict[str, Value]:
        """The extracted values, keyed by field name, plus the page URL."""
        return {"url": self.url} | {name: result.value for name, result in self.fields.items()}


class Extractor:
    """Extracts a record from a page using a spec's fields. Build once, reuse per page."""

    def __init__(self, fields: Mapping[str, FieldSpec]) -> None:
        self._fields = {
            name: (spec, [build(strategy) for strategy in spec.strategies])
            for name, spec in fields.items()
        }
        # (field, strategy index) -> pages it timed out on, since this extractor was built
        self.timeouts: Counter[tuple[str, int]] = Counter()

    def extract(self, doc: Document) -> RecordResult:
        results = {
            name: self._extract_field(name, spec, strategies, doc)
            for name, (spec, strategies) in self._fields.items()
        }
        return RecordResult(url=doc.url, fields=results)

    def _extract_field(
        self, name: str, spec: FieldSpec, strategies: list[Strategy], doc: Document
    ) -> FieldResult:
        """The first strategy whose text converts to the field's type wins. Text that
        doesn't convert (a price reading "See documents") means that strategy found the
        wrong thing, so the next one gets a chance; so does one that timed out."""
        failure: FieldResult | None = None
        for index, strategy in enumerate(strategies):
            try:
                raw = strategy.extract(doc)
            except StrategyTimeout as e:
                self.timeouts[name, index] += 1
                failure = failure or FieldResult(None, None, None, f"strategy {index}: {e}")
                continue
            if raw is None:
                continue
            try:
                value = convert(raw, spec, base_url=doc.url)
            except ValueError as e:
                failure = failure or FieldResult(None, raw, None, f"strategy {index}: {e}")
                continue
            return FieldResult(value, raw, index)
        return failure or FieldResult(None, None, None)
