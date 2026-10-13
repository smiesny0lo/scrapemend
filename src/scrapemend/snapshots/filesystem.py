"""Snapshots as plain files: one directory per run, the body next to its metadata.

    snapshots/tenders-demo/20261013T190512Z-3f9a/00001-listing.html
                                                 00001-listing.meta.json
                                                 00002-detail.html
                                                 ...

Bodies keep their raw bytes and get an extension matching their type, so a stored
page can be opened in a browser. A snapshot exists once its .meta.json does: the
body is written first, then the metadata atomically, so a crash never leaves a
snapshot that looks complete but isn't.
"""

import json
import os
import re
from pathlib import Path

from scrapemend.snapshots.base import Snapshot

_EXTENSIONS = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}
_META = ".meta.json"
_SEQ = re.compile(r"^(\d+)-")


class FilesystemSnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, snapshot: Snapshot) -> str:
        run_dir = self.root / snapshot.source_id / snapshot.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        extension = _EXTENSIONS.get(snapshot.content_type.split(";")[0].strip().lower(), ".bin")

        # Claim the next sequence number by creating the body file exclusively.
        seq = self._next_seq(run_dir)
        while True:
            stem = f"{seq:05d}-{snapshot.kind}"
            try:
                with open(run_dir / f"{stem}{extension}", "xb") as body_file:
                    body_file.write(snapshot.body)
                break
            except FileExistsError:
                seq += 1

        meta = snapshot.model_dump(mode="json", exclude={"body"})
        meta["body_file"] = f"{stem}{extension}"
        tmp = run_dir / f"{stem}{_META}.tmp"
        tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(tmp, run_dir / f"{stem}{_META}")
        return f"{snapshot.source_id}/{snapshot.run_id}/{seq:05d}"

    def get(self, snapshot_id: str) -> Snapshot:
        source_id, run_id, seq = _split_id(snapshot_id)
        metas = list((self.root / source_id / run_id).glob(f"{seq}-*{_META}"))
        if not metas:
            raise KeyError(snapshot_id)
        meta = json.loads(metas[0].read_text(encoding="utf-8"))
        meta["body"] = (metas[0].parent / meta.pop("body_file")).read_bytes()
        return Snapshot.model_validate(meta)

    def runs(self, source_id: str) -> list[str]:
        _check_segment(source_id)
        source_dir = self.root / source_id
        if not source_dir.is_dir():
            return []
        # Run ids start with a UTC timestamp, so name order is time order.
        return sorted(d.name for d in source_dir.iterdir() if any(d.glob(f"*{_META}")))

    def snapshots(self, source_id: str, run_id: str) -> list[str]:
        _check_segment(source_id)
        _check_segment(run_id)
        run_dir = self.root / source_id / run_id
        seqs = sorted(m.name[:5] for m in run_dir.glob(f"*{_META}"))
        return [f"{source_id}/{run_id}/{seq}" for seq in seqs]

    def _next_seq(self, run_dir: Path) -> int:
        seqs = [int(m.group(1)) for p in run_dir.iterdir() if (m := _SEQ.match(p.name))]
        return max(seqs, default=0) + 1


def _split_id(snapshot_id: str) -> tuple[str, str, str]:
    parts = snapshot_id.split("/")
    if len(parts) != 3 or not re.fullmatch(r"\d{5}", parts[2]):
        raise KeyError(snapshot_id)
    _check_segment(parts[0])
    _check_segment(parts[1])
    return parts[0], parts[1], parts[2]


def _check_segment(segment: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", segment):
        raise KeyError(segment)
