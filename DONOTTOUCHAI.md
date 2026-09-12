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


run replay with 2d visualisation

uv run scenariobank run --bank banks/curve --tier easy \                                                            INT ✘  00:50:07 
    --policy scenariobank.policies:ExpertPolicy --out film --record-video
  xdg-open film/videos/curve_0003.mp4


film a drive 3d

  uv run scenariobank run --bank banks/curve --tier hard --policy scenariobank.policies:ExpertPolicy \
    --out film --camera-rig rigs/av3.txt --ignore-rig-rate --record-video   



Using the actual model

bash scripts/bridge.sh build
bash scripts/bridge.sh start      # starts it; prints "already up" if it is
bash scripts/bridge.sh status     # confirms it is listening on 127.0.0.1:5558
bash scripts/bridge.sh logs       # one line per control tick; Ctrl-C leaves it running
bash scripts/bridge.sh stop       # only when you are done for the day

bash scripts/sim-image.sh build

docker rm av3 2>/dev/null         # a finished run still holds the name
docker run -d --name av3 --gpus all --network host \
  -v $PWD:/work:ro -v $PWD/../models:/models:ro -v $PWD/out:/out \
  -e HOME=/tmp -e MPLCONFIGDIR=/tmp/matplotlib \
  metadrive-wingfin-sim:latest \
  python -m scenariobank run \
    --bank /work/banks/t-junction --scenarios t_junction_0000 \
    --policy scenariobank.av3:AV3Policy --camera-rig /work/rigs/av3.txt \
    --step-hz 100 --decision-hz 20 \
    --model-config /models/model_dev.yml \
    --checkpoint /models/step_440000_trt_direct_full.ep \
    --out /out/av3-film --record-video --heartbeat 10
docker logs -f av3                # heartbeat every 10 s; Ctrl-C leaves the run going