"""`doctor` is the only thing standing between a reproducible bank and a plausible one."""

import pytest

from scenariobank.config import OBSERVATION_SHAPE
from scenariobank.doctor import (
    DoctorReport,
    check,
    collect,
    format_report,
    has_simulator,
    resolved_source,
)
from scenariobank.handedness import DRIVE_SIDE_LEFT, DRIVE_SIDE_RIGHT

# Named, so that a skip is legible in the report rather than a silent absence. A guard that
# stops running quietly is worse than one that fails.
needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)

HEALTHY = dict(
    report_version=1,
    metadrive_version="0.4.3",
    edition="MetaDrive v0.4.3",
    dist_version="0.4.3",
    commit="85e5dadc6c7436d324348f6e3d8f8e680c06b4db",
    requested_revision="85e5dadc",
    asset_version="0.4.3",
    python_version="3.10.14",
    packages={"numpy": "2.2.6"},
    observation_space="Box(19,)",
    observation_shape=OBSERVATION_SHAPE,
    action_space="Box(2,)",
    drive_side=DRIVE_SIDE_LEFT,
)


def report(**overrides) -> DoctorReport:
    """A report that passes every check, with the fields under test overridden."""
    return DoctorReport(**{**HEALTHY, **overrides})


def test_a_healthy_environment_reports_no_problems():
    assert check(report()) == []


def test_a_wheel_install_is_rejected_because_its_simulator_cannot_be_identified():
    problems = check(report(commit=None))
    assert len(problems) == 1
    assert "cannot be identified" in problems[0]


def test_requiring_the_wrong_commit_fails_even_when_the_version_string_matches():
    # The whole reason `doctor` exists: EDITION and dist version are identical across the tag
    # and the commit 32 patches past it.
    problems = check(report(), require_commit="deadbeef")
    assert len(problems) == 1
    assert "different bank" in problems[0]


def test_requiring_a_commit_prefix_of_the_installed_commit_passes():
    assert check(report(), require_commit="85e5dadc") == []


def test_a_missing_asset_tree_is_reported_rather_than_ignored():
    assert any("asset version" in problem for problem in check(report(asset_version=None)))


@pytest.mark.parametrize("shape", [(259,), (41,), (31,), (19, 1)])
def test_any_observation_other_than_the_pinned_one_fails(shape):
    problems = check(report(observation_shape=shape))
    assert len(problems) == 1
    assert "lidar block" in problems[0]


def test_a_right_side_traffic_environment_is_rejected():
    """The mirror not taking is silent everywhere else: the bank builds, and it is wrong."""
    problems = check(report(drive_side=DRIVE_SIDE_RIGHT))
    assert len(problems) == 1
    assert "traffic keeps right" in problems[0]


def test_an_unmeasured_drive_side_is_not_treated_as_a_failure():
    assert check(report(drive_side=None)) == []


def test_an_unprobed_report_does_not_invent_an_observation_verdict():
    # --no-probe skips the reset; it must not then claim the observation is wrong.
    assert check(report(observation_shape=None, observation_space=None)) == []


def test_the_formatted_report_names_every_field_and_marks_absent_ones():
    text = format_report(report(commit=None))
    assert "EDITION:" in text
    assert "commit:" in text
    assert text.splitlines()[3].endswith("-")


def test_resolving_the_source_of_a_package_that_is_not_installed_is_not_an_error():
    assert resolved_source("a-distribution-that-does-not-exist") == (None, None)


@needs_sim
def test_the_installed_simulator_is_the_pinned_commit_with_the_pinned_observation():
    collected = collect(probe=True)
    assert collected.observation_shape == OBSERVATION_SHAPE
    assert check(collected, require_commit="85e5dadc") == []


def test_the_simulator_guard_agrees_with_actually_importing_the_simulator():
    # Runs in both configurations and is the check that matters: uninstalling the sim group
    # leaves `site-packages/metadrive/assets/` behind, Python reads the leftover directory as a
    # namespace package, and a guard built on `find_spec(...) is None` answers "installed" for
    # a MetaDrive that cannot be imported. Every needs_sim skipif in this repo rests on this.
    try:
        import metadrive.envs.base_env  # noqa: F401

        importable = True
    except ImportError:
        importable = False
    assert has_simulator() is importable
