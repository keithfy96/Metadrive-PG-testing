"""The studio's HTTP surface, driven without a server, a port, or a simulator.

`create_app` takes its roots as arguments precisely so this file can point it at a temp directory.
The tests that matter here are the ones about *refusal* -- what the studio will not serve -- because
it runs subprocesses that write into the repository and it has no authentication.
"""

from __future__ import annotations

import json
import time

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
    # `workdir` is both where jobs run and the boundary they may write inside, so pointing it at
    # the temp directory is what lets the containment tests below be about somewhere real.
    application = create_app(
        banks_root=banks, state_dir=tmp_path / ".studio", workdir=tmp_path
    )
    with TestClient(application) as started:
        started.workdir = tmp_path
        yield started


def settle(client, job, timeout=30.0):
    """Poll a job the way the page does, and return it once it stops running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job['job_id']}").json()
        if job["state"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job['job_id']} never finished")


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
    application = create_app(
        banks_root=tmp_path / "b", state_dir=tmp_path / "s", workdir=tmp_path / "w"
    )
    assert application.state.banks_root == tmp_path / "b"
    assert application.state.state_dir == tmp_path / "s"
    assert application.state.workdir == tmp_path / "w"


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


def test_the_studio_will_not_run_itself_and_says_why(client):
    served = client.get("/api/runnable").json()
    assert "studio" not in served["commands"]
    assert served["not_runnable"]["studio"]
    # Everything else the CLI offers is runnable: the page's list of commands is the CLI's list
    # minus a named exception, never a hand-picked subset that quietly falls behind.
    import typer

    from scenariobank.cli import app

    registered = set(typer.main.get_command(app).commands)
    assert set(served["commands"]) == registered - set(served["not_runnable"])


def test_the_form_and_the_table_describe_the_same_flags(client):
    served = client.get("/api/commands").json()
    for group in served["groups"]:
        for entry in group["commands"]:
            # `options` is what the reference tab reads; `params` is what the run form is built
            # from. Two views of one Typer command -- if they can disagree, one is lying.
            documented = {row["flag"].strip("`").split("/")[0].split(" ")[0]
                          for row in entry["options"]}
            offered = {param["flag"] for param in entry["params"]}
            assert documented == offered, entry["name"]
            for param in entry["params"]:
                assert param["flag"].startswith("--"), entry["name"]


def test_the_flags_with_a_closed_set_of_values_offer_it(client):
    served = client.get("/api/commands").json()
    params = [
        param
        for group in served["groups"]
        for entry in group["commands"]
        for param in entry["params"]
    ]
    for flag, expected in (
        ("--category", list(CATEGORIES)),
        ("--rule", [rule.value for rule in ExitRule]),
    ):
        matching = [param for param in params if param["flag"] == flag]
        assert matching, flag
        # A dropdown rather than a text box, and its contents come from `categories.py`: a
        # category cannot be mistyped in the page, and adding one adds it here.
        assert all(param["choices"] == expected for param in matching)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"command": "nope"}, "no such command"),
        ({"command": "studio"}, "cannot be run from the page"),
        ({"command": "inspect", "options": {"--nope": 1}}, "does not take --nope"),
        ({"command": "inspect", "options": {"--category": "banana"}}, "does not accept"),
        ({"command": "inspect", "options": {"--seed": "soon"}}, "whole number"),
        ({"command": "generate", "options": {"--bank-id": "b"}}, "needs --out"),
    ],
)
def test_a_submission_the_cli_would_reject_is_refused_readably(client, body, expected):
    response = client.post("/api/jobs", json=body)
    # 400 with a sentence about one flag, not a Typer traceback recovered from a log afterwards.
    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_a_path_option_may_not_escape_the_directory_the_studio_was_started_in(client):
    response = client.post(
        "/api/jobs", json={"command": "inspect", "options": {"--out": "../escaped.png"}}
    )
    assert response.status_code == 400
    assert str(client.workdir) in response.json()["detail"]
    assert not (client.workdir.parent / "escaped.png").exists()


def test_a_job_runs_the_cli_and_the_page_can_read_its_output(client):
    started = client.post("/api/jobs", json={"command": "categories"})
    assert started.status_code == 201
    job = settle(client, started.json())
    assert job["state"] == "finished"
    assert job["exit_code"] == 0
    # The same seven names `/api/categories` serves, this time produced by the CLI itself in a
    # subprocess -- which is the whole claim the studio makes about being a second front door.
    for name in CATEGORIES:
        assert name in job["log"]


def test_a_bad_job_id_is_a_404_not_a_file_read(client):
    assert client.get("/api/jobs/../../../etc/passwd").status_code in (404, 400)
    assert client.get("/api/jobs/nope").status_code == 404
