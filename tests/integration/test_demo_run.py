"""End-to-end runs of the demo spec against the demo site, served over real HTTP.

A fake clock stands in for time on both sides (the fetcher's waits and the site's
rate limiter), so retries and Retry-After cost no real time.
"""

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from examples.mutating_site.app import create_app
from examples.mutating_site.records import Notice, generate_notices
from scrapemend.cli import app as cli
from scrapemend.fetch import BlockedError, HttpFetcher, RobotsDisallowed, TransientError
from scrapemend.pipeline import SourceRun
from scrapemend.snapshots import FilesystemSnapshotStore
from scrapemend.spec import SourceSpec, load_spec
from tests.support import FakeClock, serve

pytestmark = pytest.mark.integration

DEMO_SPEC_PATH = Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml"
NOTICES = generate_notices()


@contextmanager
def demo_site(
    clock: FakeClock, layout: int = 1, chaos: str = "off", rate: float = 0.2
) -> Iterator[str]:
    app = create_app(layout=layout, chaos=chaos, failure_rate=rate)
    app.state.chaos.clock = clock
    with serve(app) as base_url:
        yield base_url


def demo_run(
    base_url: str, clock: FakeClock, tmp_path: Path, max_attempts: int = 4, **kwargs: Any
) -> SourceRun:
    data = load_spec(DEMO_SPEC_PATH).model_dump(mode="json")
    data["fetch"]["start"] = f"{base_url}/notices"
    spec = SourceSpec.model_validate(data)
    fetcher = HttpFetcher.for_spec(
        spec.fetch,
        clock=clock,
        sleep=clock.sleep,
        rng=random.Random(0),
        max_attempts=max_attempts,
    )
    return SourceRun(spec, fetcher, FilesystemSnapshotStore(tmp_path), **kwargs)


def expected(notice: Notice, base_url: str) -> dict[str, Any]:
    return {
        "url": f"{base_url}/notices/{notice.id}",
        "reference": notice.id,
        "title": notice.title,
        "authority": notice.authority,
        "category": notice.category,
        "location": notice.location,
        "published": notice.published,
        "deadline": notice.deadline,
        "value": notice.value,
        "status": notice.status,
        "description": notice.description,
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# --- the Phase 3 milestone ---------------------------------------------------------------


def test_scrapes_every_record_from_layout_v1(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock) as base_url:
        source_run = demo_run(base_url, clock, tmp_path)
        records = {r.url: r.record for r in source_run.records()}

    assert records == {f"{base_url}/notices/{n.id}": expected(n, base_url) for n in NOTICES}
    summary = source_run.summary
    assert (summary.listing_pages, summary.items, summary.records) == (10, 200, 200)
    assert summary.failures == [] and summary.fallbacks == {} and summary.errors == {}
    assert summary.empty == {"value": sum(n.value is None for n in NOTICES)}

    store = FilesystemSnapshotStore(tmp_path)
    kinds = [store.get(i).kind for i in store.snapshots("tenders-demo", summary.run_id)]
    assert (kinds.count("listing"), kinds.count("detail")) == (10, 200)


def test_stored_snapshots_reparse_without_refetching(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock) as base_url:
        source_run = demo_run(base_url, clock, tmp_path, limit=5)
        live = [r.record for r in source_run.records()]
    # The site is gone now; the snapshots are all we need.
    store = FilesystemSnapshotStore(tmp_path)
    details = [
        store.get(i)
        for i in store.snapshots("tenders-demo", source_run.summary.run_id)
        if store.get(i).kind == "detail"
    ]
    reparsed = [source_run._extractor.extract(s.document()).record for s in details]
    assert reparsed == live


def test_layout_v2_breaks_the_v1_spec(clock: FakeClock, tmp_path: Path) -> None:
    # The listing's container selector no longer matches: no items at all. Detecting
    # and reporting this as layout_changed is Phase 4.
    with demo_site(clock, layout=2) as base_url:
        source_run = demo_run(base_url, clock, tmp_path)
        assert list(source_run.records()) == []
    assert (source_run.summary.listing_pages, source_run.summary.items) == (1, 0)


# --- politeness against chaos -------------------------------------------------------------


def test_survives_flaky_server(clock: FakeClock, tmp_path: Path) -> None:
    # With 30% of requests failing, 4 attempts would exhaust somewhere among ~210
    # requests (0.3^4 per request) and a failed listing page rightly ends the run.
    # 8 attempts make that vanishingly rare; the point here is that retries work.
    with demo_site(clock, chaos="flaky", rate=0.3) as base_url:
        source_run = demo_run(base_url, clock, tmp_path, max_attempts=8)
        assert len(list(source_run.records())) == 200
    assert source_run.summary.failures == []
    assert isinstance(source_run.fetcher, HttpFetcher) and source_run.fetcher.retries > 30


def test_settles_below_the_rate_limit(clock: FakeClock, tmp_path: Path) -> None:
    # The site allows 10 requests per 10 s; the spec asks for 20 per second.
    with demo_site(clock, chaos="ratelimit") as base_url:
        source_run = demo_run(base_url, clock, tmp_path)
        assert len(list(source_run.records())) == 200
    fetcher = source_run.fetcher
    assert isinstance(fetcher, HttpFetcher)
    # Each 429 doubles the spacing, so only the first few requests over the limit
    # get turned away; after that the run stays under it.
    assert 0 < fetcher.retries <= 8
    assert source_run.summary.failures == []


def test_stops_when_blocked(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock, chaos="blocked") as base_url:
        source_run = demo_run(base_url, clock, tmp_path)
        with pytest.raises(BlockedError, match="HTTP 403"):
            list(source_run.records())
    fetcher = source_run.fetcher
    assert isinstance(fetcher, HttpFetcher)
    assert (fetcher.requests, fetcher.retries) == (2, 0)  # robots.txt, first page, nothing more


def test_gives_up_on_a_dead_server(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock, chaos="flaky", rate=1.0) as base_url:
        source_run = demo_run(base_url, clock, tmp_path)
        with pytest.raises(TransientError, match="HTTP 503 after 4 attempts"):
            list(source_run.records())


def test_obeys_the_demo_robots_txt(clock: FakeClock) -> None:
    with demo_site(clock) as base_url:
        fetcher = HttpFetcher(clock=clock, sleep=clock.sleep)
        with pytest.raises(RobotsDisallowed):
            fetcher.fetch(f"{base_url}/admin/layout")


# --- the CLI --------------------------------------------------------------------------------

runner = CliRunner()


def test_cli_run_writes_jsonl(clock: FakeClock, tmp_path: Path) -> None:
    out = tmp_path / "out.jsonl"
    with demo_site(clock) as base_url:
        result = runner.invoke(
            cli,
            ["run", str(DEMO_SPEC_PATH), "--start", f"{base_url}/notices", "--limit", "5",
             "--out", str(out), "--snapshots", str(tmp_path / "snapshots")],
        )  # fmt: skip
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 5
    first = lines[0]
    assert first["reference"].startswith("TN-2026-")
    assert first["deadline"] == first["deadline"][:10]  # ISO date string
    assert first["value"] is None or first["value"].replace(".", "").isdigit()
    assert "1 listing pages, 5 items, 5 records, 0 failed" in result.output


def test_cli_run_to_stdout(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock) as base_url:
        result = runner.invoke(
            cli,
            ["run", str(DEMO_SPEC_PATH), "--start", f"{base_url}/notices", "--limit", "2",
             "--snapshots", str(tmp_path)],
        )  # fmt: skip
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["url"] for r in records] == [f"{base_url}/notices/{n['reference']}" for n in records]


def test_cli_run_exits_2_when_blocked(clock: FakeClock, tmp_path: Path) -> None:
    with demo_site(clock, chaos="blocked") as base_url:
        result = runner.invoke(
            cli,
            ["run", str(DEMO_SPEC_PATH), "--start", f"{base_url}/notices",
             "--snapshots", str(tmp_path)],
        )  # fmt: skip
    assert result.exit_code == 2
    assert "run aborted: blocked by the site" in result.output


def test_cli_run_rejects_a_bad_start_url(tmp_path: Path) -> None:
    result = runner.invoke(cli, ["run", str(DEMO_SPEC_PATH), "--start", "not a url"])
    assert result.exit_code == 1
    assert "--start" in result.output
