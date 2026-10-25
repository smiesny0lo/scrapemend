"""Helpers shared by unit and integration tests."""

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn
from fastapi import FastAPI

from scrapemend.heal.providers.anthropic import RepairOutput

RECORDED = Path(__file__).parent / "fixtures" / "recorded"


class RecordedClaude:
    """Stands in for anthropic.Anthropic: answers `beta.messages.parse` with recorded
    structured outputs, in order, and keeps every request it got."""

    def __init__(self, *answers: RepairOutput | str, stop_reason: str = "end_turn") -> None:
        self.answers = [recorded(a) if isinstance(a, str) else a for a in answers]
        self.stop_reason = stop_reason
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        answer = self.answers.pop(0) if self.answers else None
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber")
            if self.stop_reason == "refusal"
            else None,
            parsed_output=answer,
            usage=None,
        )


def recorded(name: str) -> RepairOutput:
    """A structured answer Claude gave in a live run, e.g. recorded("anthropic-demo-v2")."""
    return RepairOutput.model_validate(json.loads((RECORDED / f"{name}.json").read_text("utf-8")))


class FakeClock:
    """Stands in for time.monotonic and time.sleep: sleeping only moves the clock forward."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Run an app on a free local port in a background thread; yields its base URL."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
