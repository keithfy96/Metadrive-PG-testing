"""What a rig accepts, and what it refuses before it touches a card.

Phase 7 Step 3, the half that runs while the GPU is still free. Every refusal here is
**permanent** -- the same job on the other rig would be refused identically -- so each test is
also a statement about what Step 5 must dead-letter rather than retry.

No docker, no simulator, no share: these build a bank on `tmp_path` and read it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scenariobank.agent.jobs import (
    CONTAINER_BANK,
    JobRefused,
    Roots,
    checkout,
    parse_job,
    read_job,
    resolve,
)
from scenariobank.bank import CategoryEntry, Manifest, ScenarioRow, write_manifest
from scenariobank.handedness import DRIVE_SIDE_LEFT
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, JobOptions


def row(scenario_id: str, seed: int) -> ScenarioRow:
    return ScenarioRow(
        scenario_id=scenario_id,
        seed=seed,
        destination=f"dest-{seed}",
        spawn_lane_index=0,
        route_length_m=100.0,
        net_rotation_deg=0.0,
        turn_pairs="",
        thumbnail=None,
    )


def write_bank(directory: Path, bank_id: str = "t-junction") -> Path:
    """One category, two rows, written so `read_manifest` reads it."""
    directory.mkdir(parents=True, exist_ok=True)
    write_manifest(
        directory,
        Manifest(
            schema_version="1.4",
            bank_id=bank_id,
            created_utc="2026-09-15T00:00:00Z",
            metadrive={
                "edition": None, "dist_version": None, "commit": None, "asset_version": None,
            },
            base_config={"traffic_density": 0.0},
            drive_side=DRIVE_SIDE_LEFT,
            categories={
                "t_junction": CategoryEntry(
                    description="a T",
                    block_seq="T",
                    exit_rule="left",
                    max_steps=30,
                    scenarios=[row("t_junction_0000", 0), row("t_junction_0001", 1)],
                ),
            },
        ),
    )
    return directory


@pytest.fixture
def roots(tmp_path) -> Roots:
    """A rig's four directories, all under `tmp_path`, with one bank in place."""
    write_bank(tmp_path / "banks" / "t-junction")
    return Roots(
        banks=tmp_path / "banks",
        models=tmp_path / "models",
        results=tmp_path / "results",
        out=tmp_path / "out",
        repo=Path("/host/checkout"),
    )


def job(**fields) -> Job:
    """A runnable job, with anything named here changed."""
    base = {
        "schema_version": JOB_SCHEMA_VERSION,
        "job_id": "j7",
        "attempt": 1,
        "bank": JobBank(id="t-junction", path="/wherever/the/submitter/kept/it"),
        "scenarios": ["t_junction_0000"],
        "policy": "scenariobank.policies:ExpertPolicy",
    }
    base.update(fields)
    return Job(**base)


# --- the roots ----------------------------------------------------------------------------------


def test_with_no_environment_at_all_the_roots_are_this_checkout(monkeypatch):
    # The laptop's case, and it is the reason `agent --once job.json` runs in a fresh clone with
    # nothing mounted: Open question 8 is about the share's protocol, and this step does not
    # wait on it.
    for name in ("SCENARIOBANK_SHARE", "SCENARIOBANK_BANKS", "SCENARIOBANK_MODELS",
                 "SCENARIOBANK_RESULTS", "SCENARIOBANK_OUT", "SCENARIOBANK_REPO"):
        monkeypatch.delenv(name, raising=False)
    here = checkout()
    found = Roots.from_environment({})
    assert found.banks == here / "banks"
    # Beside the repo, which is `sim-run.sh`'s own MODELS_DIR default and must stay equal to it.
    assert found.models == here.parent / "models"
    assert found.out == here / "out"
    assert found.repo == here


def test_the_share_gives_three_roots_and_one_override_wins():
    env = {"SCENARIOBANK_SHARE": "/mnt/nas", "SCENARIOBANK_MODELS": "/srv/models"}
    found = Roots.from_environment(env)
    assert found.banks == Path("/mnt/nas/banks")
    assert found.results == Path("/mnt/nas/results")
    assert found.models == Path("/srv/models")


def test_the_repo_root_is_the_hosts_path_and_not_this_processes():
    # The agent container reads the checkout at /work and the daemon has never heard of /work.
    found = Roots.from_environment({"SCENARIOBANK_REPO": "/home/metadrive/dev/Metadrive-PG"})
    assert found.repo == Path("/home/metadrive/dev/Metadrive-PG")


def test_delivered_and_staging_differ_by_the_suffix_that_says_incomplete(roots):
    assert roots.staging("j7").name == "j7.partial"
    assert roots.delivered("j7").name == "j7"
    assert roots.staging("j7").parent == roots.delivered("j7").parent


# --- what is refused, with the card still free ---------------------------------------------------


def test_a_job_with_no_id_is_refused_because_a_rig_cannot_mint_one(roots):
    with pytest.raises(JobRefused, match="no job_id"):
        resolve(job(job_id=None), roots)


def test_a_job_id_that_is_not_a_directory_name_is_refused(roots):
    with pytest.raises(JobRefused, match="directory name"):
        resolve(job(job_id="../../etc"), roots)


def test_a_job_with_no_bank_id_is_refused_and_the_submitted_path_is_never_read(roots):
    # A job carries names and the rig carries paths. `bank.path` as submitted is the
    # submitter's own machine's, and running against whatever is mounted at it would be a score
    # against a bank nobody named.
    with pytest.raises(JobRefused, match="no bank.id"):
        resolve(job(bank=JobBank(id=None, path=str(roots.banks / "t-junction"))), roots)


def test_a_bank_id_that_escapes_the_banks_root_is_refused(roots):
    with pytest.raises(JobRefused, match="outside the banks root"):
        resolve(job(bank=JobBank(id="../../../etc", path="x")), roots)


def test_a_bank_that_is_not_on_this_share_is_refused_by_name(roots):
    with pytest.raises(JobRefused, match="no bank 'nowhere'"):
        resolve(job(bank=JobBank(id="nowhere", path="x")), roots)


def test_a_manifest_that_disagrees_about_which_bank_it_is_is_refused(roots, tmp_path):
    # The share has the wrong bank under that name. Catching it here rather than in the runner
    # is the difference between a refusal and a card held for the length of a docker pull.
    write_bank(roots.banks / "swapped", bank_id="something-else")
    with pytest.raises(JobRefused, match="holds 'something-else'"):
        resolve(job(bank=JobBank(id="swapped", path="x")), roots)


def test_an_unreadable_manifest_is_refused_rather_than_raising_its_own_error(roots):
    (roots.banks / "broken").mkdir()
    (roots.banks / "broken" / "manifest.json").write_text("{not json")
    with pytest.raises(JobRefused, match="not a readable bank"):
        resolve(job(bank=JobBank(id="broken", path="x")), roots)


def test_a_scenario_the_bank_does_not_hold_is_refused_by_name(roots):
    with pytest.raises(JobRefused, match="no scenario named nope"):
        resolve(job(scenarios=["t_junction_0000", "nope"]), roots)


def test_a_policy_that_is_not_a_spec_is_refused_on_shape_alone(roots):
    # Shape only: importing it to see whether it loads would pull torch and a CUDA context into
    # the supervisor, and the run itself already reports `permanent: true` when it will not.
    with pytest.raises(JobRefused, match="is not a spec"):
        resolve(job(policy="scenariobank.policies.ExpertPolicy"), roots)


def test_a_checkpoint_name_nothing_matches_is_refused(roots):
    roots.models.mkdir()
    with pytest.raises(JobRefused, match="no checkpoint named"):
        resolve(job(checkpoint_path="step_440000.ep"), roots)


def test_a_checkpoint_name_two_files_match_is_refused_and_says_how_many(roots):
    # Two files with one name under the models root is a submission whose weights nobody can
    # name afterwards; picking either would put a number on a model that was not scored.
    for where in ("a", "b"):
        (roots.models / where).mkdir(parents=True)
        (roots.models / where / "step_440000.ep").write_bytes(b"")
    with pytest.raises(JobRefused, match="2 files named"):
        resolve(job(checkpoint_path="step_440000.ep"), roots)


def test_an_absolute_checkpoint_path_is_refused(roots):
    roots.models.mkdir()
    with pytest.raises(JobRefused, match="absolute path"):
        resolve(job(checkpoint_path="/models/step_440000.ep"), roots)


def test_a_model_named_with_no_models_root_at_all_is_refused(roots):
    with pytest.raises(JobRefused, match="no models root"):
        resolve(job(checkpoint_path="step_440000.ep"), roots)


def test_something_that_is_not_a_job_is_refused_as_such(tmp_path):
    with pytest.raises(JobRefused, match="not a job"):
        parse_job('{"schema_version": 1}')
    missing = tmp_path / "nope.json"
    with pytest.raises(JobRefused, match="cannot read"):
        read_job(missing)


# --- what a good job becomes ---------------------------------------------------------------------


def test_a_good_job_is_rewritten_into_the_containers_own_paths(roots):
    resolved = resolve(job(), roots, gpu=1)
    # The bank travels as a bind mount and nothing else: the container is told /bank, and the
    # host directory to mount there is kept beside it as a Path rather than a str.
    assert resolved.job.bank.path == CONTAINER_BANK
    assert resolved.bank_dir == (roots.banks / "t-junction").resolve()
    assert resolved.job.bank.id == "t-junction"
    assert resolved.out_dir == roots.out / "j7"
    assert resolved.gpu == 1
    assert resolved.job_id == "j7" and resolved.attempt == 1
    # Nothing else about the job was touched.
    assert resolved.job.policy == "scenariobank.policies:ExpertPolicy"
    assert resolved.job.scenarios == ["t_junction_0000"]


def test_a_job_that_names_no_model_needs_no_models_mount(roots):
    assert resolve(job(), roots).models_dir is None


def test_a_checkpoint_is_found_by_name_and_named_to_the_container_under_models(roots):
    (roots.models / "av3").mkdir(parents=True)
    (roots.models / "av3" / "step_440000.ep").write_bytes(b"")
    (roots.models / "av3" / "model_dev.yml").write_text("x: 1\n")
    resolved = resolve(
        job(checkpoint_path="step_440000.ep", model_config_path="model_dev.yml"), roots
    )
    assert resolved.job.checkpoint_path == "/models/av3/step_440000.ep"
    assert resolved.job.model_config_path == "/models/av3/model_dev.yml"
    assert resolved.models_dir == roots.models


def test_the_batch_directory_follows_the_jobs_tier_and_the_process_files_do_not(roots):
    # `events.jsonl` and `exit_code` belong to the process and stay in the directory the agent
    # named; everything the batch writes moves down a level. A supervisor that looked in one
    # place for both would report a finished run as one that wrote nothing.
    plain = resolve(job(), roots)
    assert plain.batch_dir == plain.out_dir
    tiered = resolve(job(options=JobOptions(tier="hard")), roots)
    assert tiered.batch_dir == tiered.out_dir / "hard"


def test_the_container_name_says_the_card_the_job_and_the_attempt(roots):
    resolved = resolve(job(job_id="a/b c", attempt=3), roots, gpu=2, job_id="a-b-c")
    assert resolved.container_name == "scenariobank-gpu2-a-b-c-3"


def test_an_awkward_job_id_is_sanitised_for_docker_but_kept_as_itself(roots):
    resolved = resolve(job(job_id="01J9:ZK@X"), roots)
    assert resolved.job_id == "01J9:ZK@X"  # the queue's id, unchanged, for the results directory
    assert resolved.container_name == "scenariobank-gpu0-01J9-ZK-X-1"


def test_a_job_with_no_attempt_is_attempt_one(roots):
    assert resolve(job(attempt=None), roots).attempt == 1


def test_the_resolved_job_still_validates_as_a_job(roots, tmp_path):
    # It is written to disk and read back by `run --job`, so it has to survive the round trip
    # through the same model with `extra="forbid"`.
    resolved = resolve(job(), roots)
    text = json.dumps(resolved.job.model_dump(mode="json"))
    assert Job.model_validate_json(text) == resolved.job
