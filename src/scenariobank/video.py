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

The one rule: **this reads the scene and never writes it.** The renderer is asked between two
steps, after the loop's bookkeeping and before the env is stepped again, and the gate in
`test_reproducibility.py` is what says the film changed nothing -- the same row with and
without a film has the same `actions_digest`. `cv2` arrives with the simulator
(`metadrive-simulator` requires `opencv-python`), and is imported inside `Recorder`, so this
module imports on a machine without either.
"""

from __future__ import annotations

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
        self.frames = 0

    def open(self, path: Path, fps: float) -> Recorder:
        """Start a film at `path`, `fps` frames a second -- the step rate, for real time."""
        import cv2

        path = Path(path)
        if not (isinstance(fps, int | float) and fps > 0):
            raise VideoError(f"a film needs a positive frame rate, not {fps!r}")
        if not path.parent.is_dir():
            raise VideoError(f"the film's directory does not exist: {path.parent}")
        fourcc = cv2.VideoWriter_fourcc(*FOURCC)
        writer = cv2.VideoWriter(str(path), fourcc, float(fps), SCREEN_SIZE)
        if not writer.isOpened():
            raise VideoError(f"OpenCV could not open {path} for writing with {FOURCC}")
        self._writer = writer
        self.path = path
        self.frames = 0
        return self

    def write(self, image: np.ndarray) -> None:
        """Append one BGR frame of `SCREEN_SIZE` pixels."""
        if self._writer is None:
            raise VideoError("the recorder is not open")
        height, width = image.shape[:2]
        if (width, height) != SCREEN_SIZE:
            raise VideoError(f"a frame is {width}x{height}, and the film is {SCREEN_SIZE}")
        self._writer.write(image)
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


__all__ = ["FILM_SIZE", "FOURCC", "SCALING", "SCREEN_SIZE", "Recorder", "VideoError", "frame"]
