from pathlib import Path

from scrapemend.heal.proposals import ProposalStore
from scrapemend.heal.verify import FieldCheck, VerificationReport
from scrapemend.spec.patch import SpecPatch
from scrapemend.state import connect

PATCH = SpecPatch.model_validate(
    {"container": {"css": "tr.row"}, "rationale": "new rows", "confidence": 0.7}
)


def report(accepted: bool) -> VerificationReport:
    return VerificationReport(
        accepted=accepted,
        threshold=0.9,
        anchors=10,
        anchors_checked=10,
        fields={"title": FieldCheck(matched=10 if accepted else 3, compared=10)},
        problems=[] if accepted else ["title reproduces 3/10 anchor values"],
    )


def test_status_follows_the_verdict(tmp_path: Path) -> None:
    store = ProposalStore(connect(tmp_path / "state.db"))
    good = store.add("demo", 1, "run1", "anthropic", PATCH, report(True))
    bad = store.add("demo", 1, "run1", "anthropic", PATCH, report(False))
    assert (good.status, bad.status) == ("accepted", "rejected")
    assert good.patch == PATCH and good.report.score == 1.0
    assert [p.id for p in store.for_source("demo")] == [bad.id, good.id]
    assert store.for_source("other") == []


def test_mark_promoted(tmp_path: Path) -> None:
    store = ProposalStore(connect(tmp_path / "state.db"))
    proposal = store.add("demo", 1, "run1", "patch", PATCH, report(True))
    store.mark_promoted(proposal.id, 2)
    promoted = store.get(proposal.id)
    assert promoted is not None
    assert (promoted.status, promoted.promoted_version) == ("promoted", 2)
    assert store.get(999) is None
