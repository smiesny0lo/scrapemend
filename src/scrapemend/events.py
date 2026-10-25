"""Events and notifiers: how scrapemend tells someone that something needs attention."""

import logging
import sys
from importlib.metadata import entry_points
from typing import Literal, Protocol, TextIO

from pydantic import BaseModel, Field

EventKind = Literal[
    "run_failed",  # a run ended with a failure
    "run_recovered",  # a good run after a failed one
    "warning",  # data is fine, but something is wearing thin (e.g. fallbacks in use)
    "drift",  # data parses but looks different from the last good run
    "repair_proposed",  # a repair passed verification and waits for `scrapemend promote`
    "repair_rejected",  # a repair failed verification; the source is quarantined
    "repair_promoted",  # a repaired spec version is now active
    "quarantined",  # the source needs a human: no acceptable repair
]

_LEVELS: dict[EventKind, int] = {
    "run_failed": logging.ERROR,
    "run_recovered": logging.INFO,
    "warning": logging.WARNING,
    "drift": logging.WARNING,
    "repair_proposed": logging.WARNING,
    "repair_rejected": logging.ERROR,
    "repair_promoted": logging.WARNING,
    "quarantined": logging.ERROR,
}


class Event(BaseModel):
    kind: EventKind
    source_id: str
    message: str
    run_id: str | None = None
    status: str | None = None
    details: dict[str, object] = Field(default_factory=dict)

    @property
    def level(self) -> int:
        return _LEVELS[self.kind]


class Notifier(Protocol):
    def notify(self, event: Event) -> None: ...


class StreamNotifier:
    """Writes one line per event, e.g. to stderr next to the run summary."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream

    def notify(self, event: Event) -> None:
        stream = self.stream or sys.stderr
        label = logging.getLevelName(event.level)
        print(f"[{label}] {event.source_id}: {event.kind}: {event.message}", file=stream)


class LogNotifier:
    """Sends events to the `scrapemend` logger, for whatever handlers the host configures."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("scrapemend")

    def notify(self, event: Event) -> None:
        self.logger.log(
            event.level,
            "%s: %s: %s",
            event.source_id,
            event.kind,
            event.message,
            extra={"scrapemend_event": event.model_dump(mode="json")},
        )


class Notifiers:
    """Fans an event out to several notifiers. A notifier that fails is logged and
    skipped: a broken alert channel must never break a run."""

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self.notifiers = list(notifiers or [])

    def notify(self, event: Event) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(event)
            except Exception:
                logging.getLogger("scrapemend").exception("notifier %r failed", notifier)


ENTRY_POINT_GROUP = "scrapemend.notifiers"


def make_notifier(name: str) -> Notifier:
    """A notifier by name: built in (stdout, log) or registered by a plugin under the
    `scrapemend.notifiers` entry point group (a callable returning a notifier)."""
    if name == "stdout":
        return StreamNotifier()
    if name == "log":
        return LogNotifier()
    for entry_point in entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == name:
            notifier: Notifier = entry_point.load()()
            return notifier
    raise ValueError(f"unknown notifier {name!r} (built in: stdout, log)")
