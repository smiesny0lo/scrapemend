import codecs
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from examples.mutating_site.app import create_app
from scrapemend.extract import Extractor
from scrapemend.runs import new_run_id
from scrapemend.snapshots import FilesystemSnapshotStore, Snapshot, SnapshotStore, decode_body
from scrapemend.spec import load_spec

RUN = "20261013T190512Z-3f9a"
HTML = "text/html; charset=utf-8"


def snap(**overrides: Any) -> Snapshot:
    data: dict[str, Any] = {
        "source_id": "tenders-demo",
        "run_id": RUN,
        "spec_version": 1,
        "kind": "listing",
        "url": "http://localhost:8000/notices",
        "status": 200,
        "headers": {"Content-Type": HTML},
        "fetched_at": datetime(2026, 10, 13, 19, 5, 12, tzinfo=UTC),
        "body": b"<html><body><h1>Notices</h1></body></html>",
    }
    return Snapshot.model_validate(data | overrides)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemSnapshotStore:
    return FilesystemSnapshotStore(tmp_path)


# --- storing and loading ------------------------------------------------------------------


def test_round_trip(store: FilesystemSnapshotStore) -> None:
    original = snap(fingerprint="abc123")
    assert store.get(store.put(original)) == original


def test_files_on_disk(store: FilesystemSnapshotStore, tmp_path: Path) -> None:
    store.put(snap())
    run_dir = tmp_path / "tenders-demo" / RUN
    assert sorted(p.name for p in run_dir.iterdir()) == [
        "00001-listing.html",
        "00001-listing.meta.json",
    ]
    assert (run_dir / "00001-listing.html").read_bytes() == snap().body
    meta = json.loads((run_dir / "00001-listing.meta.json").read_text(encoding="utf-8"))
    assert "body" not in meta
    assert meta["body_file"] == "00001-listing.html"
    assert meta["fetched_at"] == "2026-10-13T19:05:12Z"


def test_ids_follow_fetch_order(store: FilesystemSnapshotStore) -> None:
    ids = [store.put(snap()), store.put(snap(kind="detail")), store.put(snap(kind="detail"))]
    assert ids == [f"tenders-demo/{RUN}/0000{n}" for n in (1, 2, 3)]
    assert store.snapshots("tenders-demo", RUN) == ids
    assert [store.get(i).kind for i in ids] == ["listing", "detail", "detail"]


def test_runs_oldest_first(store: FilesystemSnapshotStore) -> None:
    later, earlier = "20261014T080000Z-bbbb", "20261013T080000Z-aaaa"
    store.put(snap(run_id=later))
    store.put(snap(run_id=earlier))
    assert store.runs("tenders-demo") == [earlier, later]
    assert store.runs("unknown-source") == []


def test_runs_in_the_same_second_by_their_first_page(store: FilesystemSnapshotStore) -> None:
    # A run id is the second the run started and a random suffix: the suffix can't
    # tell which of two runs in one second came first, their first page can.
    first, second = "20261013T080000Z-ffff", "20261013T080000Z-0000"
    at = datetime(2026, 10, 13, 8, 0, 0, tzinfo=UTC)
    store.put(snap(run_id=first, fetched_at=at))
    store.put(snap(run_id=second, fetched_at=at.replace(microsecond=500_000)))
    store.put(snap(run_id=first, fetched_at=at.replace(microsecond=900_000)))
    assert store.runs("tenders-demo") == [first, second]


def test_delete_run(store: FilesystemSnapshotStore, tmp_path: Path) -> None:
    gone, kept = "20261013T080000Z-aaaa", "20261014T080000Z-bbbb"
    for run_id in (gone, gone, kept):
        store.put(snap(run_id=run_id))
    store.delete_run("tenders-demo", gone)
    assert store.runs("tenders-demo") == [kept]
    assert store.snapshots("tenders-demo", gone) == []
    assert not (tmp_path / "tenders-demo" / gone).exists()
    store.delete_run("tenders-demo", gone)  # nothing left to delete is fine
    with pytest.raises(KeyError):
        store.delete_run("tenders-demo", "../escape")


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("application/pdf", ".pdf"),
        ("application/json; charset=utf-8", ".json"),
        ("TEXT/HTML", ".html"),
        ("", ".bin"),
    ],
)
def test_body_extension_follows_content_type(
    store: FilesystemSnapshotStore, tmp_path: Path, content_type: str, extension: str
) -> None:
    store.put(snap(headers={"content-type": content_type}))
    assert (tmp_path / "tenders-demo" / RUN / f"00001-listing{extension}").exists()


def test_half_written_snapshot_is_ignored(store: FilesystemSnapshotStore, tmp_path: Path) -> None:
    # A crash after the body but before the metadata leaves a lone body file.
    run_dir = tmp_path / "tenders-demo" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "00007-detail.html").write_bytes(b"<html>partial")
    assert store.snapshots("tenders-demo", RUN) == []
    assert store.runs("tenders-demo") == []
    assert store.put(snap()).endswith("/00008")  # and it is never overwritten


@pytest.mark.parametrize(
    "snapshot_id",
    [f"tenders-demo/{RUN}/00099", "../../etc/00001", f"tenders-demo/{RUN}", "a/b/1", ""],
)
def test_get_unknown_or_malformed_id(store: FilesystemSnapshotStore, snapshot_id: str) -> None:
    store.put(snap())
    with pytest.raises(KeyError):
        store.get(snapshot_id)


def test_store_satisfies_protocol(tmp_path: Path) -> None:
    store: SnapshotStore = FilesystemSnapshotStore(tmp_path)  # checked by mypy
    assert store.runs("x") == []


# --- the snapshot model ---------------------------------------------------------------------


@pytest.mark.parametrize("field", ["source_id", "run_id"])
@pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".hidden"])
def test_ids_must_be_plain_path_segments(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        snap(**{field: bad})


def test_fetch_time_must_have_a_timezone() -> None:
    with pytest.raises(ValidationError):
        snap(fetched_at=datetime(2026, 10, 13, 19, 5, 12))


def test_headers_are_lowercased_and_cookies_dropped() -> None:
    headers = {"Content-Type": HTML, "Set-Cookie": "session=secret", "ETag": '"x"'}
    assert snap(headers=headers).headers == {"content-type": HTML, "etag": '"x"'}


# --- decoding ---------------------------------------------------------------------------------

CZECH = "Příliš žluťoučký kůň úpěl ďábelské ódy"


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (CZECH.encode("windows-1250"), "text/html; charset=windows-1250"),
        (CZECH.encode("windows-1250"), "text/html; charset=\"WINDOWS-1250\""),
        (b'<meta charset="windows-1250">' + CZECH.encode("windows-1250"), "text/html"),
        (b"<meta http-equiv=Content-Type content='text/html; charset=iso-8859-2'>"
         + CZECH.encode("iso-8859-2"), "text/html"),
        (codecs.BOM_UTF8 + CZECH.encode("utf-8"), "text/html; charset=windows-1250"),
        (CZECH.encode("utf-8"), "text/html"),
        (CZECH.encode("utf-8"), "text/html; charset=no-such-charset"),
    ],
)  # fmt: skip
def test_decode_body(body: bytes, content_type: str) -> None:
    assert CZECH in decode_body(body, content_type)


def test_header_charset_beats_meta() -> None:
    body = b'<meta charset="utf-8">' + CZECH.encode("windows-1250")
    assert CZECH in decode_body(body, "text/html; charset=windows-1250")


def test_undeclared_non_utf8_falls_back_to_windows_1252() -> None:
    assert decode_body("café".encode("windows-1252")) == "café"


def test_xhtml_with_xml_declaration_parses() -> None:
    body = b'<?xml version="1.0" encoding="utf-8"?><html><body><h1>Hi</h1></body></html>'
    assert snap(body=body).document().text == "Hi"


# --- with the demo site -------------------------------------------------------------------------


def test_stored_page_extracts_like_the_live_one(store: FilesystemSnapshotStore) -> None:
    spec = load_spec(Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml")
    url = "http://localhost:8000/notices/TN-2026-0001"
    response = TestClient(create_app(layout=1)).get("/notices/TN-2026-0001")
    live = snap(kind="detail", url=url, headers=dict(response.headers), body=response.content)

    stored = store.get(store.put(live))

    extractor = Extractor(spec.fields)
    assert extractor.extract(stored.document()).record == extractor.extract(live.document()).record
    assert extractor.extract(stored.document()).record["reference"] == "TN-2026-0001"


def test_run_ids_sort_by_time() -> None:
    first = new_run_id(datetime(2026, 10, 13, 9, 0, tzinfo=UTC))
    second = new_run_id(datetime(2026, 10, 13, 10, 0, tzinfo=UTC))
    assert re.fullmatch(r"20261013T090000Z-[0-9a-f]{4}", first)
    assert first < second
    snap(run_id=first)  # valid as a run id
