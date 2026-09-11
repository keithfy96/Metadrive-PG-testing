"""`av3/openpilot_policy.py` -- the translation between a MetaDrive ego and the openpilot bridge.

Nothing here needs MetaDrive: the route arrives as a `PointLane`-shaped stand-in, an ego as
three numbers, and `StubBridge` is a real socket, so the whole path from a route to an action
is testable without an engine. Every assertion is about a **sign or a frame**, because those
are what fail silently: a flipped `y` steers the car neatly into the wrong side of the road, a
missing negation makes the drive look like a badly tuned controller, and MetaDrive would clip
or swallow all of it.
"""

from __future__ import annotations

import json
import math
import socket
import struct

import pytest
from test_av3_model import FakeAgent, FakeTrajectory

from scenariobank.av3.openpilot_policy import (
    BRIDGE_DT_S,
    DEFAULT_STEER_RATIO,
    HEADER_FMT,
    LONGITUDINAL_MODES,
    ROUTE_POINTS,
    ROUTE_SPACING_M,
    WAYPOINT_OFFSETS_S,
    BridgeConnection,
    BridgeError,
    OpenpilotDriver,
    StubBridge,
    bridge_ego,
    rate_note,
    recv_msg,
    route_points,
    sample_route,
    send_msg,
    to_metadrive_action,
    waypoints_from_route,
)

# -- the route, in the car's frame ------------------------------------------------------


def straight_route(points: int = ROUTE_POINTS, spacing: float = ROUTE_SPACING_M) -> dict:
    return route_points(FakeAgent(), FakeTrajectory(), points, spacing)


def curving_route(radius: float = 30.0, *, leftwards: bool = True) -> dict:
    sign = 1.0 if leftwards else -1.0
    return route_points(FakeAgent(), FakeTrajectory(sign / radius))


def test_route_points_are_ahead_and_left_in_metres_from_the_cars_own_projection():
    route = straight_route()
    assert route["frame"] == "ego_x_ahead_y_left"
    assert len(route["points_m"]) == ROUTE_POINTS
    assert route["points_m"][0] == pytest.approx([0.0, 0.0])
    assert route["points_m"][-1] == pytest.approx([(ROUTE_POINTS - 1) * ROUTE_SPACING_M, 0.0])
    assert route["spacing_m"] == ROUTE_SPACING_M
    assert route["remaining_m"] == pytest.approx(200.0)
    left = curving_route(leftwards=True)["points_m"]
    assert all(y > 0 for _, y in left[1:]), "a left bend is +y in MetaDrive's frame"
    with pytest.raises(BridgeError, match="route"):
        route_points(FakeAgent(), None)


def test_route_points_clamp_at_the_end_of_the_route():
    route = route_points(FakeAgent(), FakeTrajectory(length=10.0), 8, 2.0)
    aheads = [x for x, _ in route["points_m"]]
    assert aheads == pytest.approx([0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 10.0, 10.0])


# -- the wire ---------------------------------------------------------------------------


def test_the_framing_matches_the_bridges_own_encoder():
    """A 4-byte big-endian length then UTF-8 JSON, byte for byte: `bridge_protocol.py`."""
    message = {"type": "step", "waypoints": [[1.0, -2.0, 0.5]], "ego": {"v_ego": 8.4}}
    payload = json.dumps(message).encode("utf-8")
    theirs = struct.pack(HEADER_FMT, len(payload)) + payload
    left, right = socket.socketpair()
    try:
        left.sendall(theirs)
        assert recv_msg(right) == message
        send_msg(right, message)
        assert left.recv(4) == struct.pack(HEADER_FMT, len(payload))
    finally:
        left.close()
        right.close()


def test_a_closed_connection_is_an_error_rather_than_an_empty_message():
    left, right = socket.socketpair()
    left.close()
    with pytest.raises(ConnectionError):
        recv_msg(right)
    right.close()


def test_an_unreachable_bridge_is_named_with_the_script_that_starts_it():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    connection = BridgeConnection("127.0.0.1", port, connect_timeout=1.0)
    with pytest.raises(BridgeError) as raised:
        connection.connect()
    assert f"127.0.0.1:{port}" in str(raised.value) and "bridge.sh" in str(raised.value)
    with pytest.raises(BridgeError, match="before connect"):
        connection.step({"type": "step"})


# -- the route becomes waypoints --------------------------------------------------------


def test_sampling_interpolates_between_the_route_points_and_holds_the_destination():
    ahead, left = sample_route([[0.0, 0.0], [2.0, 1.0], [4.0, 0.0]], 2.0, 1.0)
    assert (ahead, left) == pytest.approx((1.0, 0.5))
    route = straight_route(points=5)
    assert sample_route(route["points_m"], ROUTE_SPACING_M, 1000.0) == route["points_m"][-1]
    with pytest.raises(BridgeError):
        sample_route([], 2.0, 1.0)


def test_a_straight_route_asks_for_no_sideways_movement():
    waypoints = waypoints_from_route(straight_route(), speed_mps=10.0)
    assert len(waypoints) == len(WAYPOINT_OFFSETS_S)
    for x, y, t in waypoints:
        assert y == pytest.approx(0.0, abs=1e-9) and x == pytest.approx(10.0 * t)


def test_a_left_hand_bend_gives_negative_y_because_the_bridge_is_right_positive():
    """The one assertion that catches a flipped frame: MetaDrive's ego frame is y LEFT, the
    bridge takes CARLA's y RIGHT, so a route bending left arrives with negative `y`."""
    assert all(y < 0.0 for _, y, _ in waypoints_from_route(curving_route(), 10.0))
    assert all(y > 0.0 for _, y, _ in waypoints_from_route(curving_route(leftwards=False), 10.0))


def test_a_stopped_car_still_gets_waypoints_ahead_of_it():
    waypoints = waypoints_from_route(straight_route(), speed_mps=0.0)
    assert waypoints[-1][0] > 5.0
    assert all(b[0] > a[0] for a, b in zip(waypoints, waypoints[1:], strict=False))


# -- the ego state and the reply --------------------------------------------------------


def test_the_column_angle_is_negated_on_the_way_to_the_bridge():
    state = bridge_ego(8.0, 0.1, steering=0.5, max_steering_deg=40.0, steer_ratio=12.0)
    assert state["steering_angle_deg"] == pytest.approx(-0.5 * 40.0 * 12.0)
    assert state["v_ego"] == pytest.approx(8.0) and state["yaw_rate"] == pytest.approx(0.1)
    assert DEFAULT_STEER_RATIO == 12.0, "what wing-sim's own config sends"


def test_the_reply_is_negated_and_accel_is_the_default_longitudinal():
    reply = {"type": "control", "steer": 0.25, "throttle": 0.4, "brake": 0.0, "accel_cmd": -1.0}
    assert LONGITUDINAL_MODES[0] == "accel"
    assert to_metadrive_action(reply) == pytest.approx([-0.25, -1.0 / 3.48])
    assert to_metadrive_action(reply, "pedal") == pytest.approx([-0.25, 0.4])
    braking = {"type": "control", "steer": 0.0, "throttle": 0.0, "brake": 0.6}
    assert to_metadrive_action(braking, "pedal")[1] == pytest.approx(-0.6)


def test_accel_mode_normalises_each_direction_by_its_own_end_of_the_envelope():
    assert to_metadrive_action({"steer": 0.0, "accel_cmd": 2.0})[1] == pytest.approx(1.0)
    assert to_metadrive_action({"steer": 0.0, "accel_cmd": -3.48})[1] == pytest.approx(-1.0)
    assert to_metadrive_action({"steer": 0.0, "accel_cmd": 4.0})[1] == pytest.approx(1.0)


def test_accel_mode_refuses_a_reply_with_no_acceleration_and_a_nan():
    with pytest.raises(BridgeError, match="accel_cmd"):
        to_metadrive_action({"steer": 0.0, "throttle": 0.3, "brake": 0.0})
    with pytest.raises(BridgeError, match="accel_cmd"):
        to_metadrive_action({"steer": 0.0, "accel_cmd": float("nan")})
    with pytest.raises(BridgeError, match="longitudinal"):
        to_metadrive_action({"steer": 0.0, "accel_cmd": 0.0}, "table")


@pytest.mark.parametrize(
    "reply",
    [
        {"type": "control", "steer": float("nan"), "accel_cmd": 0.0},
        {"type": "control", "steer": 1.5, "accel_cmd": 0.0},
        {"type": "control", "steer": 0.0, "throttle": float("inf"), "brake": 0.0},
        {"type": "error", "reason": "no planners"},
        {"type": "control"},
        "steer",
    ],
)
def test_everything_metadrive_would_have_swallowed_is_refused_here(reply):
    mode = "pedal" if isinstance(reply, dict) and "throttle" in reply else "accel"
    with pytest.raises(BridgeError):
        to_metadrive_action(reply, mode)


def test_the_rate_note_names_the_bridges_tick_and_the_clock_that_matches_it():
    assert rate_note(BRIDGE_DT_S) is None
    note = rate_note(0.1)
    assert note is not None and "_DT_MDL" in note and "2.0x" in note and "--step-hz 100" in note


# -- end to end, against a real socket ---------------------------------------------------


@pytest.fixture
def stub():
    bridge = StubBridge()
    yield bridge
    bridge.close()


def driver_for(stub: StubBridge, **kwargs) -> OpenpilotDriver:
    driver = OpenpilotDriver(stub.host, stub.port, **kwargs)
    driver.episode(max_steering_deg=40.0, wheelbase_m=2.47)
    return driver


def test_the_whole_path_steers_left_for_a_left_hand_bend(stub):
    """Route in, action out, over a real socket -- and the two negations cancel."""
    driver = driver_for(stub, target_speed_mps=10.0)
    try:
        steering, throttle_brake = driver.act(8.0, 0.0, route=curving_route(leftwards=True))
        assert steering > 0.05, "a left bend must give left (positive) steering in MetaDrive"
        assert throttle_brake > 0.0, "below the target speed, so it should be accelerating"
        assert driver.act(8.0, 0.0, route=curving_route(leftwards=False))[0] < -0.05
        assert driver.calls == 2 and stub.steps == 2
        assert stub.inits[0]["n_waypoints"] == 4 and stub.inits[0]["max_steer_angle"] == 40.0
    finally:
        driver.close()


def test_a_straight_route_is_driven_straight_and_over_the_target_it_brakes(stub):
    driver = driver_for(stub, target_speed_mps=4.0)
    try:
        steering, throttle_brake = driver.act(14.0, 0.0, route=straight_route())
        assert steering == pytest.approx(0.0, abs=1e-9)
        assert throttle_brake < 0.0, "MetaDrive brakes below zero; [0, 1] cannot brake at all"
    finally:
        driver.close()


def test_a_models_waypoints_go_through_as_given_with_the_modelv2_rows_beside_them(stub):
    """Conversion 6: the model's y is already the bridge's RIGHT, so nothing is flipped on the
    way in, and a right-bending prediction comes back as right (negative) steering."""
    driver = driver_for(stub, target_speed_mps=10.0)
    try:
        rightwards = [[5.0 * t, 1.0 * t, t] for t in (0.5, 1.0, 1.5, 2.0)]
        rows = [[x, y, t, 0.0, 0.0, 5.0, 0.0, 0.0, 0.0] for x, y, t in rightwards]
        steering, _ = driver.act(5.0, 0.0, waypoints=rightwards, modelv2=rows)
        assert steering < -0.05
        with pytest.raises(BridgeError, match="neither"):
            driver.act(5.0, 0.0)
    finally:
        driver.close()


def test_an_empty_waypoint_list_is_a_hard_stop_and_act_before_episode_is_refused(stub):
    driver = OpenpilotDriver(stub.host, stub.port)
    with pytest.raises(BridgeError, match="before episode"):
        driver.act(5.0, 0.0, route=straight_route())
    driver.episode(40.0, 2.47, n_waypoints=20)
    try:
        assert stub.inits[-1]["n_waypoints"] == 20, "the model's count, sent at init"
    finally:
        driver.close()
    # What the bridge itself does with an empty list (`server.py:_handle_step`): a hard stop.
    stopped = StubBridge.control({"waypoints": [], "ego": {"v_ego": 5.0}}, {})
    assert stopped["brake"] == 1.0 and to_metadrive_action(stopped)[1] == pytest.approx(-1.0)


def test_a_new_episode_is_a_new_connection_and_close_is_safe_twice(stub):
    driver = driver_for(stub)
    driver.episode(40.0, 2.47)
    assert len(stub.inits) == 2
    driver.close()
    driver.close()
    assert not driver.bridge.connected


def test_the_stubs_geometry_is_pure_pursuit_in_the_bridges_conventions():
    settings = {"max_steer_angle": 40.0, "wheelbase_m": 2.5}
    right = StubBridge.control(
        {"waypoints": [[8.0, 2.0, 1.0]], "ego": {"v_ego": 5.0}, "target_speed": 10.0}, settings
    )
    assert right["steer"] > 0.0, "a waypoint to the RIGHT is a positive (CARLA) steer"
    assert right["curvature"] < 0.0, "and a negative (left-positive) curvature"
    assert right["accel_cmd"] > 0.0 and right["throttle"] > 0.0 and right["brake"] == 0.0
    assert math.isfinite(right["lookahead_m"])
