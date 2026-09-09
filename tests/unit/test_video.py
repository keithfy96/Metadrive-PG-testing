"""`video.py` and the loop's `observe` hook: a film that changes nothing.

Offline first: the recorder writes what it is handed and refuses what it cannot write, by
name, and the hook fires once after the reset and once after every step, the ending step
included. Then one live test, the one the step exists under: a `hard` row of `banks/curve`
filmed is the same row as unfilmed -- `actions_digest` and all -- and the film holds
`steps + 1` frames of `SCREEN_SIZE` at the step rate.

`cv2` arrives with the simulator, so the recorder tests skip without it; the hook test needs
neither.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scenariobank.doctor import has_simulator
from scenariobank.results import JOB_SCHEMA_VERSION, Job, JobBank, JobOptions
from scenariobank.runner import run_bank, run_episode
from scenariobank.video import SCREEN_SIZE, Recorder, VideoError


def _has_cv2() -> bool:
    try:
        import cv2  # noqa: F401
    except ImportError:
        return False
    return True


needs_cv2 = pytest.mark.skipif(not _has_cv2(), reason="needs OpenCV (uv sync --group sim)")
needs_sim = pytest.mark.skipif(
    not has_simulator(),
    reason="needs_sim: MetaDrive is not installed (uv sync --group sim)",
)
CURVE = Path("banks/curve")
needs_bank = pytest.mark.skipif(
    not (CURVE / "manifest.json").exists(), reason=f"needs the bank at {CURVE}"
)


def read_back(path: Path) -> tuple[int, float, int, int]:
    """Frames, fps, width, height of the file at `path`, off OpenCV."""
    import cv2

    capture = cv2.VideoCapture(str(path))
    try:
        return (
            int(capture.get(cv2.CAP_PROP_FRAME_COUNT)),
            float(capture.get(cv2.CAP_PROP_FPS)),
            int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
    finally:
        capture.release()


# --- offline: the recorder ---------------------------------------------------------------------


@needs_cv2
def test_the_recorder_writes_what_it_is_handed_at_the_rate_it_was_given(tmp_path):
    recorder = Recorder().open(tmp_path / "x.mp4", fps=10)
    width, height = SCREEN_SIZE
    for shade in (0, 128, 255):
        recorder.write(np.full((height, width, 3), shade, dtype=np.uint8))
    assert recorder.close() == 3
    assert recorder.close() == 3, "closing twice is fine and says the same"
    assert read_back(tmp_path / "x.mp4") == (3, 10.0, width, height)


@needs_cv2
def test_the_recorder_refuses_by_name(tmp_path):
    with pytest.raises(VideoError, match="positive frame rate"):
        Recorder().open(tmp_path / "x.mp4", fps=0)
    with pytest.raises(VideoError, match="directory does not exist"):
        Recorder().open(tmp_path / "nowhere" / "x.mp4", fps=10)
    with pytest.raises(VideoError, match="not open"):
        Recorder().write(np.zeros((*SCREEN_SIZE, 3), dtype=np.uint8))
    recorder = Recorder().open(tmp_path / "y.mp4", fps=10)
    with pytest.raises(VideoError, match="640x480"):
        recorder.write(np.zeros((480, 640, 3), dtype=np.uint8))
    recorder.close()


# --- offline: the hook ---------------------------------------------------------------------------


class FakeSpace:
    shape = (2,)


class FakeEnv:
    def __init__(self, ends_at: int | None = None) -> None:
        self.ends_at = ends_at
        self.action_space = FakeSpace()
        self.taken = 0

    def reset(self, *, seed: int):
        del seed
        return np.zeros(19), {}

    def step(self, action):
        del action
        self.taken += 1
        done = self.ends_at is not None and self.taken >= self.ends_at
        return np.zeros(19), 0.0, False, done, {"max_step": True} if done else {}


def test_observe_sees_the_scene_after_the_reset_and_after_every_step_the_last_included():
    seen: list[int] = []
    env = FakeEnv(ends_at=4)
    drive = run_episode(
        env, seed=0, prepare=lambda _e: None, cap=50, stride=1,
        act=lambda _o: (0.0, 0.0), observe=lambda e: seen.append(e.taken),
    )
    assert drive.steps == 4
    assert seen == [0, 1, 2, 3, 4], "the placed scene, then every step, the ending one too"


def test_observe_is_not_asked_after_a_stop_that_landed_before_a_step():
    seen: list[int] = []
    env = FakeEnv()
    drive = run_episode(
        env, seed=0, prepare=lambda _e: None, cap=50, stride=1,
        act=lambda _o: (0.0, 0.0), stop=lambda: env.taken >= 3,
        observe=lambda e: seen.append(e.taken),
    )
    assert drive.stopped and drive.steps == 3
    assert seen == [0, 1, 2, 3]


# --- live: the film changes nothing ----------------------------------------------------------


@needs_sim
@needs_bank
def test_a_filmed_row_is_the_unfilmed_row_and_the_film_holds_every_step(tmp_path):
    job = Job(
        schema_version=JOB_SCHEMA_VERSION,
        bank=JobBank(path=str(CURVE)),
        scenarios=["curve_0004"],
        options=JobOptions(tier="hard"),
        policy="scenariobank.policies:ExpertPolicy",
    )
    filmed = run_bank(job, tmp_path / "film", record_video=True)
    plain = run_bank(job, tmp_path / "plain")
    assert filmed.results[0].model_dump(exclude={"wall_time_s"}) == (
        plain.results[0].model_dump(exclude={"wall_time_s"})
    )
    assert filmed.results[0].placed["TrafficCone"], "a row with something to look at"
    film = tmp_path / "film" / "videos" / "curve_0004.mp4"
    frames, fps, width, height = read_back(film)
    assert (frames, fps) == (filmed.results[0].steps + 1, filmed.env.step_hz)
    assert (width, height) == SCREEN_SIZE
    assert not (tmp_path / "plain" / "videos").exists()
