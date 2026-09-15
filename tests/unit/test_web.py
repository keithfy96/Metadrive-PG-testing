"""The studio's HTTP surface, driven without a server, a port, or a simulator.

`create_app` takes its roots as arguments precisely so this file can point it at a temp directory.
The tests that matter here are the ones about *refusal* -- what the studio will not serve -- because
it runs subprocesses that write into the repository and it has no authentication.
"""

from __future__ import annotations

import json
import time

import pytest

from scenariobank.categories import BLOCKS, CATEGORIES, ExitRule
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
        banks_root=banks,
        state_dir=tmp_path / ".studio",
        workdir=tmp_path,
        results_root=tmp_path / "share" / "results",
    )
    with TestClient(application) as started:
        started.workdir = tmp_path
        started.results_root = tmp_path / "share" / "results"
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
    # The laptop's own layout when no share is named: what the agent's `Roots` resolves to with
    # `SCENARIOBANK_SHARE` unset, so `agent --once` and the studio read the same directory.
    assert application.state.results_root == tmp_path / "w" / "out" / "results"
    given = create_app(
        banks_root=tmp_path / "b", state_dir=tmp_path / "s", results_root=tmp_path / "r"
    )
    assert given.state.results_root == tmp_path / "r"


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
    # Read by `POST /api/jobs` rather than by the page, now that there is no Run tab: the studio
    # is the one command a job may not be, and the refusal names it either way.
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
            # `options` is what the reference tab reads; `params` is what `invoke.py` validates a
            # submitted job against. Two views of one Typer command -- if they can disagree, the
            # page would document a flag the studio then refuses to send.
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
        # A name this build ships is described by it; anything else is a category composed by
        # hand, which the manifest describes for itself -- the same two sources `add` has.
        known = CATEGORIES.get(category)
        entries[category] = CategoryEntry(
            description=known.description if known else f"Composed by hand: {category}.",
            block_seq=known.block_seq if known else "CCX",
            exit_rule=str(known.exit_rule) if known else "sharpest",
            max_steps=known.max_steps if known else 1300,
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


def _real_bank(root, name, *, created="2026-09-01T00:00:00Z", step_hz=100.0,
               tracks=None, origin=None, scenarios=1, route=False, features=None):
    """An imported bank on disk. Written directly rather than through `import_workspace`.

    `test_import.py` owns the copy; what this file needs is the *file* an import leaves behind,
    so the listing can be asked what it makes of one without a converter checkout in sight.
    """
    from scenariobank.bank import (
        SCHEMA_VERSION,
        Manifest,
        Provenance,
        RealWorldEntry,
        RealWorldRow,
        write_manifest,
    )
    from scenariobank.handedness import DRIVE_SIDE_LEFT
    from scenariobank.workspace import Route

    drive = Route(
        source="generated", name="route-1", start_lane="a", end_lane="b", lane_count=18,
        lane_changes=3, junction_movements=14, distance_m=395.1, speed_kph=50.0,
        slowest_kph=10.42, duration_s=3782 / step_hz, driving_duration_s=3782 / step_hz,
        waiting_s=0.0, stop_count=0,
    ) if route else None

    directory = root / name
    directory.mkdir(parents=True)
    tracks = {"VEHICLE": 120, "PEDESTRIAN": 31} if tracks is None else tracks
    rows = [
        RealWorldRow(
            scenario_id=f"{name}_{index:04d}",
            scenario_index=index,
            file=f"sd_{name}_{index}.pkl",
            stored_id=f"osm-scenario_v1_{name}-{index}",
            max_steps=3782,
            route_length_m=395.1,
            duration_s=3782 / step_hz,
            tracks=dict(tracks),
            lights={"TRAFFIC_LIGHT": 8},
            map_features=features,
            map_feature_types={"LANE_SURFACE_STREET": features} if features else {},
            route=drive,
            thumbnail=None,
        )
        for index in range(scenarios)
    ]
    write_manifest(
        directory,
        Manifest(
            schema_version=SCHEMA_VERSION,
            bank_id=name,
            created_utc=created,
            source="osm-scenario",
            metadrive={
                "edition": None,
                "dist_version": None,
                "commit": None,
                "asset_version": None,
            },
            base_config={},
            drive_side=DRIVE_SIDE_LEFT,
            categories={
                name: RealWorldEntry(
                    description=f"{name}: recorded at {step_hz:g} Hz",
                    dataset_dir="dataset",
                    step_hz=step_hz,
                    origin={"latitude": 3.1478, "longitude": 101.6953}
                    if origin is None
                    else origin,
                    attribution="OpenStreetMap contributors",
                    provenance=Provenance(
                        generator_version="v1",
                        generation_fingerprint="a" * 64,
                        source_osm_sha256="b" * 64,
                        reviewed_lane_model_sha256="c" * 64,
                        stage_5_status="passed",
                    ),
                    tool_versions={"osmnx": "2.0.7"},
                    artifacts={},
                    copied=[],
                    signals=None,
                    max_steps=3782,
                    scenarios=rows,
                )
            },
        ),
    )
    return directory


def test_the_listing_says_which_kind_of_bank_each_one_is(client):
    # The discriminator the page groups on, straight off the manifest. One endpoint and one
    # listing: the field already exists in the file, so nothing here asks a second time.
    _bank(client.workdir / "banks", "curvy", created="2026-09-01T00:00:00Z")
    _real_bank(client.workdir / "banks", "junction-1", created="2026-09-02T00:00:00Z")

    listed = {bank["name"]: bank for bank in client.get("/api/banks").json()}
    assert listed["curvy"]["source"] == "pg"
    # The recording's own word for what it is, not "real" -- a converter that starts writing a
    # second kind says so in the file rather than being flattened here.
    assert listed["junction-1"]["source"] == "osm-scenario"


def test_a_real_world_row_carries_the_place_the_rate_and_the_actors(client):
    # The three things a recording has and a road built from a seed has no equivalent for. They
    # are on the row because they are what tells two imports apart in a picker.
    _real_bank(client.workdir / "banks", "junction-1")

    [row] = client.get("/api/banks").json()
    assert row["origin"] == {"latitude": 3.1478, "longitude": 101.6953}
    assert row["step_hz"] == 100.0
    # Summed across the bank, and the ego is one of the vehicles -- the converter counts it as a
    # track like every other, and subtracting it here would disagree with `workspace`.
    assert row["tracks"] == {"PEDESTRIAN": 31, "VEHICLE": 120}


def test_the_actors_are_summed_across_the_recordings_in_the_bank(client):
    _real_bank(client.workdir / "banks", "junction-1", scenarios=3,
               tracks={"VEHICLE": 2, "CYCLIST": 1})

    [row] = client.get("/api/banks").json()
    assert row["scenarios"] == 3
    assert row["tracks"] == {"CYCLIST": 3, "VEHICLE": 6}


def test_a_procedural_row_is_exactly_what_it_was_before_the_split(client):
    # A PG bank has no origin, its rate is a property of the run rather than of the file, and its
    # traffic is a level a run sets rather than a count. So the fields are absent, not null: the
    # page needs no second listing and the old one did not change shape.
    _bank(client.workdir / "banks", "curvy")

    [row] = client.get("/api/banks").json()
    assert set(row) == {"name", "bank_id", "created_utc", "source", "categories", "scenarios"}


def test_a_bank_that_will_not_read_is_filed_under_neither_kind(client):
    # Nothing was read, so which kind it is is unknown. A row that claimed `pg` would put a
    # broken import under the procedural heading, which is worse than saying nothing.
    broken = client.workdir / "banks" / "broken"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json")

    [row] = client.get("/api/banks").json()
    assert "source" not in row
    assert row["error"]


def test_both_kinds_are_listed_together_newest_first(client):
    # One list, sorted the one way, and the page groups it. Two endpoints would let the two kinds
    # sort by different rules and would need the page to merge them back.
    _bank(client.workdir / "banks", "curvy", created="2026-09-01T00:00:00Z")
    _real_bank(client.workdir / "banks", "junction-1", created="2026-09-03T00:00:00Z")
    _bank(client.workdir / "banks", "roundy", created="2026-09-02T00:00:00Z")

    listed = client.get("/api/banks").json()
    assert [bank["name"] for bank in listed] == ["junction-1", "roundy", "curvy"]


def test_an_imported_bank_is_served_and_says_it_is_one(client):
    # The manifest endpoint is unshaped, so this is really asking that a 1.3 real-world manifest
    # round-trips through the studio at all -- the listing above reads the same file.
    from scenariobank.bank import read_manifest

    directory = _real_bank(client.workdir / "banks", "junction-1")
    served = client.get("/api/banks/junction-1").json()

    assert served == json.loads(read_manifest(directory).model_dump_json())
    assert served["source"] == "osm-scenario"
    assert served["categories"]["junction-1"]["step_hz"] == 100.0


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


def test_the_studio_says_where_a_candidate_seed_may_be_drawn(client):
    served = client.get("/api/studio").json()
    # Relative, because that is the form `inspect --out` wants and the form `invoke.py` resolves
    # against the working directory. The page may not invent this: where the scratch lives is the
    # studio's decision, and `/api/looks` serves the same directory back.
    assert served["looks"] == ".studio/looks"


def test_a_look_that_has_not_been_drawn_is_a_404_not_a_blank(client):
    answer = client.get("/api/looks/curve-seed22.png")
    assert answer.status_code == 404
    assert "curve-seed22" in answer.json()["detail"]


def test_a_look_is_served_once_a_job_has_drawn_it(client):
    looks = client.workdir / ".studio" / "looks"
    looks.mkdir(parents=True)
    (looks / "curve-seed22.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    answer = client.get("/api/looks/curve-seed22.png")
    assert answer.status_code == 200
    assert answer.headers["content-type"] == "image/png"


@pytest.mark.parametrize("name", [".ssh", "-lead", "a b"])
def test_a_look_name_that_is_not_a_name_never_becomes_a_path(client, name):
    # The same guard a thumbnail gets, for the same reason: the shape of the name is checked
    # before it is joined to anything, so a traversal is not filtered out -- it cannot be spelled.
    assert client.get(f"/api/looks/{name}.png").status_code == 400


# ------------------------------------------------------------- the road builder (step 10)


def test_blocks_serves_the_fifteen_pieces_a_road_is_spelled_from(client):
    rows = client.get("/api/blocks").json()
    assert [row["id"] for row in rows] == [block.id for block in BLOCKS]
    for row in rows:
        # A letter alone is not a palette: the button says what the block is, and the tooltip
        # says what MetaDrive calls it.
        assert row["label"] and row["cls"]


def test_the_road_builder_sends_only_flags_inspect_takes(client):
    """The form is `inspect --block-seq --rule --seed --json --out`, and every one of those is a
    flag the CLI declares -- read back through the same reference the page builds from."""
    from scenariobank.web.invoke import catalog

    commands, _ = catalog()
    params = commands["inspect"]["params"]
    assert {"--block-seq", "--rule", "--seed", "--json", "--out"} <= set(params)
    assert params["--json"]["type"] == "boolean"
    # The rule dropdown is the closed set `categories.py` owns, not a text box.
    assert params["--rule"]["choices"] == [rule.value for rule in ExitRule]
    assert params["--out"]["type"] == "path"


def test_adding_a_drawn_road_sends_only_flags_add_takes(client):
    """`Add to bank` is the same `add` the edit panel runs, with a road instead of a name."""
    from scenariobank.web.invoke import catalog

    commands, _ = catalog()
    params = commands["add"]["params"]
    assert {"--bank", "--block-seq", "--rule", "--seed"} <= set(params)
    assert params["--rule"]["choices"] == [rule.value for rule in ExitRule]
    assert params["--bank"]["type"] == "path"
    # `--category` stopped being required when a road became the other way in. Both optional is
    # what lets one form send either.
    assert not params["--category"]["required"]


def test_a_road_may_only_be_added_to_a_bank_inside_the_studio(client):
    # `--bank` is a path like every other, so the containment check is the same one. Named here
    # because this is the flag the road builder fills in from the studio's own answer.
    answer = client.post("/api/jobs", json={
        "command": "add",
        "options": {"--bank": "/tmp/elsewhere", "--block-seq": "CCX", "--rule": "sharpest",
                    "--seed": "0"},
    })
    assert answer.status_code == 400
    assert "--bank" in answer.json()["detail"]


def test_a_bank_holding_a_composed_category_is_served_and_reviewed(client):
    """A composed category is an ordinary entry, so nothing downstream needs to know about it."""
    _bank(client.workdir / "banks", "b", categories=("curve", "CCX_sharpest"))

    manifest = client.get("/api/banks/b").json()
    report = client.get("/api/banks/b/review").json()

    assert set(manifest["categories"]) == {"curve", "CCX_sharpest"}
    assert manifest["categories"]["CCX_sharpest"]["scenarios"][0]["scenario_id"] == (
        "CCX_sharpest_0000"
    )
    assert {one["category"] for one in report["categories"]} == {"curve", "CCX_sharpest"}
    # And its pictures are servable, which is the reason the name is spelled the way it is.
    assert client.get("/api/banks/b/thumbs/CCX_sharpest_0000.png").status_code == 200


def test_a_road_drawn_by_hand_may_only_land_in_the_studio_scratch(client):
    # The same containment every job gets, spelled for the one flag the road builder fills in
    # from the studio's own answer: an `--out` outside the working directory is refused before
    # a subprocess exists.
    answer = client.post("/api/jobs", json={
        "command": "inspect",
        "options": {"--block-seq": "CCX", "--rule": "left", "--seed": "0", "--json": True,
                    "--out": "/tmp/road-CCX-left-seed0.png"},
    })
    assert answer.status_code == 400
    assert "--out" in answer.json()["detail"]


# ------------------------------------------------- the one write that is not a job (step 8b)


def test_a_step_budget_is_saved_without_starting_a_job(client):
    """`max_steps` is declared, not measured, so this endpoint writes the manifest itself.

    Every other edit builds a road, and a road means an engine, which means a subprocess. Making
    the page start a Python interpreter to change one integer would be a second of waiting for
    nothing -- so this is the one write here with no job behind it, and the test says so by
    checking that no job was ever created.
    """
    _bank(client.workdir / "banks", "b")

    answer = client.post("/api/banks/b/scenarios/curve_0001/budget", json={"max_steps": 500})

    assert answer.status_code == 200
    assert answer.json()["max_steps"] == 500
    assert client.get("/api/jobs").json() == []

    rows = client.get("/api/banks/b").json()["categories"]["curve"]["scenarios"]
    assert rows[1]["max_steps"] == 500
    assert rows[0]["max_steps"] is None, "the others still follow the category"

    # And the review reports the cap that applies to each row, from the one place an override is
    # resolved -- so the page reads a number rather than working one out.
    budget = client.get("/api/banks/b/review").json()["categories"][0]["budget"]
    assert budget["caps"]["curve_0001"] == 500
    assert budget["caps"]["curve_0000"] == budget["max_steps"] == CATEGORIES["curve"].max_steps


def test_clearing_a_step_budget_puts_the_scenario_back_on_its_categorys(client):
    _bank(client.workdir / "banks", "b")
    client.post("/api/banks/b/scenarios/curve_0001/budget", json={"max_steps": 500})

    answer = client.post("/api/banks/b/scenarios/curve_0001/budget", json={"max_steps": None})

    assert answer.status_code == 200
    assert answer.json()["max_steps"] is None
    budget = client.get("/api/banks/b/review").json()["categories"][0]["budget"]
    assert budget["caps"]["curve_0001"] == CATEGORIES["curve"].max_steps


def test_a_budget_for_a_scenario_the_bank_does_not_hold_is_a_404(client):
    # The bank is fine and the request is well formed; the id names nothing in it. The same split
    # `/compare` makes, and the reason `ScenarioNotFound` is its own type.
    _bank(client.workdir / "banks", "b")
    answer = client.post("/api/banks/b/scenarios/curve_0009/budget", json={"max_steps": 500})
    assert answer.status_code == 404
    assert "curve_0009" in answer.json()["detail"]


def test_a_budget_that_would_end_the_episode_before_it_began_is_refused(client):
    _bank(client.workdir / "banks", "b")
    answer = client.post("/api/banks/b/scenarios/curve_0001/budget", json={"max_steps": 0})
    assert answer.status_code == 400
    assert "before it began" in answer.json()["detail"]


def test_option_levels_are_pinned_without_starting_a_job(client):
    """The second write on this page with no job behind it, for the same reason as the first.

    A level is applied when a run happens, not when the bank was built, so nothing is measured
    and no road is driven -- the roads, the routes and the pictures are the same afterwards. The
    test says so by checking that no job was ever created and that `base_config` did not move.
    """
    _bank(client.workdir / "banks", "b")
    before = client.get("/api/banks/b").json()
    assert before["options"] == {axis: "none" for axis in
                                 ["traffic", "cones", "barriers", "pedestrians", "cyclists",
                                  "lights"]}

    answer = client.post("/api/banks/b/options", json={"traffic": "medium"})

    assert answer.status_code == 200
    assert answer.json()["traffic"] == "medium"
    assert client.get("/api/jobs").json() == []

    after = client.get("/api/banks/b").json()
    assert after["options"]["traffic"] == "medium"
    assert after["categories"] == before["categories"], "no scenario moved"
    assert after["base_config"] == before["base_config"], "generation truth is not run intent"

    # The sentence comes from `review.py`, so the page does not write a second description of it.
    line = client.get("/api/banks/b/review").json()["options_line"]
    assert line == "runs at traffic=medium, everything else none"


def test_a_level_the_cli_would_refuse_is_a_400_naming_the_axis(client):
    _bank(client.workdir / "banks", "b")
    answer = client.post("/api/banks/b/options", json={"traffic": "enormous"})
    assert answer.status_code == 400
    assert "traffic" in answer.json()["detail"]
    # An axis that is not one is caught before it reaches the manifest: `extra="forbid"` on the
    # request model, so the page cannot invent a seventh dropdown.
    assert client.post("/api/banks/b/options", json={"weather": "high"}).status_code == 422
    assert client.post("/api/banks/b/options", json={}).status_code == 400


@pytest.mark.parametrize("bank", ["..", "../etc", ".hidden"])
def test_a_bank_name_that_is_not_a_name_never_becomes_a_path_on_the_options_write(client, bank):
    # Same guard as every other bank-addressed route: the shape of the name is checked before it
    # touches the filesystem, rather than checking where the path landed afterwards.
    answer = client.post(f"/api/banks/{bank}/options", json={"traffic": "low"})
    assert answer.status_code in (400, 404)


@pytest.mark.parametrize("scenario", [".ssh", "-lead", "a b"])
def test_a_scenario_id_that_is_not_a_name_never_becomes_a_path(client, scenario):
    _bank(client.workdir / "banks", "b")
    answer = client.post(f"/api/banks/b/scenarios/{scenario}/budget", json={"max_steps": 500})
    assert answer.status_code == 400
    assert "not a scenario id" in answer.json()["detail"]


def test_a_budget_is_not_written_underneath_a_running_job(client, monkeypatch):
    """A `replace` holds the manifest in memory and writes it back when it finishes.

    An edit slipped in beside it would be overwritten without a word, and a lost write is the one
    failure the page could not show you. So the slot is checked first and the answer says what to
    wait for.
    """
    _bank(client.workdir / "banks", "b")
    monkeypatch.setattr(client.app.state.jobs, "running", lambda: {"command": "replace"})

    answer = client.post("/api/banks/b/scenarios/curve_0001/budget", json={"max_steps": 500})

    assert answer.status_code == 409
    assert "replace" in answer.json()["detail"]
    rows = client.get("/api/banks/b").json()["categories"]["curve"]["scenarios"]
    assert rows[1]["max_steps"] is None


def test_the_commands_that_edit_one_item_are_all_runnable_from_the_page(client):
    # Every edit but the budget is a subprocess of this same CLI, so the page can only offer them
    # if `/api/runnable` does. A flag typo'd here would be a 400 from `invoke.py`, not a page that
    # quietly does nothing.
    runnable = client.get("/api/runnable").json()["commands"]
    assert {"replace", "add", "remove", "budget"} <= set(runnable)


def test_the_destinations_reference_is_served_as_the_file_on_disk(client):
    """Text, not a parsed document. `destinations.render` decides the layout of that file, and a
    second opinion about it here would be the place the page and the command disagree."""
    doc = client.workdir / "docs" / "reference" / "destinations.md"
    doc.parent.mkdir(parents=True)
    doc.write_text("# Destinations\n\n| category | seed 0 |\n|---|---|\n| `curve` | `2C0_1_` |\n")

    body = client.get("/api/reference/destinations").json()
    assert body["written"] is True
    assert body["path"] == "docs/reference/destinations.md"
    assert body["text"] == doc.read_text()
    assert body["at"] > 0


def test_a_studio_with_no_reference_measured_is_told_so_rather_than_refused(client):
    """Absent is not an error: a studio started outside the repo gets the button that fixes it."""
    body = client.get("/api/reference/destinations").json()
    assert body == {
        "path": "docs/reference/destinations.md", "written": False, "text": None, "at": None
    }


def test_the_palette_is_served_the_rule_and_not_only_a_sentence_about_it(client):
    """The road builder offers `SyyPS` for `SPS`, so it needs `after_any` and `insert` as data.

    Held to `categories.BLOCKS` rather than restated in JavaScript: the page's note and the CLI's
    refusal are then one declaration read twice, and cannot come to differ.
    """
    rows = {row["id"]: row["needs"] for row in client.get("/api/blocks").json()}
    assert {block_id for block_id, needs in rows.items() if needs} == {"f", "F", "P"}
    assert rows["P"]["after_any"] == "y" and rows["P"]["insert"] == "yy"
    assert all(rows[block.id] is None for block in BLOCKS if block.id not in {"f", "F", "P"})


def test_the_review_endpoint_answers_for_an_imported_bank(client):
    """Step 3 left this 422-ing and the page swallowed it with `.catch(() => null)`, so an
    imported bank drew no chips and said nothing about why. Step 5 gave a recording a review of
    its own, and this is the endpoint the page reads it from."""
    _real_bank(client.workdir / "banks", "junction-1", route=True, features=974)

    answer = client.get("/api/banks/junction-1/review")
    assert answer.status_code == 200
    report = answer.json()
    assert report["source"] == "osm-scenario"
    # Absent, not zero and not the total: there is no second recording to be distinct from.
    assert report["distinct"] is None
    assert report["total"] == 1

    one = report["categories"][0]
    assert one["drive"]["route_length_max_m"] == 395.1
    assert one["actors"]["tracks"] == {"PEDESTRIAN": 31, "VEHICLE": 120}
    assert one["map_size"]["features"] == 974
    assert one["replay"]["at_hz"] == 100.0
    assert "duplicates" not in one


def test_comparing_two_recordings_is_still_refused_by_the_endpoint(client):
    """A bank holds one recording, so there is no second drive in it to compare the first
    against. `/compare` keeps the guard `/review` gave up."""
    _real_bank(client.workdir / "banks", "junction-1")

    answer = client.get(
        "/api/banks/junction-1/compare", params={"left": "junction-1_0000",
                                                 "right": "junction-1_0000"}
    )
    assert answer.status_code == 422
    assert "no second drive" in answer.json()["detail"]


def test_a_procedural_review_is_unchanged_but_for_the_discriminator(client):
    _bank(client.workdir / "banks", "curvy")

    report = client.get("/api/banks/curvy/review").json()
    assert report["source"] == "pg"
    # A number rather than `None`: the count is the point of a procedural review, and the two
    # `curvy` rows collapse to one drive.
    assert (report["total"], report["distinct"]) == (2, 1)
    assert "duplicates" in report["categories"][0]


# -- Phase 7 Step 6: the results the rigs delivered ------------------------------------------


def _delivered(root, name, *, scenarios=("t_junction_0000",), stopped=False):
    """A directory the way `RunSession.deliver` leaves one, built from the runner's own models."""
    from tests.unit.test_results_store import deliver, record, scored

    return deliver(root, name, record(name, [scored(s) for s in scenarios], stopped=stopped))


def test_results_are_indexed_on_request_and_listed_newest_first(client):
    # Nothing delivered yet: an empty list and the two places named, not a 404.
    empty = client.get("/api/results").json()
    assert empty["jobs"] == [] and empty["ingested"]["total"] == 0
    assert empty["results_root"] == str(client.results_root)
    assert empty["index"] == str(client.workdir / ".studio" / "results.sqlite"), (
        "the index is under the studio's state directory, on local disk, never the share"
    )

    _delivered(client.results_root, "j1", scenarios=("t_junction_0000", "t_junction_0001"))
    _delivered(client.results_root, "j2")
    listed = client.get("/api/results").json()
    assert listed["ingested"]["added"] == 2
    assert sorted(job["name"] for job in listed["jobs"]) == ["j1", "j2"]
    assert {job["status"] for job in listed["jobs"]} == {"complete"}
    # The same request again reads the tree, finds nothing new, and the list is the same.
    again = client.get("/api/results").json()
    assert again["ingested"] == {"added": 0, "skipped": 2, "invalid": 0, "total": 2}
    assert again["jobs"] == listed["jobs"]
    # A rebuild reads every directory again and lands on the same list.
    rebuilt = client.get("/api/results", params={"rebuild": "true"}).json()
    assert rebuilt["ingested"] == {"added": 2, "skipped": 0, "invalid": 0, "total": 2}
    assert rebuilt["jobs"] == listed["jobs"]


def test_one_result_is_its_job_and_its_rows(client):
    _delivered(client.results_root, "j1", scenarios=("t_junction_0000", "t_junction_0001"))
    body = client.get("/api/results/j1").json()
    assert body["job"]["name"] == "j1" and body["job"]["n"] == 2
    assert [row["scenario_id"] for row in body["rows"]] == ["t_junction_0000", "t_junction_0001"]
    assert body["rows"][0]["success"] is True
    assert body["rows"][0]["collisions"] == {"human": 0, "vehicle": 0}


def test_an_attempt_is_served_with_no_rows_and_a_missing_result_is_a_404(client):
    from tests.unit.test_results_store import attempt

    attempt(client.results_root, "j1.attempt1")
    body = client.get("/api/results/j1.attempt1").json()
    assert body["job"]["kind"] == "attempt" and body["job"]["job_id"] == "j1"
    assert body["rows"] == []

    missing = client.get("/api/results/j1")
    assert missing.status_code == 404
    assert "no result named 'j1'" in missing.json()["detail"]
    # A name that is not a name never reaches the index.
    assert client.get("/api/results/..%2Fetc").status_code in (400, 404)
    assert client.get("/api/results/.hidden").status_code == 400


def test_the_rigs_are_the_status_files_and_nothing_else(client):
    from scenariobank.web.results import STATUS_DIR

    status = client.results_root / STATUS_DIR
    status.mkdir(parents=True)
    (status / "sim-gpu0.json").write_text(
        json.dumps({"consumer": "sim:gpu0", "state": "idle", "job_id": None})
    )
    assert client.get("/api/rigs").json() == [
        {"file": "sim-gpu0.json", "consumer": "sim:gpu0", "state": "idle", "job_id": None}
    ]
    # And the status directory is never a job.
    assert client.get("/api/results").json()["jobs"] == []
