# scenariobank

A bank of procedurally generated MetaDrive scenarios with fixed seeds, forced destinations, and
run-time difficulty options — plus the runner that scores a camera model against it.

`IMPLEMENTATION_PLAN.md` is the source of truth for what gets built and in what order.
`CONTRACT.md` (Phase 6) is what the bank promises its consumers.

```bash
uv sync --group sim
uv run scenariobank doctor
```

`doctor` is the first thing to run on any machine or in any container: it names the exact simulator
you are on. If it cannot resolve a commit SHA, stop — every later phase is built on a simulator you
cannot identify.
