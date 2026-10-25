import io
import logging

import pytest

from scrapemend.events import Event, LogNotifier, Notifier, Notifiers, StreamNotifier

EVENT = Event(kind="run_failed", source_id="demo", message="layout_changed: container", run_id="r1")


def test_stream_notifier() -> None:
    stream = io.StringIO()
    StreamNotifier(stream).notify(EVENT)
    assert stream.getvalue() == "[ERROR] demo: run_failed: layout_changed: container\n"


def test_log_notifier(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="scrapemend"):
        LogNotifier().notify(EVENT)
        LogNotifier().notify(Event(kind="run_recovered", source_id="demo", message="good again"))
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("ERROR", "demo: run_failed: layout_changed: container"),
        ("INFO", "demo: run_recovered: good again"),
    ]
    assert caplog.records[0].scrapemend_event["run_id"] == "r1"  # type: ignore[attr-defined]


class Broken:
    def notify(self, event: Event) -> None:
        raise RuntimeError("alert channel down")


def test_a_broken_notifier_does_not_stop_the_others(caplog: pytest.LogCaptureFixture) -> None:
    stream = io.StringIO()
    notifiers: list[Notifier] = [Broken(), StreamNotifier(stream)]
    Notifiers(notifiers).notify(EVENT)
    assert "run_failed" in stream.getvalue()
    assert "failed" in caplog.text and "alert channel down" in caplog.text
