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

import pytest

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


def test_compose_builds_the_runner_from_the_sim_dockerfile():
    compose = (ROOT / "compose.yaml").read_text()
    assert "dockerfile: docker/Dockerfile" in compose
    assert "image: scenariobank-sim:latest" in compose
    assert "metadrive-wingfin-sim:latest" not in compose, "the converter's tag is not ours to build"
    studio = (ROOT / "docker" / "studio.Dockerfile").read_text()
    assert "FROM scenariobank-sim:latest" in studio


# --- the bridge image -------------------------------------------------------------------------


def test_bridge_context_is_complete():
    assert (BRIDGE / "Dockerfile").is_file()
    assert (BRIDGE / "bridge" / "zapeta" / "server.py").is_file()
    assert (FORK / "SConstruct").is_file(), "not vendored; bridge.sh build would need SSH"
    assert any((FORK / "cereal").iterdir()), "cereal/ is empty -- a submodule that did not vendor"
    assert (FORK / "VENDORED.md").is_file()


@pytest.mark.parametrize("path", VENDORED_SYMLINKS)
def test_vendored_fork_kept_its_symlinks(path: str):
    # A transport that flattens symlinks makes scons die on a missing SConscript, which reads
    # like a broken Dockerfile and is not.
    assert (FORK / path).is_symlink(), f"{path} is not a symlink; the tree was copied, not cloned"


def test_bridge_dockerfile_copies_what_the_context_holds():
    text = (BRIDGE / "Dockerfile").read_text()
    assert "COPY deps/openpilot/ " in text
    assert "COPY bridge/ /opt/bridge/" in text


def test_bridge_script_checks_the_same_symlinks_the_test_does():
    script = (ROOT / "scripts" / "bridge.sh").read_text()
    for path in VENDORED_SYMLINKS:
        assert path in script
