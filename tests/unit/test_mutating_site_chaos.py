import pytest
from fastapi.testclient import TestClient

from examples.mutating_site.app import create_app
from examples.mutating_site.chaos import RATE_LIMIT, RATE_WINDOW, Chaos, Mode, parse_modes


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def client_for(chaos: str, failure_rate: float = 0.2) -> TestClient:
    return TestClient(create_app(layout=1, chaos=chaos, failure_rate=failure_rate))


def statuses(client: TestClient, n: int, path: str = "/notices") -> list[int]:
    return [client.get(path).status_code for _ in range(n)]


# --- parsing -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("off", set()),
        ("", set()),
        ("flaky", {Mode.FLAKY}),
        (" Flaky , ratelimit ", {Mode.FLAKY, Mode.RATELIMIT}),
        ("blocked,off", {Mode.BLOCKED}),
    ],
)
def test_parse_modes(raw: str, expected: set[Mode]) -> None:
    assert parse_modes(raw) == expected


def test_parse_modes_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="captcha"):
        parse_modes("flaky,captcha")


def test_failure_rate_must_be_a_probability() -> None:
    with pytest.raises(ValueError):
        Chaos(failure_rate=1.5)


# --- modes -----------------------------------------------------------------------


def test_off_by_default() -> None:
    assert statuses(client_for("off"), 30) == [200] * 30


def test_flaky_fails_some_requests() -> None:
    results = statuses(client_for("flaky", failure_rate=0.3), 100)
    assert set(results) == {200, 503}
    assert 10 < results.count(503) < 50


def test_flaky_extremes() -> None:
    assert statuses(client_for("flaky", failure_rate=1.0), 10) == [503] * 10
    assert statuses(client_for("flaky", failure_rate=0.0), 10) == [200] * 10


def test_flaky_is_reproducible() -> None:
    assert statuses(client_for("flaky"), 50) == statuses(client_for("flaky"), 50)


def test_ratelimit_returns_429_with_retry_after() -> None:
    app = create_app(layout=1, chaos="ratelimit")
    clock = FakeClock()
    app.state.chaos.clock = clock
    client = TestClient(app)

    assert statuses(client, RATE_LIMIT) == [200] * RATE_LIMIT
    clock.now = 3.0
    response = client.get("/notices")
    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert retry_after == RATE_WINDOW - 3

    clock.now += retry_after
    assert client.get("/notices").status_code == 200


def test_blocked_returns_403_page() -> None:
    client = client_for("blocked")
    for path in ("/notices", "/notices?page=2", "/notices/TN-2026-0001"):
        response = client.get(path)
        assert response.status_code == 403
        assert "Access denied" in response.text


def test_blocked_wins_over_other_modes() -> None:
    assert statuses(client_for("flaky,ratelimit,blocked", failure_rate=1.0), 15) == [403] * 15


def test_chaos_spares_admin_docs_and_robots() -> None:
    client = client_for("blocked")
    for path in ("/robots.txt", "/admin/layout", "/admin/chaos", "/docs"):
        assert client.get(path).status_code == 200, path


# --- configuration -----------------------------------------------------------------


def test_admin_switches_chaos_at_runtime() -> None:
    client = client_for("off")
    assert client.get("/admin/chaos").json() == {"modes": [], "failure_rate": 0.2}

    response = client.post("/admin/chaos/flaky", params={"failure_rate": 1.0})
    assert response.json() == {"modes": ["flaky"], "failure_rate": 1.0}
    assert client.get("/notices").status_code == 503

    client.post("/admin/chaos/off")
    assert client.get("/notices").status_code == 200


def test_admin_keeps_failure_rate_when_omitted() -> None:
    client = client_for("off", failure_rate=0.0)
    assert client.post("/admin/chaos/flaky").json()["failure_rate"] == 0.0


def test_admin_rejects_bad_chaos() -> None:
    client = client_for("off")
    assert client.post("/admin/chaos/nonsense").status_code == 422
    assert client.post("/admin/chaos/flaky", params={"failure_rate": 2}).status_code == 422
    assert client.get("/admin/chaos").json()["modes"] == []


def test_chaos_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS", "flaky")
    monkeypatch.setenv("CHAOS_FAILURE_RATE", "1")
    client = TestClient(create_app(layout=1))
    assert client.get("/notices").status_code == 503


def test_invalid_env_chaos_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAOS", "meltdown")
    with pytest.raises(ValueError):
        create_app(layout=1)
