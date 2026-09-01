"""`python -m scenariobank` -- the same CLI the console script runs.

The studio launches every command as `sys.executable -m scenariobank ...`, which works from any
working directory and without the console script being on `PATH`. It is the same `app`, so a job
run from the page and a command typed into a terminal are the same program.
"""

from scenariobank.cli import app

app()
