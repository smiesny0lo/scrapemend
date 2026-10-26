from pathlib import Path

from specmend.heal.proposals import ProposalStore
from specmend.heal.verify import Check, CheckStatus, FieldCheck, VerificationReport
from specmend.spec.patch import SpecPatch
from specmend.state import connect

PATCH = SpecPatch.model_validate(
    {"container": {"css": "tr.row"}, "rationale": "new rows", "confidence": 0.7}
)


def report(shadow: CheckStatus) -> VerificationReport:
    """Anchors that pass, and a shadow run that passed, failed or didn't run."""
    problems = ["title reproduces 3/10 values of the last good run"] if shadow == "failed" else []
    return VerificationReport(
        checks=[
            Check(name="anchors", status="passed", reason="10 of 10 anchor pages compared"),
            Check(name="shadow run", status=shadow, reason="the whole source", problems=problems),
        ],
        threshold=0.9,
        anchors=10,
        anchors_checked=10,
        fields={"title": FieldCheck(matched=10, compared=10)},
    )


def test_status_follows_the_verdict(tmp_path: Path) -> None:
    store = ProposalStore(connect(tmp_path / "state.db"))
    good = store.add("demo", 1, "run1", "anthropic", PATCH, report("passed"))
    bad = store.add("demo", 1, "run1", "anthropic", PATCH, report("failed"))
    unsure = store.add("demo", 1, "run1", "anthropic", PATCH, report("not run"))
    assert (good.status, bad.status, unsure.status) == ("accepted", "rejected", "unverified")
    assert good.patch == PATCH and good.report.score == 1.0
    assert bad.report.problems == ["title reproduces 3/10 values of the last good run"]
    assert [p.id for p in store.for_source("demo")] == [unsure.id, bad.id, good.id]
    assert store.for_source("other") == []


def test_verifying_again_can_change_the_status(tmp_path: Path) -> None:
    store = ProposalStore(connect(tmp_path / "state.db"))
    proposal = store.add("demo", 1, "run1", "anthropic", PATCH, report("not run"))
    store.reverified(proposal.id, report("passed"))
    again = store.get(proposal.id)
    assert again is not None and (again.status, again.report.verdict) == ("accepted", "passed")


def test_a_report_without_checks_is_not_a_pass() -> None:
    # Fail closed, e.g. for a report stored by an older version, which has no checks.
    assert VerificationReport().verdict == "not run"
    assert not VerificationReport().accepted


def test_mark_promoted(tmp_path: Path) -> None:
    store = ProposalStore(connect(tmp_path / "state.db"))
    proposal = store.add("demo", 1, "run1", "patch", PATCH, report("passed"))
    store.mark_promoted(proposal.id, 2)
    promoted = store.get(proposal.id)
    assert promoted is not None
    assert (promoted.status, promoted.promoted_version) == ("promoted", 2)
    assert store.get(999) is None
