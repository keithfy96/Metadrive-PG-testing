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
from scenariobank.video import SCREEN_SIZE, CameraFilm, Recorder, VideoError, chain, mosaic


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


@needs_cv2
def test_the_recorder_takes_a_size_and_holds_its_frames_to_it(tmp_path):
    recorder = Recorder().open(tmp_path / "cam.mp4", fps=10, size=(64, 48))
    recorder.write(np.zeros((48, 64, 3), dtype=np.uint8))
    with pytest.raises(VideoError, match=r"800x800, and the film is \(64, 48\)"):
        recorder.write(np.zeros((800, 800, 3), dtype=np.uint8))
    assert recorder.close() == 1
    assert read_back(tmp_path / "cam.mp4") == (1, 10.0, 64, 48)


# --- offline: the camera film ---------------------------------------------------------------------


def tile(shade: int, height: int = 4, width: int = 6) -> np.ndarray:
    return np.full((height, width, 3), shade, dtype=np.uint8)


def test_a_mosaic_tiles_three_across_in_order_and_pads_the_last_row_with_black():
    six = mosaic([tile(n) for n in (1, 2, 3, 4, 5, 6)])
    assert six.shape == (8, 18, 3)
    assert [int(six[0, c * 6, 0]) for c in range(3)] == [1, 2, 3]
    assert [int(six[4, c * 6, 0]) for c in range(3)] == [4, 5, 6]
    four = mosaic([tile(n) for n in (1, 2, 3, 4)])
    assert four.shape == (8, 18, 3)
    assert [int(four[4, c * 6, 0]) for c in range(3)] == [4, 0, 0], "black where it runs short"
    one = mosaic([tile(9)])
    assert one.shape == (4, 6, 3) and int(one[0, 0, 0]) == 9
    assert mosaic([tile(1), tile(2)], columns=1).shape == (8, 6, 3)


def test_a_mosaic_of_mixed_sizes_or_nothing_is_refused_by_name():
    with pytest.raises(VideoError, match="tile 1 is 8x4 and the first is 6x4"):
        mosaic([tile(1), tile(2, width=8)])
    with pytest.raises(VideoError, match="no frames"):
        mosaic([])


class FakeCamera:
    def __init__(self, name: str, width: int, height: int) -> None:
        self.name, self.width, self.height = name, width, height


class FakeRig:
    """Reads a frame per camera whose shade is the read count, so a film's frames are ordered."""

    def __init__(self, *cameras: FakeCamera) -> None:
        self.cameras = list(cameras)
        self.reads = 0

    def read(self):
        self.reads += 1
        return {
            camera.name: np.full((camera.height, camera.width, 3), self.reads, dtype=np.uint8)
            for camera in self.cameras
        }


@needs_cv2
def test_the_camera_film_writes_one_file_per_camera_and_the_mosaic_from_one_read(tmp_path):
    rig = FakeRig(FakeCamera("a", 64, 48), FakeCamera("b", 64, 48), FakeCamera("c", 64, 48),
                  FakeCamera("d", 64, 48))
    film = CameraFilm().open(tmp_path, "row", rig, fps=10)
    for _ in range(3):
        film.add(env=None)
    assert film.close() == 3
    assert rig.reads == 3, "one read of the rig per frame, however many films"
    assert [path.name for path in film.paths] == [
        "row.a.mp4", "row.b.mp4", "row.c.mp4", "row.d.mp4", "row.rig.mp4"
    ]
    for name in ("a", "b", "c", "d"):
        assert read_back(tmp_path / f"row.{name}.mp4") == (3, 10.0, 64, 48)
    assert read_back(tmp_path / "row.rig.mp4") == (3, 10.0, 192, 96), "four tiles, 3x2"
    assert film.mosaic_skipped is None
    with pytest.raises(VideoError, match="not open"):
        CameraFilm().add(env=None)


@needs_cv2
def test_a_rig_of_two_sizes_is_filmed_per_camera_and_the_mosaic_is_skipped_by_name(tmp_path):
    rig = FakeRig(FakeCamera("wide", 128, 48), FakeCamera("narrow", 64, 48))
    film = CameraFilm().open(tmp_path, "row", rig, fps=10)
    film.add(env=None)
    film.close()
    assert [path.name for path in film.paths] == ["row.wide.mp4", "row.narrow.mp4"]
    assert film.mosaic_skipped == "the rig's cameras are 64x48, 128x48 and a mosaic tiles one size"
    assert not (tmp_path / "row.rig.mp4").exists()


def test_chain_runs_every_hook_in_order_and_is_nothing_when_there_are_none():
    seen = []
    both = chain(lambda env: seen.append(("a", env)), None, lambda env: seen.append(("b", env)))
    both("env")
    assert seen == [("a", "env"), ("b", "env")]
    only = seen.append
    assert chain(None, only) is only
    assert chain(None, None) is None


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
