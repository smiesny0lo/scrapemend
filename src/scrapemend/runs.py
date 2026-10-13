"""Run metadata. For now only run ids; the SQLite runs table lands with detection."""

import secrets
from datetime import UTC, datetime


def new_run_id(now: datetime | None = None) -> str:
    """A run id like 20261013T190512Z-3f9a: sortable by time, unique enough per source."""
    now = now or datetime.now(UTC)
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
