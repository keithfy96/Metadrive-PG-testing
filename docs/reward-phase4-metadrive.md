# reward-phase4-metadrive

What the `reward` field in a Phase 4 result is, where the number comes from, and why it is
stored but never used to judge a run.

**Short version.** `reward` is MetaDrive's own reinforcement-learning shaping reward, at
MetaDrive's default weights, summed over every physics step of the episode. We do not write a
reward of our own. The one thing we add is a penalty for hitting a person, because MetaDrive has
none. The number is a training signal, not a score: a run passes or fails on `success`,
`route_completion`, `collisions` and `failure_reason`.

## Where the number is made

`scenariobank.runner` calls `env.step(action)` once per physics step and adds whatever MetaDrive
returns (`runner.py`, the one `= env.step(` site). The row's `reward` is that sum, rounded to six
places, and it is written to `results.json` next to `cost`, which is summed the same way.

MetaDrive computes the per-step value in its env class. A generated road uses `MetaDriveEnv`
(`metadrive/envs/metadrive_env.py`); a recorded scenario from the converter uses `ScenarioEnv`
(`metadrive/envs/scenario_env.py`). The two functions differ, so a PG row and an imported row are
not comparable on this field even at the same step rate.

## Generated roads: `MetaDriveEnv.reward_function`

Every physics step:

```
reward  = driving_reward × (metres advanced along the current lane this step)
        + speed_reward   × (speed_km_h / max_speed_km_h)
```

with the defaults

| key | default | meaning |
|---|---|---|
| `driving_reward` | 1.0 | one point per metre of forward progress along the lane |
| `speed_reward` | 0.1 | up to 0.1 per step at top speed; the top speed is 80 km/h for `DefaultVehicle` |
| `use_lateral_reward` | False | lateral position in the lane is ignored |

Both terms are multiplied by −1 when the car is on a road that runs against its route, so
driving the wrong way scores negative progress.

If the step ends with an event, the step's reward is **replaced**, not added to, in this order:

| event | reward for that step |
|---|---|
| arrived at the destination | +10 (`success_reward`) |
| left the road | −5 (`out_of_road_penalty`) |
| hit a vehicle | −5 (`crash_vehicle_penalty`) |
| hit an object (cone, barrier) | −5 (`crash_object_penalty`) |
| hit a person | −5 (**ours**, `crash_human_penalty`, see below) |
| touched a sidewalk | 0 (`crash_sidewalk_penalty`) |

Every one of these except the sidewalk also ends the episode, so the replacement happens once
per row at most.

**Our one change.** MetaDrive ends the episode on `crash_human` but scores it nothing. Our env
subclass in `scenariobank.env` (`ScenarioBankEnv.reward_function`) wraps MetaDrive's function
and substitutes −5 when a person was hit and none of the higher-ranked events happened in the
same step. The matching `cost_function` adds a cost of 1.0 for the same event, mirroring what
MetaDrive does for vehicles and objects. Both values live in `env.py` as `CRASH_HUMAN_PENALTY`
and `CRASH_HUMAN_COST`.

## Recorded scenarios: `ScenarioEnv.reward_function`

A different formula with different weights, kept here so the two are not confused:

```
reward  = driving_reward × (metres advanced along the reference trajectory)
        − lateral_penalty × (|lateral offset| / max_lateral_dist)       # 0.5, over 4 m
        − heading_penalty × (|heading error| / π)                         # 1.0
        − steering_range_penalty × (steering beyond 1/speed)             # 0.5
        clipped at 0 from below (no_negative_reward=True)
```

Crashes replace the step's reward with −1 (vehicle, object, person), a lane line or sidewalk with
−1, arrival with +5, leaving the road with −5. There is no speed term. We change nothing here.

## Why it is not a score

- **It rewards behaviour that a test does not.** Creeping forward earns points; being fast earns
  points. A car that drives 50 m and stops scores more than one that stops at once, and neither
  passed the scenario.
- **It changes with the step rate.** The speed term is added on every physics step. A drive at
  `--step-hz 100` collects ten times the speed reward of the same drive at the default 10 Hz.
  The progress term does not change, since metres are metres. So the field is comparable only
  between runs at the same `step_hz`, and never between a PG row and a recorded row.
- **The weights are MetaDrive's.** They were tuned to train MetaDrive's example agents. Nothing in
  this project chose them, and Phase 4b's calibration does not touch them.

What it is good for is a cheap diff. Two runs of the same scenario with the same actions produce
the same reward to six places, so it is one of the fields the reproducibility check compares.

## A worked example, from Phase 4 Step 8

The two scored AV3 runs of `t_junction_0000` on the rig, 100 Hz physics, 3200 steps:

| run | reward | route completion | what the car did |
|---|---|---|---|
| 1 | 3.63 | 6.69 % | crept about 1.9 m in the first 20 s, then held the brake |
| 2 | 3.50 | 6.61 % | the same, slightly less far |

Roughly 1.9 of each number is the progress term (one point per metre). The rest is the speed
term: a few hundred steps at 0.1 to 1 m/s against a top speed of 80 km/h, 0.1 point per step
at full speed, adds up to about 1.5. The 0.13 between the two runs is the policy not repeating
itself, which is what that step measured.

## Where to read the code

- `src/scenariobank/runner.py`: the sum, and the `reward` field's docstring saying it is not a score.
- `src/scenariobank/env.py`: `CRASH_HUMAN_PENALTY`, `CRASH_HUMAN_COST`, and the two overrides
  in `ScenarioBankEnv`.
- `metadrive/envs/metadrive_env.py`: `reward_function`, `cost_function`, and the default weights
  near the top of the file.
- `metadrive/envs/scenario_env.py`: the same two for recorded scenarios.
- `metadrive/component/pg_space.py`: `max_speed_km_h` for each vehicle type.
