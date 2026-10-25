from typing import Protocol

from scrapemend.heal.context import RepairContext
from scrapemend.spec.patch import SpecPatch


class ProviderError(Exception):
    """The provider could not produce a repair (API failure, refusal, unusable output)."""


class RepairProvider(Protocol):
    """Proposes a repair for a broken spec.

    A provider returns data, never code: a SpecPatch, which the verification gate
    then judges on pages under the new layout. Returning None means "no repair"
    (nothing to suggest, or nothing new after earlier attempts were rejected).
    `context.attempts` holds earlier proposals for the same failure and the
    problems verification found with them.
    """

    name: str

    def propose(self, context: RepairContext) -> SpecPatch | None: ...
