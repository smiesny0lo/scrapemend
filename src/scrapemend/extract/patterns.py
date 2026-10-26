"""Regular expressions whose running time is bounded.

Regexes run on page text, which the scraped site controls, and a pattern that
backtracks catastrophically on some text can run for hours. So a regex runs on one
of two engines:

- re2 (google-re2) runs in time linear in the text, so no text can make it slow. A
  repair proposed by a provider runs its regexes on it, and one RE2 can't compile
  (a backreference, a lookaround) is refused. Its \\w, \\d and \\s match ASCII only.
- re, Python's own, keeps a hand-written spec's exact semantics. Each search runs in
  a worker process, which is stopped once a search takes longer than TIME_LIMIT.
"""

import atexit
import functools
import json
import queue
import re
import subprocess
import sys
import threading
from typing import Any, Literal, Protocol

import re2

Engine = Literal["re", "re2"]
TIME_LIMIT = 1.0  # seconds a search with Python's re may take

_RE2_OPTIONS = re2.Options()
_RE2_OPTIONS.log_errors = False  # errors are reported, not written to stderr


class RegexTimeout(Exception):
    """A search took longer than TIME_LIMIT and was stopped."""


class Pattern(Protocol):
    def first(self, text: str) -> str | None:
        """The first match's first group if the pattern has groups, else the whole
        match; None if nothing matched (or the group took no part)."""
        ...

    def fullmatch(self, text: str) -> bool: ...


def problem(regex: str, engine: Engine) -> str | None:
    """Why a regex doesn't compile on an engine, or None if it does."""
    try:
        if engine == "re2":
            re2.compile(regex, options=_RE2_OPTIONS)
        else:
            re.compile(regex)
    except re2.error as e:
        reason = e.args[0] if e.args else ""
        return reason.decode(errors="replace") if isinstance(reason, bytes) else str(reason)
    except re.error as e:
        return str(e)
    return None


@functools.lru_cache(maxsize=512)
def compile_pattern(regex: str, engine: Engine) -> Pattern:
    """A compiled pattern, on either engine. Raises ValueError if it doesn't compile."""
    if (reason := problem(regex, engine)) is not None:
        raise ValueError(reason)
    return _Re2(regex) if engine == "re2" else _TimeLimited(regex)


class _Re2:
    def __init__(self, regex: str) -> None:
        self._regex = re2.compile(regex, options=_RE2_OPTIONS)

    def first(self, text: str) -> str | None:
        match = self._regex.search(text)
        if match is None:
            return None
        found: str | None = match.group(1 if self._regex.groups else 0)
        return found

    def fullmatch(self, text: str) -> bool:
        return self._regex.fullmatch(text) is not None


class _TimeLimited:
    """Python's re, searched in the worker process."""

    def __init__(self, regex: str) -> None:
        self.regex = regex

    def first(self, text: str) -> str | None:
        found: str | None = _ask({"regex": self.regex, "text": text, "full": False})
        return found

    def fullmatch(self, text: str) -> bool:
        return bool(_ask({"regex": self.regex, "text": text, "full": True}))


# --- the worker process ----------------------------------------------------------------------

# Requests and answers are JSON lines (ASCII, since json escapes the rest).
_WORKER = r"""
import json, re, sys
compiled = {}
for line in sys.stdin:
    ask = json.loads(line)
    pattern = compiled.get(ask["regex"])
    if pattern is None:
        if len(compiled) > 512:
            compiled.clear()
        pattern = compiled[ask["regex"]] = re.compile(ask["regex"])
    if ask["full"]:
        answer = pattern.fullmatch(ask["text"]) is not None
    else:
        match = pattern.search(ask["text"])
        answer = None if match is None else match.group(1 if pattern.groups else 0)
    sys.stdout.write(json.dumps(answer) + "\n")
    sys.stdout.flush()
"""


class _Worker:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.answers: queue.Queue[bytes | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.answers.put(line)
        self.answers.put(None)  # the worker stopped

    def ask(self, request: dict[str, Any], limit: float) -> Any:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(request).encode("ascii") + b"\n")
        self.process.stdin.flush()
        try:
            answer = self.answers.get(timeout=limit)
        except queue.Empty:
            self.stop()
            raise RegexTimeout from None
        if answer is None:
            raise RuntimeError("the regex worker process stopped")
        return json.loads(answer)

    def stop(self) -> None:
        self.process.kill()
        self.process.wait()


_lock = threading.Lock()
_worker: _Worker | None = None


def _ask(request: dict[str, Any]) -> Any:
    """Ask the worker, starting one if there is none, or none that still runs."""
    global _worker
    with _lock:
        if _worker is None or _worker.process.poll() is not None:
            _worker = _Worker()
        try:
            return _worker.ask(request, TIME_LIMIT)
        except RegexTimeout:
            _worker = None  # stopped: the next search starts a new one
            raise


@atexit.register
def _stop() -> None:
    if _worker is not None and _worker.process.poll() is None:
        _worker.stop()
