from scrapemend.heal.context import RepairContext
from scrapemend.spec.patch import SpecPatch


class NoneProvider:
    """Never proposes anything: failures are detected and reported, and a human repairs
    the spec. The default, so scrapemend works offline and without an LLM."""

    name = "none"

    def propose(self, context: RepairContext) -> SpecPatch | None:
        return None


class PatchProvider:
    """Proposes one given patch, e.g. a repair written by hand, so it goes through the
    same verification gate as any other."""

    def __init__(self, patch: SpecPatch, name: str = "patch") -> None:
        self.patch = patch
        self.name = name

    def propose(self, context: RepairContext) -> SpecPatch | None:
        return None if context.attempts else self.patch
