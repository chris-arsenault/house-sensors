from __future__ import annotations

import numpy as np
import pandas as pd
from conftest import load_module

downsampling = load_module(
    "medium_to_long_backfill_test",
    "jobs/downsampling/medium_to_long_backfill.py",
)
raw_downsampling = load_module(
    "raw_to_medium_test",
    "jobs/downsampling/raw_to_medium.py",
)


def _raw_config(tmp_path):
    params = list(raw_downsampling.DEFAULT_PARAMS)
    return raw_downsampling.Config(
        influx_url="http://influx.example",
        influx_token="token",
        influx_org="ahara",
        raw_power_bucket="voltage-data",
        raw_env_bucket="environment-data",
        medium_bucket="sensors-medium",
        medium_measurement="sensors",
        power_measurement="voltage_monitoring",
        env_measurement="environment",
        params=params,
        power_fields=list(raw_downsampling.DEFAULT_POWER_FIELDS),
        env_fields=list(raw_downsampling.DEFAULT_ENV_FIELDS),
        env_field_map=dict(raw_downsampling.DEFAULT_ENV_FIELD_MAP),
        start_iso=None,
        end_iso=None,
        days_back=1,
        chunk_minutes=60,
        dry_run=True,
        state_file=tmp_path / "raw-to-medium-state.json",
        interval_seconds=60,
        delay_seconds=60,
        overlap_minutes=5,
        quantile=0.95,
        alpha=0.2,
        target_anomaly_rate=0.05,
        rate_beta=0.25,
        learn_thresholds=True,
        learn_incremental=False,
        ensure_medium_bucket=False,
        medium_retention_seconds=0,
        abs_bounds=raw_downsampling._default_abs_bounds(params),
        initial_minute_thresholds=raw_downsampling._default_minute_thresholds(params),
    )


def _install_fake_point(monkeypatch):
    created = []

    class FakePoint:
        def __init__(self, measurement):
            self.measurement = measurement
            self.timestamp = None
            self.tags = {}
            self.fields = {}
            created.append(self)

        def time(self, timestamp):
            self.timestamp = timestamp
            return self

        def tag(self, key, value):
            self.tags[key] = value
            return self

        def field(self, key, value):
            self.fields[key] = value
            return self

    monkeypatch.setattr(raw_downsampling, "Point", FakePoint)
    return created


def test_oscillation_count_ignores_flat_steps():
    assert downsampling._oscillation_count(np.array([1.0, 2.0, 2.0, 1.0, 3.0, 2.0])) == 3


def test_hour_is_anomaly_checks_bounds_thresholds_and_oscillation():
    abs_bounds = {"voltage": {"min": 110.0, "max": 125.0}}
    thresholds = {"voltage": {"spreadT": 3.0, "stdT": 1.0, "oscT": 4}}

    assert downsampling.hour_is_anomaly("voltage", 109.9, 120.0, 1.0, 0.2, 0, abs_bounds, thresholds)
    assert downsampling.hour_is_anomaly("voltage", 118.0, 124.0, 6.0, 0.2, 0, abs_bounds, thresholds)
    assert downsampling.hour_is_anomaly("voltage", 118.0, 120.0, 1.0, 0.2, 4, abs_bounds, thresholds)
    assert not downsampling.hour_is_anomaly("voltage", 118.0, 120.0, 1.0, 0.2, 1, abs_bounds, thresholds)


def test_update_hour_thresholds_learns_quantiles_and_rate_feedback():
    observations = pd.DataFrame(
        [
            {"_field": "voltage", "hour_min": 119.0, "hour_max": 120.0, "spread": 1.0, "std": 0.1, "osc": 1, "is_anom": 0},
            {"_field": "voltage", "hour_min": 118.0, "hour_max": 121.0, "spread": 3.0, "std": 0.3, "osc": 3, "is_anom": 1},
        ]
    )

    updated = downsampling.update_hour_thresholds(
        observations,
        {"voltage": {"min": 100.0, "max": 130.0}},
        {"voltage": {"spreadT": None, "stdT": None, "oscT": 6}},
        quantile=0.5,
        alpha=1.0,
        target_rate=0.25,
        rate_beta=0.25,
    )

    assert updated["voltage"]["spreadT"] == 2.125
    assert updated["voltage"]["stdT"] == 0.21250000000000002
    assert updated["voltage"]["oscT"] == 2


def test_minute_is_anomaly_checks_bounds_thresholds_and_oscillation():
    abs_bounds = {"voltage": {"min": 110.0, "max": 125.0}}
    thresholds = {"voltage": {"spreadT": 3.0, "stdT": 1.0, "oscT": 4}}

    assert raw_downsampling.minute_is_anomaly("voltage", 109.9, 120.0, 1.0, 0.2, 0, abs_bounds, thresholds)
    assert raw_downsampling.minute_is_anomaly("voltage", 118.0, 124.0, 6.0, 0.2, 0, abs_bounds, thresholds)
    assert raw_downsampling.minute_is_anomaly("voltage", 118.0, 120.0, 1.0, 0.2, 4, abs_bounds, thresholds)
    assert not raw_downsampling.minute_is_anomaly("voltage", 118.0, 120.0, 1.0, 0.2, 1, abs_bounds, thresholds)


def test_update_minute_thresholds_learns_quantiles_and_rate_feedback():
    observations = pd.DataFrame(
        [
            {"_field": "voltage", "minute_min": 119.0, "minute_max": 120.0, "spread": 1.0, "std": 0.1, "osc": 1, "is_anom": 0},
            {"_field": "voltage", "minute_min": 118.0, "minute_max": 121.0, "spread": 3.0, "std": 0.3, "osc": 3, "is_anom": 1},
        ]
    )

    updated = raw_downsampling.update_minute_thresholds(
        observations,
        {"voltage": {"min": 100.0, "max": 130.0}},
        {"voltage": {"spreadT": None, "stdT": None, "oscT": 6}},
        quantile=0.5,
        alpha=1.0,
        target_rate=0.25,
        rate_beta=0.25,
    )

    assert updated["voltage"]["spreadT"] == 2.125
    assert updated["voltage"]["stdT"] == 0.21250000000000002
    assert updated["voltage"]["oscT"] == 2


def test_build_medium_points_aggregates_env_and_preserves_voltage_anomalies(tmp_path, monkeypatch):
    points = _install_fake_point(monkeypatch)
    config = _raw_config(tmp_path)
    abs_bounds = raw_downsampling._default_abs_bounds(config.params)
    thresholds = raw_downsampling._default_minute_thresholds(config.params)
    thresholds["humidity"] = {"spreadT": 5.0, "stdT": 5.0, "oscT": 8}
    thresholds["voltage"] = {"spreadT": 2.0, "stdT": 10.0, "oscT": 8}

    env_frame = pd.DataFrame(
        [
            {
                "_time": pd.Timestamp("2026-06-30T00:00:01Z"),
                "_value": 40.0,
                "_field": "humidity",
                "device_name": None,
                "device_id": "env-1",
                "location": "office",
            },
            {
                "_time": pd.Timestamp("2026-06-30T00:00:30Z"),
                "_value": 40.1,
                "_field": "humidity",
                "device_name": None,
                "device_id": "env-1",
                "location": "office",
            },
        ]
    )
    power_frame = pd.DataFrame(
        [
            {
                "_time": pd.Timestamp("2026-06-30T00:00:01Z"),
                "_value": 120.0,
                "_field": "voltage",
                "device_name": "Chris Office Smart Plug",
                "device_id": None,
                "location": None,
            },
            {
                "_time": pd.Timestamp("2026-06-30T00:00:20Z"),
                "_value": 110.0,
                "_field": "voltage",
                "device_name": "Chris Office Smart Plug",
                "device_id": None,
                "location": None,
            },
            {
                "_time": pd.Timestamp("2026-06-30T00:00:40Z"),
                "_value": 130.0,
                "_field": "voltage",
                "device_name": "Chris Office Smart Plug",
                "device_id": None,
                "location": None,
            },
        ]
    )

    env_points, _, env_stats = raw_downsampling.build_medium_points(config, env_frame, False, abs_bounds, thresholds)
    power_points, _, power_stats = raw_downsampling.build_medium_points(config, power_frame, True, abs_bounds, thresholds)

    assert env_stats == {"series": 1, "anomalies": 0}
    assert len(env_points) == 4
    assert {point.tags["stat"] for point in env_points} == {"min", "max", "mean", "computed"}
    assert {point.tags["resolution"] for point in env_points} == {"1m"}
    assert {point.tags["domain"] for point in env_points} == {"environment"}
    assert {point.tags["sensor_location"] for point in env_points} == {"office"}
    assert {point.fields["humidity"] for point in env_points} == {40.0, 40.05, 40.1}

    assert power_stats == {"series": 1, "anomalies": 1}
    assert len(power_points) == 6
    assert {point.tags["stat"] for point in power_points} == {"raw", "computed"}
    assert {point.tags["resolution"] for point in power_points} == {"1s"}
    assert {point.tags["domain"] for point in power_points} == {"power"}
    assert {point.tags["sensor_id"] for point in power_points} == {"CHRIS_OFFICE_SMART_PLUG"}
    assert len(points) == 10
