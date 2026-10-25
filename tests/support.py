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
    """Stands in for anthropic.Anthropic: answers `beta.messages.create` with structured
    outputs, in order, as the API returns them (JSON text after a thinking block), and
    keeps every request it got. An answer is a recording's name, or a RepairOutput."""

    def __init__(self, *answers: RepairOutput | str, stop_reason: str = "end_turn") -> None:
        # The JSON texts to answer with.
        self.answers = [
            (RECORDED / f"{a}.json").read_text("utf-8")
            if isinstance(a, str)
            else a.model_dump_json()
            for a in answers
        ]
        self.stop_reason = stop_reason
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        content = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
        if self.answers:
            content.append(SimpleNamespace(type="text", text=self.answers.pop(0)))
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber")
            if self.stop_reason == "refusal"
            else None,
            content=content,
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
