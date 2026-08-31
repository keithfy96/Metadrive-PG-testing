"""Environment truth: which simulator is this, exactly.

Every artifact this repo produces is only meaningful relative to one simulator build. A version
string is not enough to name it -- `metadrive.constants.EDITION` reports `"MetaDrive v0.4.3"` for
both the 0.4.3 tag and the commit 32 patches past it that we actually run. So `doctor` reports the
resolved git SHA out of the installed distribution's `direct_url.json`, alongside the asset
version and the observation the canonical config produces.

Run it first on any machine, and in the container. If `commit` is `None`, stop.
"""

from __future__ import annotations

import importlib.metadata
import json
import platform
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from scenariobank.config import OBSERVATION_SHAPE, base_config
from scenariobank.handedness import DRIVE_SIDE_LEFT, DRIVE_SIDE_RIGHT

#: Reported alongside the simulator because each of them has, at some point, changed a rendered
#: frame or a geometric result without changing anything MetaDrive reports about itself.
COMPANION_PACKAGES = ("numpy", "shapely", "opencv-python", "panda3d", "pygame")

SIMULATOR_DIST = "metadrive-simulator"


class DoctorError(RuntimeError):
    """Raised when the environment cannot be identified, or does not match what was required."""


class DoctorReport(BaseModel):
    """What machine this is, in the only terms that make a bank reproducible."""

    model_config = ConfigDict(extra="forbid")

    report_version: Literal[1]
    metadrive_version: str | None
    edition: str | None
    dist_version: str | None
    commit: str | None
    requested_revision: str | None
    asset_version: str | None
    python_version: str
    packages: dict[str, str | None]
    observation_space: str | None
    observation_shape: tuple[int, ...] | None
    action_space: str | None
    #: Which side of the road traffic keeps to, **measured** from the map this environment
    #: actually builds -- not read off `handedness._installed`. A flag that says "mirrored"
    #: while the maps come out right-side-traffic is the one failure that would silently
    #: invalidate every scenario in the bank, so the check does not trust the flag.
    drive_side: str | None


def has_simulator() -> bool:
    """True when MetaDrive is importable, not merely present as a directory.

    `importlib.util.find_spec("metadrive") is None` is not enough, and the difference is not
    academic. MetaDrive downloads its asset tree into `site-packages/metadrive/assets/` at
    first use; uv does not own those files, so uninstalling the `sim` group removes every
    module but leaves that directory standing. Python reads the leftover as a **namespace
    package**, `find_spec` returns a spec, and a `needs_sim` guard written that way stops
    skipping -- so the simulator tests fail instead of skipping, which is precisely the failure
    the repo's testing rule is aimed at: a guard that stops guarding silently.

    The test is `origin`, not `loader`. A namespace package has no origin, ever. Its `loader`
    starts out `None` but becomes a real `_NamespaceLoader` the moment anything imports it --
    including a *failed* `import metadrive.envs.x`, which still binds the parent -- so a
    loader-based check answers differently depending on what ran before it.
    """
    spec = importlib.util.find_spec("metadrive")
    return spec is not None and spec.origin is not None


def _dist_version(name: str) -> str | None:
    """Return an installed distribution's version, or None if it is not installed."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def resolved_source(name: str = SIMULATOR_DIST) -> tuple[str | None, str | None]:
    """Return `(commit_id, requested_revision)` for a VCS-installed distribution.

    pip and uv both write PEP 610 `direct_url.json` into the dist-info of anything installed
    from a URL. For a git install it carries the *resolved* commit, which is the only field in
    the environment that distinguishes our simulator from the released tag. A wheel from PyPI
    has no such file, and that is itself the finding: `(None, None)` means nobody can say which
    MetaDrive this is.
    """
    try:
        distribution = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None, None
    raw = distribution.read_text("direct_url.json")
    if not raw:
        return None, None
    try:
        vcs_info = json.loads(raw).get("vcs_info") or {}
    except json.JSONDecodeError:
        return None, None
    return vcs_info.get("commit_id"), vcs_info.get("requested_revision")


def probe_simulator() -> dict[str, Any]:
    """Build a throwaway env from `base_config()` and report the spaces it exposes.

    This costs one reset, and it is the earliest point at which a stray lidar setting becomes
    visible: it shows up as a wider observation rather than as an error. The runner asserts the
    same shape from the same constant, so a bank that passes `doctor` cannot surprise the
    runner about what a policy will be handed.
    """
    from metadrive.envs.metadrive_env import MetaDriveEnv

    env = MetaDriveEnv(base_config())
    try:
        env.reset(seed=0)
        observation_space = env.observation_space
        action_space = env.action_space
        shape = tuple(observation_space.shape) if observation_space.shape else None
        return {
            "observation_space": str(observation_space),
            "observation_shape": shape,
            "action_space": str(action_space),
            "drive_side": measure_drive_side(env),
        }
    finally:
        env.close()


def measure_drive_side(env: Any) -> str | None:
    """Which side of the road the ego drives on, read off the map it was spawned into.

    Finds the opposing carriageway of the ego's own road and asks which side of the ego it is
    on. Oncoming traffic on the left means traffic keeps right; oncoming on the right means
    traffic keeps left. Returns `None` when the spawn road is one-way, which is not a failure --
    it just means this map cannot answer the question.
    """
    import numpy as np

    agent = env.agent
    road = agent.navigation.current_road
    network = env.engine.current_map.road_network
    opposing = (network.graph.get("-" + road.end_node) or {}).get("-" + road.start_node)
    if not opposing:
        return None

    position = np.asarray(agent.position, dtype=float)
    heading = float(agent.heading_theta)
    to_the_left = np.array([-np.sin(heading), np.cos(heading)])
    lateral = float(
        np.mean(
            [
                (np.asarray(lane.position(lane.length / 2, 0), dtype=float) - position)
                @ to_the_left
                for lane in opposing
            ]
        )
    )
    return DRIVE_SIDE_RIGHT if lateral > 0 else DRIVE_SIDE_LEFT


def collect(*, probe: bool = True) -> DoctorReport:
    """Gather everything that identifies this environment."""
    try:
        from metadrive import constants, version
    except ImportError as error:
        raise DoctorError(
            f"MetaDrive is not importable ({error}). Install the sim group: "
            "`uv sync --group sim`."
        ) from error

    commit, requested = resolved_source()

    probed: dict[str, Any] = {
        "observation_space": None,
        "observation_shape": None,
        "action_space": None,
        "drive_side": None,
    }
    if probe:
        probed = probe_simulator()

    # Read *after* the probe, never before. MetaDrive downloads its asset tree lazily on the
    # first engine start (`base_engine.py:773`), so on a fresh machine the assets do not exist
    # until `probe_simulator()` has built an env -- and asking first would report every clean
    # container as broken.
    try:
        assets = version.asset_version()
    except (ValueError, OSError):
        # It raises rather than returning None when the tree is absent, which under
        # `--no-probe` is a genuine finding. Report it as absent; do not fail the command.
        assets = None

    return DoctorReport(
        report_version=1,
        metadrive_version=version.VERSION,
        edition=constants.EDITION,
        dist_version=_dist_version(SIMULATOR_DIST),
        commit=commit,
        requested_revision=requested,
        asset_version=assets,
        python_version=platform.python_version(),
        packages={name: _dist_version(name) for name in COMPANION_PACKAGES},
        **probed,
    )


def check(report: DoctorReport, *, require_commit: str | None = None) -> list[str]:
    """Return the reasons this environment is not usable. Empty means it is."""
    problems: list[str] = []

    if report.commit is None:
        problems.append(
            "no resolved commit: metadrive-simulator was not installed from git, so this "
            "simulator cannot be identified. Every later phase would be built on it blind."
        )
    elif require_commit and not report.commit.startswith(require_commit):
        problems.append(
            f"commit mismatch: required {require_commit}, installed {report.commit}. "
            "A different commit means a different bank."
        )

    if report.asset_version is None:
        problems.append(
            "no asset version: MetaDrive's asset tree is missing. Maps will render, but "
            "nothing that reads a texture or a model will."
        )

    if report.observation_shape is not None and report.observation_shape != OBSERVATION_SHAPE:
        problems.append(
            f"observation is {report.observation_shape}, expected {OBSERVATION_SHAPE}. "
            "The lidar block in `base_config()` did not take -- MetaDrive's default is 240 "
            "lasers, which reads as Box(259,)."
        )

    if report.drive_side is not None and report.drive_side != DRIVE_SIDE_LEFT:
        problems.append(
            f"traffic keeps {report.drive_side}, expected {DRIVE_SIDE_LEFT}. The mirror in "
            "`handedness.install()` did not take, so this environment builds MetaDrive's own "
            "right-side-traffic maps. Every scenario generated here would be the wrong "
            "market, and nothing else about it would look wrong."
        )

    return problems


def format_report(report: DoctorReport) -> str:
    """Render the report as aligned `key: value` lines, for pasting next to another machine's."""
    rows: list[tuple[str, object]] = [
        ("metadrive.VERSION", report.metadrive_version),
        ("EDITION", report.edition),
        ("dist", report.dist_version),
        ("commit", report.commit),
        ("requested", report.requested_revision),
        ("asset_version", report.asset_version),
        ("python", report.python_version),
        *report.packages.items(),
        ("obs_space", report.observation_space),
        ("action_space", report.action_space),
        ("drive_side", report.drive_side),
    ]
    width = max(len(key) for key, _ in rows)
    return "\n".join(f"{key + ':':<{width + 1}} {'-' if value is None else value}"
                     for key, value in rows)
