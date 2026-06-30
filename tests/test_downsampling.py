from __future__ import annotations

import numpy as np
import pandas as pd
from conftest import load_module

downsampling = load_module(
    "medium_to_long_backfill_test",
    "jobs/downsampling/medium_to_long_backfill.py",
)


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
