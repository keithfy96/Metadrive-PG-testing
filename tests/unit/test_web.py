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
    # subprocess -- which is the whole claim the studio makes about how it executes.
    for name in CATEGORIES:
        assert name in job["log"]


def test_a_bad_job_id_is_a_404_not_a_file_read(client):
    assert client.get("/api/jobs/../../../etc/passwd").status_code in (404, 400)
    assert client.get("/api/jobs/nope").status_code == 404


def test_every_category_has_an_example_picture_checked_in():
    """The gallery's assets, guarded from a machine with no simulator.

    Not a web test at all in spirit: the realistic failure is adding a category and forgetting to
    redraw, and that has to fail on every machine rather than only where MetaDrive is installed --
    the same reason `test_docs.py` guards the generated reference.
    """
    from scenariobank.cli import EXAMPLES_DIR

    missing = [name for name in CATEGORIES if not (EXAMPLES_DIR / f"{name}.png").is_file()]
    assert not missing, (
        f"no example picture for {', '.join(missing)}. Run: uv run scenariobank examples"
    )


def test_the_gallery_serves_a_picture_for_every_category(tmp_path):
    """Served through the app, from a temp directory that stands in for the repo."""
    from scenariobank.cli import EXAMPLES_DIR

    examples = tmp_path / EXAMPLES_DIR
    examples.mkdir(parents=True)
    for name in CATEGORIES:
        (examples / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    application = create_app(
        banks_root=tmp_path / "banks", state_dir=tmp_path / ".studio", workdir=tmp_path
    )
    with TestClient(application) as served:
        for name in CATEGORIES:
            response = served.get(f"/api/examples/{name}.png")
            assert response.status_code == 200, name
            assert response.headers["content-type"] == "image/png"


def test_a_picture_that_has_not_been_drawn_yet_names_the_command(client):
    # The studio started somewhere without the pictures. A blank card teaches nothing; this is
    # the one screen where the fix is a single command.
    response = client.get("/api/examples/curve.png")
    assert response.status_code == 404
    assert "scenariobank examples" in response.json()["detail"]


@pytest.mark.parametrize("segment", ["banana", "..", "../../../etc/passwd", "curve-seed0"])
def test_the_gallery_only_answers_to_a_category_name(client, segment):
    # The name is checked against `CATEGORIES` before anything becomes a path, so a traversal is
    # not filtered out -- it never reaches the filesystem at all.
    response = client.get(f"/api/examples/{segment}.png")
    assert response.status_code == 404
    assert "escaped" not in response.text


def test_the_studio_says_where_a_new_bank_would_go(client):
    """The page may not invent a directory for `generate --out`.

    Banks live under `--banks-root`, which is a flag, and a job may only write inside the
    directory the studio was started in. A page that assumed `banks/` would quietly write
    somewhere else the moment either changed.
    """
    served = client.get("/api/studio").json()
    assert served["workdir"] == str(client.workdir)
    assert served["banks_root"] == "banks"
    assert served["writable"] is True


def test_a_banks_root_outside_the_workdir_is_listed_but_not_generated_into(tmp_path):
    # Legal to point at: the bank list is a read. Not legal to write into, because no job may
    # write outside the checkout -- so the page is told, rather than finding out at the click.
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    workdir = tmp_path / "repo"
    workdir.mkdir()
    application = create_app(
        banks_root=outside, state_dir=workdir / ".studio", workdir=workdir
    )
    with TestClient(application) as served:
        studio = served.get("/api/studio").json()
        assert studio["writable"] is False
        assert studio["banks_root"] == str(outside)


def test_the_reference_carries_the_seeds_a_category_is_built_at(client):
    """`--seeds` defaults to `None` in the parameter table; the real list has to reach the page.

    The studio says how many scenarios a selection asks for *before* the first one is built, and
    counting them means knowing the seeds. Carried from `categories.SEEDS` rather than restated
    in the page, which is the one place it could go stale unnoticed.
    """
    from scenariobank.categories import SEEDS

    served = client.get("/api/commands").json()
    assert served["default_seeds"] == list(SEEDS)
    # The same list the prose promises, so the sentence and the number cannot disagree.
    seeds = ",".join(str(seed) for seed in SEEDS)
    rows = [
        row
        for group in served["groups"]
        for entry in group["commands"]
        for row in entry["options"]
        if row["flag"].startswith("`--seeds")
    ]
    assert rows and all(f"`{seeds}`" in row["meaning"] for row in rows)


def test_the_selection_becomes_a_generate_the_cli_would_accept(client):
    """What the Build tab submits, checked against the CLI's own parameters.

    Not a mock of the page: this is the same POST body it sends, refused or accepted by the same
    `invoke.build_argv` every other command goes through.
    """
    from scenariobank.web.invoke import build_argv

    argv = build_argv(
        "generate",
        {
            "--out": "banks/bank-2026-09-02-1431",
            "--bank-id": "bank-2026-09-02-1431",
            "--category": ["curve", "roundabout"],
        },
        {},
        workdir=client.workdir,
    )
    assert argv[-6:] == [
        "--bank-id", "bank-2026-09-02-1431",
        "--category", "curve",
        "--category", "roundabout",
    ]
    # `--out` is a path, so it arrives resolved and inside the workdir -- the containment check
    # is not something the page opts into.
    assert str(client.workdir / "banks" / "bank-2026-09-02-1431") in argv


def test_a_bank_name_cannot_climb_out_of_the_workdir(client):
    response = client.post(
        "/api/jobs",
        json={
            "command": "generate",
            "options": {"--out": "banks/../../escaped", "--bank-id": "escaped"},
        },
    )
    assert response.status_code == 400
    assert str(client.workdir) in response.json()["detail"]
    assert not (client.workdir.parent / "escaped").exists()


def test_how_many_of_each_type_becomes_a_seed_list_the_cli_parses(client):
    """The Build tab asks for a count; the CLI takes a seed list. This is the join between them.

    The page sends the list the way you would type it -- one comma-separated `--seeds` value --
    so the count it offers cannot mean something the CLI would read differently.
    """
    from scenariobank.cli import _parse_seed_options
    from scenariobank.web.invoke import build_argv

    argv = build_argv(
        "generate",
        {
            "--out": "banks/b",
            "--bank-id": "b",
            "--category": ["curve"],
            "--seeds": "0,1,2,3,4,5,6",
        },
        {},
        workdir=client.workdir,
    )
    at = argv.index("--seeds")
    shared, overrides = _parse_seed_options([argv[at + 1]])
    assert shared == (0, 1, 2, 3, 4, 5, 6)
    assert not overrides


def test_a_count_of_one_is_a_bank_of_one_scenario_per_type(client):
    # The smallest thing worth generating, and the case a hardcoded five could not express.
    from scenariobank.cli import _parse_seed_options

    shared, _ = _parse_seed_options(["0"])
    assert shared == (0,)


# ---------------------------------------------------------------- the dataset (Step 6)
#
# These build banks on disk by hand rather than by running `generate`, so they say what the studio
# serves without needing a simulator. `write_manifest` is the same function generation ends with,
# so the file under test is the real shape.


def _bank(root, name, *, categories=("curve",), seeds=(0, 1), thumbnails=True,
          bank_id=None, created="2026-09-01T00:00:00Z"):
    """A bank on disk: a manifest, and a PNG per scenario. No simulator involved."""
    from scenariobank.bank import (
        THUMBNAIL_DIR,
        CategoryEntry,
        Manifest,
        ScenarioRow,
        scenario_id,
        write_manifest,
    )
    from scenariobank.handedness import DRIVE_SIDE_LEFT

    directory = root / name
    directory.mkdir(parents=True)
    (directory / THUMBNAIL_DIR).mkdir()
    entries = {}
    for category in categories:
        rows = []
        for index, seed in enumerate(seeds):
            row_id = scenario_id(category, index)
            if thumbnails:
                # A real PNG header, so a browser fetching it would get an image and not a 200
                # over nonsense. The endpoint serves bytes; this is about the content type being
                # honest, which is the only thing the file's contents can be wrong about here.
                (directory / THUMBNAIL_DIR / f"{row_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            rows.append(
                ScenarioRow(
                    scenario_id=row_id,
                    seed=seed,
                    destination="1T0_1_",
                    spawn_lane_index=1,
                    route_length_m=340.1,
                    net_rotation_deg=88.0,
                    turn_pairs="L",
                    thumbnail=f"{THUMBNAIL_DIR}/{row_id}.png" if thumbnails else None,
                )
            )
        entries[category] = CategoryEntry(
            description=CATEGORIES[category].description,
            block_seq=CATEGORIES[category].block_seq,
            exit_rule=str(CATEGORIES[category].exit_rule),
            max_steps=CATEGORIES[category].max_steps,
            scenarios=rows,
        )
    write_manifest(
        directory,
        Manifest(
            schema_version="1.0",
            bank_id=bank_id or name,
            created_utc=created,
            metadrive={
                "edition": None,
                "dist_version": None,
                "commit": None,
                "asset_version": None,
            },
            base_config={},
            drive_side=DRIVE_SIDE_LEFT,
            categories=entries,
        ),
    )
    return directory


def test_no_banks_is_an_empty_list_rather_than_an_error(client):
    # The state a fresh checkout is in. The page shows "no banks yet" from this, so it has to be
    # an ordinary answer and not something to catch.
    assert client.get("/api/banks").json() == []


def test_the_listing_summarises_each_bank_newest_first(client):
    # Sorted by what the manifest says it was built at, not by directory name -- the bank you just
    # generated is the one you want at the top, and its name is whatever you called it.
    _bank(client.workdir / "banks", "zed", created="2026-09-01T00:00:00Z")
    _bank(
        client.workdir / "banks",
        "abel",
        categories=("curve", "roundabout"),
        created="2026-09-02T00:00:00Z",
    )

    banks = client.get("/api/banks").json()
    assert [bank["name"] for bank in banks] == ["abel", "zed"]
    assert banks[0]["scenarios"] == 4
    assert banks[0]["categories"] == ["curve", "roundabout"]
    # A summary, not the manifest: the picker needs counts, and a listing carrying every scenario
    # row would grow with the disk.
    assert "scenarios" not in banks[0]["categories"]


def test_a_directory_without_a_manifest_is_not_a_bank(client):
    # Generation writes the manifest last, so this is precisely the interrupted-run case: the
    # directory exists and holds nothing a reader could trust.
    (client.workdir / "banks" / "half-built" / "thumbs").mkdir(parents=True)
    assert client.get("/api/banks").json() == []
    assert client.get("/api/banks/half-built").status_code == 404


def test_an_unreadable_manifest_is_listed_with_its_reason(client):
    """A bank the studio cannot parse stays in the list, carrying the error.

    Silently dropping it is the one failure a person cannot debug from the page -- and a schema
    bump is exactly when it happens.
    """
    broken = client.workdir / "banks" / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json")

    listed = client.get("/api/banks").json()
    assert [bank["name"] for bank in listed] == ["broken"]
    assert "not valid JSON" in listed[0]["error"]

    # And opening it says why, rather than claiming there is no such bank.
    response = client.get("/api/banks/broken")
    assert response.status_code == 422
    assert "not valid JSON" in response.json()["detail"]


def test_a_bank_is_served_as_the_manifest_on_disk(client):
    from scenariobank.bank import read_manifest

    directory = _bank(client.workdir / "banks", "b", categories=("curve",), seeds=(0, 4))
    served = client.get("/api/banks/b").json()

    # Unshaped: the manifest was written to explain itself, so a studio that reformatted it here
    # would be inventing a second description of a bank for the page to drift away from.
    assert served == json.loads(read_manifest(directory).model_dump_json())
    assert [row["seed"] for row in served["categories"]["curve"]["scenarios"]] == [0, 4]


def test_a_thumbnail_is_served_as_an_image(client):
    _bank(client.workdir / "banks", "b", categories=("curve",), seeds=(0, 1))
    response = client.get("/api/banks/b/thumbs/curve_0000.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")


def test_a_bank_built_without_thumbnails_still_serves_its_rows(client):
    # `generate --no-thumbnails` is a supported way to build a bank, so a missing picture is an
    # ordinary state: the row is there, the file is not, and the 404 says so.
    _bank(client.workdir / "banks", "b", thumbnails=False)
    served = client.get("/api/banks/b").json()
    assert served["categories"]["curve"]["scenarios"][0]["thumbnail"] is None

    response = client.get("/api/banks/b/thumbs/curve_0000.png")
    assert response.status_code == 404
    assert "no thumbnail" in response.json()["detail"]


@pytest.mark.parametrize(
    "segment",
    [
        "%2e%2e%2f%2e%2e%2fetc",
        "..",
        ".hidden",
        "a/b",
    ],
)
def test_a_bank_name_that_is_not_a_name_never_becomes_a_path(client, segment):
    """Refused, and refused before it touches the filesystem.

    Two layers, and which one catches a given segment is not the point: an HTTP client normalises
    `..` and decodes `%2f` on the way out, so the router sees a path that matches no route; what
    survives as a single segment meets the name check. The same rule
    `/api/examples/{category}.png` follows -- a name that cannot hold a separator and cannot begin
    with a dot is not a traversal that gets filtered out, it is one that cannot be spelled.
    """
    _bank(client.workdir / "banks", "b")
    secret = client.workdir / "secret.txt"
    secret.write_text("not yours")

    for path in (f"/api/banks/{segment}", f"/api/banks/{segment}/thumbs/x.png"):
        response = client.get(path)
        assert response.status_code in (400, 404), path
        assert "not yours" not in response.text, path


def test_a_thumbnail_name_that_is_not_a_name_is_refused(client):
    # A leading dot is the shape that matters: `..` is the traversal, and the class that forbids
    # one forbids the other. The bank exists, so this is the name check refusing and not a 404.
    _bank(client.workdir / "banks", "b")
    response = client.get("/api/banks/b/thumbs/.ssh.png")
    assert response.status_code == 400
    assert "not a scenario name" in response.json()["detail"]


def test_the_studios_banks_root_is_the_directory_the_listing_reads(client):
    """`/api/studio` says where banks are and `/api/banks` lists them. One place, or neither
    means anything."""
    _bank(client.workdir / "banks", "b")
    root = client.get("/api/studio").json()["banks_root"]
    assert (client.workdir / root / "b" / "manifest.json").is_file()
    assert [bank["name"] for bank in client.get("/api/banks").json()] == ["b"]


# ---------------------------------------------------------------- the review (Step 6b)


def test_the_review_reports_what_is_actually_in_a_bank(client):
    """The counting is `review.py`'s and tested there; this is that it reaches the page.

    Five rows over two seeds means every scenario is built twice, so the bank has half the
    scenarios it has rows -- which is the whole point of serving this.
    """
    _bank(client.workdir / "banks", "b", categories=("curve", "roundabout"), seeds=(0, 1))
    report = client.get("/api/banks/b/review").json()

    assert report["bank_id"] == "b"
    assert report["total"] == 4
    assert [one["category"] for one in report["categories"]] == ["curve", "roundabout"]
    for one in report["categories"]:
        # `_bank` writes every row with the same destination, length and rotation, differing only
        # in the id -- so each category is one drive wearing two names.
        assert one["duplicates"]["distinct"] == 1
        assert len(one["duplicates"]["identical_groups"][0]) == 2
        assert any("distinct" in line for line in one["warnings"])


def test_the_review_is_separate_from_the_manifest_it_is_computed_from(client):
    """Step 6 serves the manifest unshaped. A computed report folded into it would be exactly the
    second description of a bank that rule exists to prevent."""
    _bank(client.workdir / "banks", "b")
    served = client.get("/api/banks/b").json()
    assert "duplicates" not in served
    assert "warnings" not in json.dumps(served)


def test_the_review_refuses_the_same_names_the_rest_of_the_bank_api_does(client):
    _bank(client.workdir / "banks", "b")
    assert client.get("/api/banks/.hidden/review").status_code == 400
    assert client.get("/api/banks/nope/review").status_code == 404


def test_an_unreadable_bank_cannot_be_reviewed_and_says_why(client):
    broken = client.workdir / "banks" / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json")

    response = client.get("/api/banks/broken/review")
    assert response.status_code == 422
    assert "not valid JSON" in response.json()["detail"]


# ------------------------------------------------------- two scenarios compared (Step 7)


def test_two_scenarios_are_compared_with_the_measure_the_review_uses(client):
    """The measure is `review.py`'s and tested there; this is that the pair reaches the page.

    The fixture builds every row alike, so these two are the same drive at two seeds -- which is
    the answer a person clicking two cards most needs to be given plainly.
    """
    _bank(client.workdir / "banks", "b")
    answer = client.get("/api/banks/b/compare?left=curve_0000&right=curve_0001").json()
    assert answer["verdict"] == "identical"
    assert answer["gap"] == 0.0
    assert answer["category"] == "curve"
    assert answer["summary"]
    fields = {one["field"]: one for one in answer["fields"]}
    assert fields["seed"]["left"] == "0" and fields["seed"]["right"] == "1"
    assert fields["seed"]["same"] is False
    assert fields["route length"]["same"] is True


def test_comparing_across_categories_is_answered_rather_than_refused(client):
    """A gap is only defined inside a category, and a click on two cards is still a fair question.
    The endpoint says there is no number and why, at 200 -- an error here would be about the
    request, and the request is fine."""
    _bank(client.workdir / "banks", "b", categories=("curve", "t_junction"))
    response = client.get(
        "/api/banks/b/compare?left=curve_0000&right=t_junction_0000")
    assert response.status_code == 200
    answer = response.json()
    assert answer["verdict"] == "incomparable"
    assert answer["gap"] is None
    assert answer["category"] is None


def test_a_scenario_this_bank_does_not_hold_is_a_404_naming_it(client):
    _bank(client.workdir / "banks", "b")
    response = client.get("/api/banks/b/compare?left=curve_0000&right=curve_0099")
    assert response.status_code == 404
    assert "curve_0099" in response.json()["detail"]


def test_a_scenario_compared_with_itself_is_a_400(client):
    _bank(client.workdir / "banks", "b")
    response = client.get("/api/banks/b/compare?left=curve_0000&right=curve_0000")
    assert response.status_code == 400
    assert "itself" in response.json()["detail"]


def test_a_comparison_needs_both_ends(client):
    """`left` and `right` are required, so a half-formed request is a 422 from the signature
    rather than a comparison of something with nothing."""
    _bank(client.workdir / "banks", "b")
    assert client.get("/api/banks/b/compare?left=curve_0000").status_code == 422


def test_comparing_refuses_the_same_bank_names_the_rest_of_the_bank_api_does(client):
    _bank(client.workdir / "banks", "b")
    both = "left=curve_0000&right=curve_0001"
    assert client.get(f"/api/banks/.hidden/compare?{both}").status_code == 400
    assert client.get(f"/api/banks/nope/compare?{both}").status_code == 404
