possibly need to change the destinations in the ftureu when i want to generate specific routes



How replay works

  replay drives one scenario and prints a report instead of writing a result. It goes through the same env builder and the same step loop as run, so what it measures is what run would measure. It issues a zero
  action every step, so nothing steers. It writes no file and loads no policy. It is the tool for checking that a bank drives at all, and it is headless too.

  Run it on one scenario:

  uv run scenariobank replay --bank banks/t-junction-left-intersection --scenario t_junction_0001

  The report shows the seed, the destination read back off the navigation, how many steps it drove, what ended the episode, the observation width at both ends, and the wall time.

  Two useful variants:

  uv run scenariobank replay --bank banks/junction-1 --steps 50
  uv run scenariobank replay --bank banks/t-junction --scenario t_junction_0000 --decision-hz 5 --json

  The first caps a recorded scenario at 50 steps so you get the round trip without the full 11 s. The second holds each action for two steps and prints the report as JSON, which shows that the action count
  halves while the episode length does not.

  Neither replay nor run opens a window, so there is nothing to watch yet. If you want a visual check, the smallest addition is a --render flag on replay that opens MetaDrive's own window for one scenario. That
  is a small step I can add to the plan.

