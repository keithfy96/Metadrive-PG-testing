"""The command reference is generated, so the only thing to test is that it has not fallen behind.

No `needs_sim`: neither `cli` nor `docs` imports MetaDrive, so this guard runs on every machine
rather than only where the simulator is installed. That is the point of generating the page.
"""

from pathlib import Path

import pytest

from scenariobank.categories import CATEGORIES, VALID_BLOCK_IDS, ExitRule
from scenariobank.docs import EXAMPLES, GROUPS, render_commands

REFERENCE = Path("docs/reference/commands.md")


def test_the_checked_in_reference_matches_the_cli():
    # The whole guarantee. If this fails, a flag changed and the page did not.
    assert REFERENCE.read_text() == render_commands(), (
        f"{REFERENCE} is out of date with the CLI. Run: uv run scenariobank commands"
    )


def test_every_registered_command_is_placed_in_a_group():
    # `render_commands` raises rather than quietly omitting a command from the page, so adding one
    # to cli.py without grouping it is an error you get told about.
    import typer

    from scenariobank.cli import app

    grouped = [name for _, _, names in GROUPS for name in names]
    assert sorted(grouped) == sorted(typer.main.get_command(app).commands)
    assert len(grouped) == len(set(grouped)), "a command is listed in two groups"


def test_the_reference_lists_every_category_and_every_rule():
    # The values you can actually pass, which is what a flag table alone does not tell you.
    rendered = render_commands()
    for name in CATEGORIES:
        assert f"`{name}`" in rendered
    for rule in ExitRule:
        assert f"`{rule.value}`" in rendered
    for block in VALID_BLOCK_IDS:
        assert f"`{block}`" in rendered


def test_the_reference_answers_the_change_one_seed_question():
    # The question that prompted the page: it must be answerable without reading a flag table.
    rendered = render_commands()
    assert "## How do I..." in rendered
    assert "change one seed in a bank I already built" in rendered
    assert "Changing one scenario does not mean rebuilding the bank" in rendered


def test_rendering_is_stable():
    assert render_commands() == render_commands()


@pytest.mark.parametrize("noise", ["--install-completion", "--show-completion"])
def test_typers_own_flags_are_left_out(noise):
    # They are identical on every Typer program and say nothing about this one.
    assert noise not in render_commands()


def _section(rendered: str, command: str) -> str:
    """The text of one command's section, up to the next heading."""
    start = rendered.index(f"### `{command}`")
    rest = rendered[start + 1 :]
    end = min(
        (i for i in (rest.find("\n### "), rest.find("\n## ")) if i != -1),
        default=len(rest),
    )
    return rest[:end]


@pytest.mark.parametrize("command", sorted(EXAMPLES))
def test_every_command_shows_its_examples_in_its_own_section(command):
    # Examples used to be collected at the bottom of the page, too far from the flags they
    # demonstrate to be followed. Each command's section now carries its own.
    section = _section(render_commands(), command)
    assert "```bash" in section, f"{command} has no example beside its flags"
    assert f"scenariobank {command}" in section


def test_a_command_without_examples_is_an_error_not_a_gap():
    import typer

    from scenariobank import docs
    from scenariobank.cli import app

    registered = typer.main.get_command(app).commands
    # Gather a real command under a name EXAMPLES does not know: that is what adding a command to
    # cli.py and forgetting its examples looks like, and it must stop the page being written.
    with pytest.raises(ValueError, match="no examples"):
        docs._command_entry("not_a_command", registered["doctor"], {})
    assert sorted(EXAMPLES) == sorted(registered)


def test_a_flags_own_row_lists_the_values_it_accepts():
    # The values belong in the row for the flag, not only in a table further down the page:
    # knowing a flag exists is no use without knowing what may follow it.
    rendered = render_commands()
    rule_row = next(line for line in rendered.splitlines() if line.startswith("| `--rule"))
    for rule in ExitRule:
        assert f"`{rule.value}`" in rule_row

    category_rows = [line for line in rendered.splitlines() if line.startswith("| `--category")]
    assert category_rows
    for row in category_rows:
        for name in CATEGORIES:
            assert f"`{name}`" in row

    block_rows = [line for line in rendered.splitlines() if line.startswith("| `--block-seq")]
    assert block_rows
    for row in block_rows:
        for block in VALID_BLOCK_IDS:
            assert f"`{block}`" in row
