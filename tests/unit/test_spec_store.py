from pathlib import Path
from typing import Any

import pytest

from scrapemend.spec import SourceSpec, load_spec
from scrapemend.spec.store import SpecConflict, SpecStore
from scrapemend.state import connect

DEMO = load_spec(Path(__file__).parents[2] / "examples" / "sources" / "demo.yaml")


def variant(version: int, **changes: Any) -> SourceSpec:
    data = DEMO.model_dump(mode="json") | {"version": version}
    data["fetch"]["rate"]["rps"] = changes.get("rps", data["fetch"]["rate"]["rps"])
    return SourceSpec.model_validate(data)


@pytest.fixture
def store(tmp_path: Path) -> SpecStore:
    return SpecStore(connect(tmp_path / "state.db"))


def test_first_registration_becomes_active(store: SpecStore) -> None:
    active = store.register(DEMO)
    assert (active.version, active.origin, active.spec) == (1, "file", DEMO)
    assert store.next_version("tenders-demo") == 2


def test_registering_again_is_harmless(store: SpecStore) -> None:
    store.register(DEMO)
    assert store.register(DEMO).version == 1
    assert len(store.history("tenders-demo")) == 1


def test_changed_file_without_new_version_is_refused(store: SpecStore) -> None:
    store.register(DEMO)
    with pytest.raises(SpecConflict, match="next free: 2"):
        store.register(variant(1, rps=2))


def test_newer_file_version_takes_over(store: SpecStore) -> None:
    store.register(DEMO)
    assert store.register(variant(2, rps=2)).version == 2


def test_an_older_file_does_not_undo_a_promoted_repair(store: SpecStore) -> None:
    store.register(DEMO)
    store.add(variant(2, rps=3), "repair", "proposal 1")
    store.activate("tenders-demo", 2)
    active = store.register(DEMO)  # the file on disk still says v1
    assert (active.version, active.origin) == (2, "repair")


def test_history_and_lookup(store: SpecStore) -> None:
    store.register(DEMO)
    store.add(variant(2, rps=3), "repair", "proposal 1")
    assert [(v.version, v.origin) for v in store.history("tenders-demo")] == [
        (1, "file"),
        (2, "repair"),
    ]
    found = store.get("tenders-demo", 2)
    assert found is not None and found.note == "proposal 1"
    assert store.get("tenders-demo", 9) is None
    assert store.active("unknown") is None


def test_activate_unknown_version(store: SpecStore) -> None:
    store.register(DEMO)
    with pytest.raises(KeyError):
        store.activate("tenders-demo", 5)


def test_quarantine_until_a_version_is_activated(store: SpecStore) -> None:
    # Replaces test_quarantine_until_released_or_a_version_is_activated: release() was
    # never called (promote, rollback and a new spec file all activate a version).
    store.register(DEMO)
    store.quarantine("tenders-demo", "no repair passed")
    state = store.state("tenders-demo")
    assert state is not None and state.quarantine_reason == "no repair passed"
    store.activate("tenders-demo", 1)  # like a rollback to v1
    state = store.state("tenders-demo")
    assert state is not None and state.quarantine_reason is None
    store.quarantine("tenders-demo", "again")
    store.register(variant(2, rps=2))  # a new spec file version
    state = store.state("tenders-demo")
    assert state is not None and (state.quarantine_reason, state.active_version) == (None, 2)
