"""The studio's HTTP surface, driven without a server, a port, or a simulator.

`create_app` takes its roots as arguments precisely so this file can point it at a temp directory.
The tests that matter here are the ones about *refusal* -- what the studio will not serve -- because
it runs subprocesses that write into the repository and it has no authentication.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip(
    "fastapi", reason="needs_web: FastAPI is not installed (uv sync --group web)"
)
from fastapi.testclient import TestClient  # noqa: E402

from scenariobank.web.api import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path):
    banks = tmp_path / "banks"
    banks.mkdir()
    application = create_app(banks_root=banks, state_dir=tmp_path / ".studio")
    with TestClient(application) as started:
        yield started


def test_the_page_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "scenariobank" in response.text


def test_doctor_reports_the_simulator_without_building_one(client):
    report = client.get("/api/doctor").json()
    assert report["report_version"] == 1
    # `probe=False` is the whole point: probing costs a reset, and a reset means a MetaDrive engine
    # in the web process, which `api.py` exists to avoid. The probed fields stay unset.
    assert report["observation_space"] is None
    assert report["action_space"] is None


def test_the_app_holds_the_roots_it_was_given(tmp_path):
    application = create_app(banks_root=tmp_path / "b", state_dir=tmp_path / "s")
    assert application.state.banks_root == tmp_path / "b"
    assert application.state.state_dir == tmp_path / "s"
