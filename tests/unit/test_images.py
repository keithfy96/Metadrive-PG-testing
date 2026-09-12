"""The two images this repo builds, checked without docker.

Both recipes were carried over from the converter on 2026-09-12 so a fresh clone builds them:
the sim image from `docker/Dockerfile` against our own lock, the bridge from `docker/openpilot/`
with the openpilot fork vendored under it. Each test here is a failure that was once found the
slow way -- four minutes into a container build, or a minute into a drive -- pulled forward to
the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM_DOCKERFILE = ROOT / "docker" / "Dockerfile"
BRIDGE = ROOT / "docker" / "openpilot"
FORK = BRIDGE / "deps" / "openpilot"

# The ten mode-120000 paths in the vendored fork; scripts/bridge.sh carries the same list.
VENDORED_SYMLINKS = [
    "rednose",
    "laika",
    "tinygrad",
    "selfdrive/hardware",
    "third_party/libyuv/x64/include",
    "third_party/snpe/x86_64",
    "third_party/snpe/larch64",
    "third_party/acados/x86_64/lib/libqpOASES_e.so",
    "third_party/acados/larch64/lib/libqpOASES_e.so",
    "third_party/acados/Darwin/lib/libqpOASES_e.dylib",
]


def _code_lines(text: str) -> str:
    """The file without its full-line comments, which discuss the very strings tested for."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _uv_sync_groups(dockerfile: Path) -> set[str]:
    """The groups every non-comment `uv sync` line names, unioned."""
    groups: set[str] = set()
    for line in dockerfile.read_text().splitlines():
        if line.lstrip().startswith("#") or "uv sync" not in line:
            continue
        groups.update(re.findall(r"--group ([a-z]+)", line))
    return groups


# --- the sim image ----------------------------------------------------------------------------


def test_sim_dockerfile_label_names_exactly_the_groups_it_syncs():
    text = SIM_DOCKERFILE.read_text()
    label = re.search(r'^LABEL wingfin\.groups="([^"]*)"', text, re.MULTILINE)
    assert label is not None, "no wingfin.groups label; scripts/sim-image.sh reads it"
    assert set(label.group(1).split()) == _uv_sync_groups(SIM_DOCKERFILE) == {"sim", "gpu", "model"}


def test_sim_dockerfile_copies_only_what_dockerignore_lets_through():
    text = SIM_DOCKERFILE.read_text()
    copied = set(re.findall(r"^COPY (?!--from)(.+?) \S+$", text, re.MULTILINE))
    files = {name for line in copied for name in line.split()}
    assert files == {"pyproject.toml", "uv.lock", "README.md", "src"}
    allowed = {
        line[1:]
        for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.startswith("!")
    }
    assert files <= allowed, f"COPYed but excluded by .dockerignore: {files - allowed}"


def _toml_array(text: str, key: str) -> list[str]:
    """The string items of a top-level `key = [ ... ]` array. Python 3.10 has no tomllib."""
    match = re.search(rf"^{re.escape(key)} = \[(.*?)^\]", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"no array {key!r} in pyproject.toml"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_pyproject_pins_the_model_stack_to_what_compiled_the_engine():
    text = (ROOT / "pyproject.toml").read_text()
    assert {"torch==2.8.0", "torch-tensorrt==2.8.0", "tensorrt==10.12.0.36"} <= set(
        _toml_array(text, "model")
    )
    assert any(dep.startswith("cuda-python>=12,<13") for dep in _toml_array(text, "gpu")), (
        "cuda-python 13 removed the cuda.cudart shim MetaDrive imports"
    )
    indexes = re.findall(
        r'\[\[tool\.uv\.index\]\]\nname = "([^"]+)"\nurl = "([^"]+)"\nexplicit = (\w+)', text
    )
    assert dict((name, url) for name, url, _ in indexes) == {
        "pytorch-cu128": "https://download.pytorch.org/whl/cu128",
        "nvidia": "https://pypi.nvidia.com",
    }
    assert all(explicit == "true" for _, _, explicit in indexes), (
        "a non-explicit index would pull every package through it"
    )
    assert 'torch = { index = "pytorch-cu128" }' in text
    assert 'tensorrt = { index = "nvidia" }' in text


def test_lock_resolves_torch_from_the_cu128_index():
    lock = (ROOT / "uv.lock").read_text()
    assert 'name = "torch"\nversion = "2.8.0+cu128"' in lock
    assert 'name = "torch-tensorrt"\nversion = "2.8.0+cu128"' in lock


def test_compose_runs_the_converters_image_and_does_not_build_it():
    # One sim image for both projects, the converter's. A `build:` on `run` would let a compose
    # build here put our smaller fallback under that tag, which fails a run minutes in on an
    # import; so the runner has no build key and the studio's is the only one in the file.
    compose = (ROOT / "compose.yaml").read_text()
    code = _code_lines(compose)
    assert code.count("build:") == 1, "only the studio builds; the runner's image is theirs"
    assert "dockerfile: docker/Dockerfile" not in code
    assert 'image: "${SIM_IMAGE:-metadrive-wingfin-sim:latest}"' in code
    assert 'SIM_IMAGE: "${SIM_IMAGE:-metadrive-wingfin-sim:latest}"' in code, "studio base arg"
    studio = (ROOT / "docker" / "studio.Dockerfile").read_text()
    assert "ARG SIM_IMAGE=metadrive-wingfin-sim:latest\nFROM ${SIM_IMAGE}\n" in studio


def test_build_pairs_each_tag_with_its_own_recipe():
    # `build` delegates to the converter checkout beside this repo when it is there, so one
    # command makes the shared image on every machine; else it makes our fallback. Either way
    # the converter's tag may come only from the converter's Dockerfile and the fallback's only
    # from ours -- a build under SIM_IMAGE could put the smaller image under the shared name.
    script = (ROOT / "scripts" / "sim-image.sh").read_text()
    code = _code_lines(script)
    assert 'CONVERTER_DIR="${CONVERTER_DIR:-../wingfin-osm-scenarionet-converter}"' in code
    assert "CONVERTER_IMAGE=metadrive-wingfin-sim:latest" in code
    assert 'CONVERTER_DOCKERFILE="$CONVERTER_DIR/docker/Dockerfile"' in code
    assert "FALLBACK_IMAGE=scenariobank-sim:latest" in code
    converter_route = (
        'tag="$CONVERTER_IMAGE"; recipe="$CONVERTER_DOCKERFILE"; context="$CONVERTER_DIR"'
    )
    assert converter_route in code
    assert 'tag="$FALLBACK_IMAGE"; recipe="$DOCKERFILE"; context=.' in code
    assert code.count("docker build ") == 1
    assert 'docker build -t "$tag" -f "$recipe" "$context"' in code
    assert 'docker build -t "$IMAGE"' not in code
    assert 'IMAGE="${SIM_IMAGE:-metadrive-wingfin-sim:latest}"' in code
