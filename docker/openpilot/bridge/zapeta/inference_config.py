# Vendored eval snapshot of common/inference_config.py. The real one is
# per-environment and gitignored (generated from common/tmpl/inference_config.tmpl.py),
# so a fresh checkout wouldn't have it — the bridge mounts this tracked copy to
# /opt/project/common/inference_config.py instead. The fork reads o_flag directly;
# everything else goes through InferenceConfigParser defaults. Keep in sync with
# the template if the eval-relevant fields change.


class InferenceConfig:
    arbitrary_starting_point = False
    min_dist_next_target_point = 50
    target_point_recalc_dist = 1
    should_use_delta_position_lstm = False
    dataset_path = '/bags/aslan-20221102105604/dataset_carla.pickle'
    o_flag = True
    should_log_can_sends = True
    apply_gps_noise = False
    waypoints_shift = 0
    START_THETA = 2.956217345788387
    raise_lidar_m = 0.11
    logs_update = False
    wm_flag = True
    should_plot_speeds = False
    should_plot_lidar = True
    flag_tp_calc_normal = True
    filter_tp = True
    pitch_camera_warp_degrees = 0
    yaw_camera_warp_degrees = 0
    should_use_lstm_gps_interpolator = False
    USE_ANDREJIC_INTERPOLATOR = False
    model_speed_multiplier = 1
    host_model_speed_correction = 0.718
    tl_test_fix_tp = False
    do_traffic_light_speed_init = False
    traffic_light_speed_init_thr_ms = 1.0
    traffic_light_detection_threshold_logit = -0.85
    traffic_light_speed_init_value_ms = 5.0
    do_set_small_speeds_to_0 = False
    reduce_to_zero_power = 1.0
    small_speed_thr = 1.1
    small_4th_wp_dist_thr = 2.0
    control_node_static_delay_compensation = 0.0
    is_dynamic_control_node_delay_compensation = False
    do_use_stop_low_speed_to_zero_feature = False
    stop_low_speed_to_zero_thr_m_per_s = 1.0
    do_use_slow_down_during_turn_feature = False
    lateral_coordinate_thr = 2.0
    target_cap_speed = 2.0
    last_wp_dist_sqr_thr = 3.0
    speed_spoofing_enabled = False
    OP_steerRatio = 1.0
    # CARLA time-vs-space fix in lateral_planner.py: integrate the lat MPC at the
    # actual longitudinal v_ego (uniform, not the model's per-node |velocity|) and
    # resample the geometric path to arc-length s=v_ego*t; cancels the low-speed
    # over-curve.
    carla_uniform_vego_arclen = True
    # Lateral MPC heading-tracking weight (openpilot default 0.11). The MPC tracks
    # y + heading and INFERS curvature; the weak default under-commits vs the model
    # path. Heading is the right lever; closed-loop tuned, saturates at 2.0.
    lateral_motion_cost = 2.0
    speeds_up = [0.0, 5.0, 15.0]
    speeds_down = [0.0, 5.0, 15.0]
    single_limit_deg_per_s = 0.0
    # Lateral MPC steering-rate cost, lowered from the openpilot default 350 (which
    # is tuned for real EPS): CARLA sync-mode has no actuator delay, so a high cost
    # only lags turn-in and under-commits curvature.
    st_rate_cost = 0
    if single_limit_deg_per_s > 0.0:
        single_limit = single_limit_deg_per_s / 100.0
        angle_rates_up = [single_limit, single_limit, single_limit]
        angle_rates_down = [single_limit, single_limit, single_limit]
    else:
        sc_up = 2.5
        sc_down = 1.5
        angle_rates_up = [4.0 * sc_up, 3.0 * sc_up, 2.0 * sc_up]
        angle_rates_down = [6.0 * sc_down, 4.0 * sc_down, 3.0 * sc_down]
