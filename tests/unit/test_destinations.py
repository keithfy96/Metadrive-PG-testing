"""How the destinations reference reports its progress.

The document itself is measured on the simulator and checked in; what is testable without one is
the counting. Twenty seconds of silence is what this command looked like from the studio, which
cannot tell a slow measurement from a hung one -- so it says where it is, in the same `[n/m]`
lines the build bar already reads. No second progress protocol.
"""

from __future__ import annotations

import re

from scenariobank import destinations
from scenariobank.categories import CATEGORIES


def test_every_category_reports_as_it_is_measured(monkeypatch):
    monkeypatch.setattr(destinations, "survey", lambda category, seeds: [])
    said: list[str] = []
    destinations.build_rows((0,), progress=said.append)
    assert said == [f"category {name}" for name in CATEGORIES]


def test_the_count_runs_across_both_passes(monkeypatch, tmp_path):
    """One count, not two bars. The roads are fingerprinted first and the categories driven
    second, and a bar that refilled halfway would read as a job starting over."""
    sequences = sorted({category.block_seq for category in CATEGORIES.values()})

    def variety(seeds, *, progress=None):
        for block_seq in sequences:
            progress(f"road {block_seq}")
        return {}, {}

    def rows(seeds, *, progress=None):
        for name in CATEGORIES:
            progress(f"category {name}")
        return {}

    monkeypatch.setattr(destinations, "measure_road_variety", variety)
    monkeypatch.setattr(destinations, "build_rows", rows)
    monkeypatch.setattr(destinations, "render", lambda *args, **kwargs: "")

    said: list[str] = []
    destinations.write(tmp_path / "destinations.md", (0,), progress=said.append)

    total = len(sequences) + len(CATEGORIES)
    assert len(said) == total
    assert said[0] == f"[1/{total}] road {sequences[0]}"
    assert said[-1] == f"[{total}/{total}] category {list(CATEGORIES)[-1]}"
    # Continuous, and in the shape `web/static/index.html`'s `COUNTED` matches.
    counted = re.compile(r"^\[\s*(\d+)/(\d+)\]\s+\S")
    assert [int(counted.match(line).group(1)) for line in said] == list(range(1, total + 1))
