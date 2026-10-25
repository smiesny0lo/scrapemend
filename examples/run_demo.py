"""The scrapemend demo in one command: watch a scraper break and heal itself.

    python examples/run_demo.py                       # offline, with a hand-written repair
    python examples/run_demo.py --provider anthropic  # Claude writes the repair
    python examples/run_demo.py --layout 3            # the harder redesign

It starts the demo site, scrapes it, redesigns the site, and lets scrapemend detect
the breakage, repair the spec, verify the repair against known-good records,
promote it and scrape again, all through the real `scrapemend` CLI. It exits
non-zero if any step doesn't do what it should, so CI runs it too.
"""

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "examples" / "sources" / "demo.yaml"


WORK: Path | None = None  # the demo's temporary state directory, shown as $WORK


def say(text: str) -> None:
    print(f"\n== {text}", flush=True)


def shown(arg: str) -> str:
    """An argument as printed: repo paths relative, the temporary directory as $WORK."""
    if WORK is not None and arg.startswith(str(WORK)):
        return "$WORK" + arg[len(str(WORK)) :].replace("\\", "/")
    if arg.startswith(str(ROOT)):
        return arg[len(str(ROOT)) + 1 :].replace("\\", "/")
    return arg


def scrapemend(*args: str, expect: int = 0) -> str:
    """Run the CLI, show what it reports, and check its exit code."""
    print(f"$ scrapemend {' '.join(shown(a) for a in args)}", flush=True)
    done = subprocess.run(
        [sys.executable, "-m", "scrapemend", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    for line in (done.stderr + done.stdout).splitlines():
        if line.strip() and not line.startswith("  snapshots:"):
            print(f"  {line}", flush=True)
    if done.returncode != expect:
        sys.exit(f"demo failed: expected exit code {expect}, got {done.returncode}")
    return done.stderr + done.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--provider", default=None, help="repair with this provider (e.g. anthropic)"
    )
    parser.add_argument(
        "--layout", type=int, choices=(2, 3), default=2, help="the redesign to heal"
    )
    args = parser.parse_args()
    started = time.monotonic()

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    site = subprocess.Popen(
        [sys.executable, "-m", "examples.mutating_site", "--port", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                httpx.get(f"{base}/robots.txt", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.1)
        with tempfile.TemporaryDirectory(prefix="scrapemend-demo-") as tmp:
            global WORK
            work = WORK = Path(tmp)
            state = ["--db", str(work / "state.db"), "--snapshots", str(work / "snapshots")]
            where = ["--start", f"{base}/notices", *state]

            say(f"1. The demo site is up at {base}. Scrape it with examples/sources/demo.yaml")
            scrapemend("run", str(SPEC), "--out", str(work / "before.jsonl"), *where)

            say(f"2. The site is redesigned (layout v{args.layout}); the spec now finds nothing")
            httpx.post(f"{base}/admin/layout/{args.layout}")
            repair = (
                ["--provider", args.provider]
                if args.provider
                else ["--patch", str(ROOT / "examples" / "repairs" / f"demo-v{args.layout}.yaml")]
            )
            report = scrapemend(
                "run", str(SPEC), "--out", str(work / "broken.jsonl"), *repair, *where, expect=3
            )
            if "status: layout_changed" not in report or "heal: proposed" not in report:
                sys.exit("demo failed: expected a detected layout change and a verified repair")

            say("3. What the verified repair changes (it waits for a human to promote it)")
            scrapemend("diff", str(SPEC), "1", *state[:2])

            say("4. Promote it: the repaired spec becomes version 2")
            scrapemend("promote", str(SPEC), "1", *state[:2])

            say("5. Scrape the redesigned site again")
            scrapemend("run", str(SPEC), "--out", str(work / "after.jsonl"), *where)

            before = [
                json.loads(line) for line in (work / "before.jsonl").read_text("utf-8").splitlines()
            ]
            after = [
                json.loads(line) for line in (work / "after.jsonl").read_text("utf-8").splitlines()
            ]
            if before != after:
                sys.exit("demo failed: the healed scrape differs from the original")
            seconds = time.monotonic() - started
            say(f"Healed: {len(after)} records, identical to before the redesign ({seconds:.0f}s)")
    finally:
        site.terminate()
        site.wait(timeout=10)


if __name__ == "__main__":
    main()
