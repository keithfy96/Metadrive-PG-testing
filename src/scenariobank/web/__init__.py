"""The local authoring studio: a page in front of the commands, not a second implementation.

Phase 2c. Everything here is localhost-only and everything that touches the simulator is a
subprocess of `scenariobank` itself -- see `api.create_app` for why.
"""

#: Where the studio keeps job logs, scratch figures and the results index. Gitignored: a job is
#: re-runnable and the index is rebuilt from the tree, so nothing here is worth keeping. Named
#: in this file rather than in `api.py` because `cli.results` needs it on a machine with no web
#: group installed -- a rig -- and `api.py` imports FastAPI at the top. Found on the rig,
#: 2026-09-15, by the first `scenariobank results` run in the agent image.
STATE_DIR_NAME = ".studio"
