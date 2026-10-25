"""Repair providers. They return structured spec diffs, never code."""

from importlib.metadata import entry_points

from scrapemend.heal.providers.base import ProviderError, RepairProvider
from scrapemend.heal.providers.none import NoneProvider, PatchProvider

ENTRY_POINT_GROUP = "scrapemend.providers"


def make_provider(name: str, *, model: str | None = None) -> RepairProvider:
    """A provider by name: built in (none, anthropic) or registered by a plugin under
    the `scrapemend.providers` entry point group (a callable returning a provider)."""
    if name == "none":
        return NoneProvider()
    if name == "anthropic":
        from scrapemend.heal.providers.anthropic import AnthropicProvider

        return AnthropicProvider(model=model)
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == name:
            provider: RepairProvider = entry_point.load()()
            return provider
    raise ValueError(f"unknown repair provider {name!r} (built in: none, anthropic)")


__all__ = ["NoneProvider", "PatchProvider", "ProviderError", "RepairProvider", "make_provider"]
