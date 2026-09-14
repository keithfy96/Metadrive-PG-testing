"""The rig agent: one container per rig, one worker per card.

Phase 7. Nothing in here is imported by the simulator, the runner or the studio -- the
dependency runs one way, because the agent is the thing that *launches* a run and must keep
working when the run it launched has died. It imports `scenariobank.events` and no other module
of ours, for the same reason `events.py` imports none: a supervisor reads a run with
`scenariobank` installed and nothing else.

R1, the independence rule: wing-sim's `orchestrator/src/rig/` is the reference for every file
here and is imported by none of them. What the two projects share is a queue (a schema) and a
lock directory (a path); neither is a library.
"""
