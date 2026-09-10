"""A top-down film of a run, for the eye. Not a measurement, and never a cause.

Phase 4 Step 5 proved that two runs of one job agree to the digest; this module exists so a
person can *look* at one -- cones and barriers on the lane, pedestrians crossing, a cyclist on
the outer lane, the traffic, the ego, and the crash that ends the row. Three facts it rests on:

**MetaDrive's live top-down renderer draws every non-map object.** `TopDownRenderer.render`
(`engine/top_down_renderer.py:343`) collects `engine.get_objects()` minus the map each frame
and draws each with its own `top_down_width`, `top_down_length` and `top_down_color`: vehicles,
`TrafficCone` and `TrafficBarrier` (`static_object/traffic_object.py`), `Pedestrian` and
`Cyclist` (`traffic_participants/`). The plan's "map only" note is about `draw_top_down_map`,
the thumbnail path, not this one.

**It is headless, on the CPU.** `window=False` renders to a pygame surface and never opens a
screen; the only `pygame.init()` sits behind `show_agent_name`. No GPU, no display, no
container -- it runs wherever `scenariobank run` runs. Measured on `banks/curve` at `hard`: the
first frame costs 0.14 s (the 4000x4000 film of the map is drawn once), every frame after it
15 ms at 800x800.

**The frame comes back RGB and OpenCV wants BGR.** `to_cv2_image` (`obs/top_down_obs_impl.py`)
transposes the pygame pixels and swaps nothing, so `frame` swaps the channels here, once.

**A film from the car's cameras is the same hook, reading a rig** (Phase 4 Step 6b). With a
`CameraRig` on the ego, `CameraFilm` reads every camera once per step -- `rig.read()` hands back
`(H, W, 3)` uint8 already in BGR, panda3d's `getRamImage` order sliced to three channels, so
nothing is swapped -- and writes one mp4 per camera beside a mosaic of all of them tiled three
across, `<stem>.<camera>.mp4` and `<stem>.rig.mp4`. At the step rate, so a 10 Hz road plays in
real time at 10 fps whatever the spec's `tick_rate` says: a film is a look and not a model input,
which is why `run --ignore-rig-rate` exists for it.

The one rule: **this reads the scene and never writes it.** The renderer is asked between two
steps, after the loop's bookkeeping and before the env is stepped again, and the gate in
`test_reproducibility.py` is what says the film changed nothing -- the same row with and
without a film has the same `actions_digest`. `cv2` arrives with the simulator
(`metadrive-simulator` requires `opencv-python`), and is imported inside `Recorder`, so this
module imports on a machine without either.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np

#: The frame, in pixels. Square, because the renderer's film is square.
SCREEN_SIZE: tuple[int, int] = (800, 800)
#: Pixels per metre: 800 px / 5 = a 160 m window around the ego, a pedestrian about 3 px wide.
SCALING: int = 5
#: The film the map is drawn on once, in pixels: 800 m of road at `SCALING`. A map that runs
#: past the film's edge is white past it; `banks/curve`'s 453 m route fits with room.
FILM_SIZE: tuple[int, int] = (4000, 4000)
#: The mp4 codec OpenCV's own build carries everywhere; `avc1` needs a system library.
FOURCC: str = "mp4v"
#: How many camera views sit side by side in a rig's mosaic: the AV3 rig's six become 3x2.
MOSAIC_COLUMNS: int = 3


class VideoError(ValueError):
    """A film that cannot be written as asked. Says what, by name."""


def frame(env: Any) -> np.ndarray:
    """One top-down frame of `env` as it stands, BGR, `SCREEN_SIZE` pixels, north up.

    The camera follows the ego (`camera_position` unset, `center_on_map=False`) and the map
    stays north-up (`target_agent_heading_up=False`), so the road is still and the objects on it
    are what moves. The renderer is built on the first call with these settings and cleared by
    the env's next `reset`.
    """
    image = env.render(
        mode="topdown",
        window=False,
        screen_size=SCREEN_SIZE,
        scaling=SCALING,
        film_size=FILM_SIZE,
        target_agent_heading_up=False,
        draw_contour=True,
    )
    return np.ascontiguousarray(np.asarray(image)[:, :, ::-1])


class Recorder:
    """Frames of one episode into one mp4. `open`, `add` per step, `close`.

    `add` takes the env and asks `frame` for the picture, so the loop's hook is
    `recorder.add`; `write` takes a frame already made, which is what the tests hand in.
    """

    def __init__(self) -> None:
        self._writer: Any = None
        self.path: Path | None = None
        self.size: tuple[int, int] = SCREEN_SIZE
        self.frames = 0

    def open(self, path: Path, fps: float, size: tuple[int, int] = SCREEN_SIZE) -> Recorder:
        """Start a film at `path`, `fps` frames a second -- the step rate, for real time.

        `size` is `(width, height)`: the top-down `SCREEN_SIZE` unless the film is a camera's.
        """
        import cv2

        path = Path(path)
        if not (isinstance(fps, int | float) and fps > 0):
            raise VideoError(f"a film needs a positive frame rate, not {fps!r}")
        if not path.parent.is_dir():
            raise VideoError(f"the film's directory does not exist: {path.parent}")
        fourcc = cv2.VideoWriter_fourcc(*FOURCC)
        writer = cv2.VideoWriter(str(path), fourcc, float(fps), tuple(size))
        if not writer.isOpened():
            raise VideoError(f"OpenCV could not open {path} for writing with {FOURCC}")
        self._writer = writer
        self.path = path
        self.size = (int(size[0]), int(size[1]))
        self.frames = 0
        return self

    def write(self, image: np.ndarray) -> None:
        """Append one BGR frame of the film's own size."""
        if self._writer is None:
            raise VideoError("the recorder is not open")
        height, width = image.shape[:2]
        if (width, height) != self.size:
            raise VideoError(f"a frame is {width}x{height}, and the film is {self.size}")
        self._writer.write(np.ascontiguousarray(image))
        self.frames += 1

    def add(self, env: Any) -> None:
        """The loop's hook: one frame of `env` as it stands now."""
        self.write(frame(env))

    def close(self) -> int:
        """Finish the file and return how many frames it holds. Safe to call twice."""
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        return self.frames


def mosaic(frames: list[np.ndarray], columns: int = MOSAIC_COLUMNS) -> np.ndarray:
    """Tile same-sized frames left to right, top to bottom, `columns` across; black where the
    last row runs short. Pure, so the layout is tested with arrays."""
    if not frames:
        raise VideoError("a mosaic of no frames")
    height, width = frames[0].shape[:2]
    for index, tile in enumerate(frames):
        if tile.shape[:2] != (height, width):
            raise VideoError(
                f"mosaic tile {index} is {tile.shape[1]}x{tile.shape[0]} and the first is "
                f"{width}x{height}; the tiles of one mosaic are one size"
            )
    columns = max(1, min(columns, len(frames)))
    rows = math.ceil(len(frames) / columns)
    sheet = np.zeros((rows * height, columns * width, 3), dtype=np.uint8)
    for index, tile in enumerate(frames):
        row, column = divmod(index, columns)
        sheet[row * height : (row + 1) * height, column * width : (column + 1) * width] = tile
    return sheet


class CameraFilm:
    """Every camera of a rig into one mp4 each, plus the mosaic of all of them. `open`, `add`,
    `close`, the same shape as `Recorder` so the loop's hook is `film.add`.

    The mosaic is skipped, by name, when the rig's cameras are not one size; the per-camera
    films are written either way.
    """

    def __init__(self) -> None:
        self.rig: Any = None
        self._per_camera: dict[str, Recorder] = {}
        self._mosaic: Recorder | None = None
        self.paths: list[Path] = []
        self.mosaic_skipped: str | None = None
        self.frames = 0

    def open(self, directory: Path, stem: str, rig: Any, fps: float) -> CameraFilm:
        """`<directory>/<stem>.<camera>.mp4` per camera and `<directory>/<stem>.rig.mp4`."""
        directory = Path(directory)
        self.rig = rig
        self._per_camera = {}
        self.paths = []
        for camera in rig.cameras:
            path = directory / f"{stem}.{camera.name}.mp4"
            self._per_camera[camera.name] = Recorder().open(
                path, fps=fps, size=(camera.width, camera.height)
            )
            self.paths.append(path)
        sizes = {(camera.width, camera.height) for camera in rig.cameras}
        if len(sizes) == 1:
            width, height = next(iter(sizes))
            columns = max(1, min(MOSAIC_COLUMNS, len(rig.cameras)))
            rows = math.ceil(len(rig.cameras) / columns)
            path = directory / f"{stem}.rig.mp4"
            self._mosaic = Recorder().open(path, fps=fps, size=(columns * width, rows * height))
            self.paths.append(path)
        else:
            self.mosaic_skipped = (
                "the rig's cameras are " + ", ".join(f"{w}x{h}" for w, h in sorted(sizes))
                + " and a mosaic tiles one size"
            )
        self.frames = 0
        return self

    def add(self, env: Any) -> None:
        """The loop's hook: one read of the rig, written to every film."""
        del env  # the cameras are on the ego already; the rig reads its own sensors
        if self.rig is None:
            raise VideoError("the camera film is not open")
        frames = self.rig.read()
        for name, recorder in self._per_camera.items():
            recorder.write(frames[name])
        if self._mosaic is not None:
            self._mosaic.write(mosaic([frames[name] for name in self._per_camera]))
        self.frames += 1

    def close(self) -> int:
        """Finish every file and return how many frames each holds. Safe to call twice."""
        for recorder in self._per_camera.values():
            recorder.close()
        if self._mosaic is not None:
            self._mosaic.close()
        return self.frames


def chain(*hooks: Any) -> Any:
    """One `observe` out of several, in order; `None` when there is nothing to observe."""
    live = [hook for hook in hooks if hook is not None]
    if not live:
        return None
    if len(live) == 1:
        return live[0]

    def observe(env: Any) -> None:
        for hook in live:
            hook(env)

    return observe


__all__ = [
    "FILM_SIZE",
    "FOURCC",
    "MOSAIC_COLUMNS",
    "SCALING",
    "SCREEN_SIZE",
    "CameraFilm",
    "Recorder",
    "VideoError",
    "chain",
    "frame",
    "mosaic",
]
