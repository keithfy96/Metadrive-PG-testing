# Camera rigs

A rig spec describes the cameras a vehicle really carries: where each one sits, which way it
looks, how big its frame is, and how often it is read. `scenariobank replay --camera-rig` mounts
one on the ego and reads it; `scenariobank rig --camera-rig` converts one and says where each
camera aims, and with `--check-frame` re-measures the vehicle frame the conversion rests on.

`av3.txt` is the AV3 model's own rig, six 512x384 cameras under the names the weights read them
by (`model_dev.yml`'s `camera_order`), at 0.05 s. It is the converter's file
(`wingfin-osm-scenarionet-converter/rigs/av3.txt`), copied verbatim on 2026-09-10 -- its header
records where the numbers came from and the two gaps that stay open: the four corner cameras are
fisheye lenses rendered as unwarped pinholes, and the frames are 4:3 to be squashed to 16:9 by the
model's preprocess rather than rendered 16:9 natively.

**The format is CARLA's and the conversion into MetaDrive's frame is not a rename** -- an x/y
swap and a sign flip on yaw, pitch passed through, roll refused. `src/scenariobank/av3/camera_rig.py`
carries the measurements it was derived from, and `scripts/av3-probe.sh` re-takes them.

**A spec's `tick_rate` has to equal the interval the cameras are read at**, the decision stride
over the step rate, and the loader refuses a mismatch rather than resampling. A road steps at
10 Hz, so `av3.txt` cannot be read at its own 20 Hz there until a run can step at 100 Hz
(Phase 4 Step 7's `--step-hz 100 --decision-hz 20`); `replay --ignore-rig-rate` mounts it anyway
for looking, and says so.

**Nine image buffers is a hard ceiling**, measured rather than read: past it `env.reset` fails
intermittently and the process aborts. A spec with more cameras is refused when it is read.
