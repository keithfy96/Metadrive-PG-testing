"""The studio's HTTP surface, driven without a server, a port, or a simulator.

`create_app` takes its roots as arguments precisely so this file can point it at a temp directory.
The tests that matter here are the ones about *refusal* -- what the studio will not serve -- because
it runs subprocesses that write into the repository and it has no authentication.
"""

from __future__ import annotations

import json

import pytest

from scenariobank.categories import CATEGORIES, ExitRule
from scenariobank.docs import reference

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


def test_categories_serves_all_seven_with_what_selects_them(client):
    rows = client.get("/api/categories").json()
    assert [row["name"] for row in rows] == list(CATEGORIES)
    for row in rows:
        # A name alone is not enough to pick a category: the road, the rule and the budget are
        # what the studio's forms and tables have to show beside it.
        assert row["block_seq"] and row["exit_rule"] and row["max_steps"] > 0
        assert row["summary"] and not row["summary"].endswith(".")


def test_the_reference_is_the_same_data_the_checked_in_page_is_built_from(client):
    served = client.get("/api/commands").json()
    # Not "looks similar to": the same dict. The page and the file listing different flags is the
    # exact failure `docs.py` exists to prevent, and a second renderer is where it would come back.
    assert served == json.loads(json.dumps(reference()))


def test_every_registered_command_reaches_the_page(client):
    import typer

    from scenariobank.cli import app

    served = client.get("/api/commands").json()
    shown = {entry["name"] for group in served["groups"] for entry in group["commands"]}
    assert shown == set(typer.main.get_command(app).commands)


def test_every_command_carries_its_own_examples(client):
    served = client.get("/api/commands").json()
    for group in served["groups"]:
        for entry in group["commands"]:
            assert entry["examples"], f"{entry['name']} would render as a bare table"
            assert all(line.startswith("uv run") or line.startswith("    ")
                       for line in entry["examples"])


def test_a_flags_own_row_carries_the_values_it_accepts(client):
    served = client.get("/api/commands").json()
    rows = [
        row
        for group in served["groups"]
        for entry in group["commands"]
        for row in entry["options"]
    ]
    rules = [row for row in rows if row["flag"].startswith("`--rule")]
    assert rules
    for row in rules:
        assert all(f"`{rule.value}`" in row["meaning"] for rule in ExitRule)
    categories = [row for row in rows if row["flag"].startswith("`--category")]
    assert categories
    for row in categories:
        assert all(f"`{name}`" in row["meaning"] for name in CATEGORIES)
