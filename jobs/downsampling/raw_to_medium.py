#!/usr/bin/env python3
"""Raw-to-medium sensor downsampling job.

This direct Python process reads raw environment and power buckets, writes the
normalized medium schema, preserves anomalous seconds, and stores learned
minute thresholds plus an incremental watermark in a local JSON state file.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import logging
import os
import re
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from app_telemetry import telemetry_from_env
from dateutil.tz import tzutc

try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:  # pragma: no cover - runtime image installs this dependency.
    InfluxDBClient = None  # type: ignore[assignment]
    Point = None  # type: ignore[assignment]
    SYNCHRONOUS = None  # type: ignore[assignment]


DEFAULT_PARAMS = ["voltage", "current", "power", "humidity", "pressure", "temperature_f", "temperature_c"]
DEFAULT_POWER_FIELDS = ["voltage", "current", "power"]
DEFAULT_ENV_FIELDS = ["humidity", "pressure", "pressure_pa", "temperature", "temperature_f", "temperature_c"]
DEFAULT_ENV_FIELD_MAP = {
    "pressure_pa": "pressure",
    "temperature": "temperature_c",
}
DEFAULT_STATE_FILE = "/state/raw_to_medium_state.json"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("raw_to_medium_downsampler")
telemetry = telemetry_from_env("house-sensors.downsampling-medium")


@dataclass(frozen=True)
class Config:
    influx_url: str
    influx_token: str
    influx_org: str
    raw_power_bucket: str
    raw_env_bucket: str
    medium_bucket: str
    medium_measurement: str
    power_measurement: str
    env_measurement: str
    params: list[str]
    power_fields: list[str]
    env_fields: list[str]
    env_field_map: dict[str, str]
    start_iso: str | None
    end_iso: str | None
    days_back: int
    chunk_minutes: int
    dry_run: bool
    state_file: Path
    interval_seconds: int
    delay_seconds: int
    overlap_minutes: int
    quantile: float
    alpha: float
    target_anomaly_rate: float
    rate_beta: float
    learn_thresholds: bool
    learn_incremental: bool
    ensure_medium_bucket: bool
    medium_retention_seconds: int
    abs_bounds: dict[str, dict[str, float | None]]
    initial_minute_thresholds: dict[str, dict[str, float | int | None]]


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [part.strip() for part in value.split(",") if part.strip()]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _json_env(name: str, default: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return copy.deepcopy(default)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc


def _missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(np.isnan(value))
    except TypeError:
        return False


def _to_float_or_none(value: Any) -> float | None:
    if _missing(value):
        return None
    return float(value)


def _default_abs_bounds(params: list[str]) -> dict[str, dict[str, float | None]]:
    return {param: {"min": None, "max": None} for param in params}


def _default_minute_thresholds(params: list[str]) -> dict[str, dict[str, float | int | None]]:
    return {param: {"spreadT": None, "stdT": None, "oscT": 8} for param in params}


def _normalize_abs_bounds(raw: dict[str, Any], params: list[str]) -> dict[str, dict[str, float | None]]:
    normalized = _default_abs_bounds(params)
    for param in params:
        value = raw.get(param) or {}
        normalized[param] = {
            "min": _to_float_or_none(value.get("min")),
            "max": _to_float_or_none(value.get("max")),
        }
    return normalized


def _normalize_minute_thresholds(raw: dict[str, Any], params: list[str]) -> dict[str, dict[str, float | int | None]]:
    normalized = _default_minute_thresholds(params)
    for param in params:
        value = raw.get(param) or {}
        osc = value.get("oscT", normalized[param]["oscT"])
        normalized[param] = {
            "spreadT": _to_float_or_none(value.get("spreadT")),
            "stdT": _to_float_or_none(value.get("stdT")),
            "oscT": int(osc) if not _missing(osc) else 8,
        }
    return normalized


def load_config(args: argparse.Namespace) -> Config:
    params = _split_csv(os.getenv("DOWNSAMPLE_PARAMS"), DEFAULT_PARAMS)
    abs_bounds = _normalize_abs_bounds(_json_env("DOWNSAMPLE_ABS_BOUNDS_JSON", {}), params)
    minute_thresholds = _normalize_minute_thresholds(_json_env("DOWNSAMPLE_MINUTE_THRESH_JSON", {}), params)

    env_field_map = dict(DEFAULT_ENV_FIELD_MAP)
    env_field_map.update(_json_env("DOWNSAMPLE_ENV_FIELD_MAP_JSON", {}))

    config = Config(
        influx_url=os.getenv("INFLUX_URL", ""),
        influx_token=os.getenv("INFLUX_TOKEN", ""),
        influx_org=os.getenv("INFLUX_ORG", "ahara"),
        raw_power_bucket=os.getenv("DOWNSAMPLE_RAW_POWER_BUCKET", "voltage-data"),
        raw_env_bucket=os.getenv("DOWNSAMPLE_RAW_ENV_BUCKET", "environment-data"),
        medium_bucket=os.getenv("DOWNSAMPLE_MEDIUM_BUCKET", "sensors-medium"),
        medium_measurement=os.getenv("DOWNSAMPLE_MEDIUM_MEASUREMENT", "sensors"),
        power_measurement=os.getenv("DOWNSAMPLE_POWER_MEASUREMENT", "voltage_monitoring"),
        env_measurement=os.getenv("DOWNSAMPLE_ENV_MEASUREMENT", "environment"),
        params=params,
        power_fields=_split_csv(os.getenv("DOWNSAMPLE_POWER_FIELDS"), DEFAULT_POWER_FIELDS),
        env_fields=_split_csv(os.getenv("DOWNSAMPLE_ENV_FIELDS"), DEFAULT_ENV_FIELDS),
        env_field_map=env_field_map,
        start_iso=args.start or os.getenv("DOWNSAMPLE_MEDIUM_START_ISO"),
        end_iso=args.end or os.getenv("DOWNSAMPLE_MEDIUM_END_ISO"),
        days_back=int(args.days_back or os.getenv("DOWNSAMPLE_MEDIUM_DAYS_BACK", "60")),
        chunk_minutes=int(os.getenv("DOWNSAMPLE_MEDIUM_CHUNK_MINUTES", "60")),
        dry_run=args.dry_run or _as_bool(os.getenv("DOWNSAMPLE_MEDIUM_DRY_RUN")),
        state_file=Path(os.getenv("DOWNSAMPLE_MEDIUM_STATE_FILE", DEFAULT_STATE_FILE)),
        interval_seconds=int(os.getenv("DOWNSAMPLE_MEDIUM_INTERVAL_SECONDS", "60")),
        delay_seconds=int(os.getenv("DOWNSAMPLE_MEDIUM_DELAY_SECONDS", "60")),
        overlap_minutes=int(os.getenv("DOWNSAMPLE_MEDIUM_OVERLAP_MINUTES", "5")),
        quantile=float(os.getenv("DOWNSAMPLE_MINUTE_THRESHOLD_QUANTILE", "0.95")),
        alpha=float(os.getenv("DOWNSAMPLE_MINUTE_THRESHOLD_ALPHA", "0.2")),
        target_anomaly_rate=float(os.getenv("DOWNSAMPLE_MINUTE_TARGET_ANOM_RATE", "0.05")),
        rate_beta=float(os.getenv("DOWNSAMPLE_MINUTE_RATE_BETA", "0.25")),
        learn_thresholds=_as_bool(os.getenv("DOWNSAMPLE_MEDIUM_LEARN_THRESHOLDS"), True),
        learn_incremental=_as_bool(os.getenv("DOWNSAMPLE_MEDIUM_LEARN_INCREMENTAL"), False),
        ensure_medium_bucket=_as_bool(os.getenv("DOWNSAMPLE_ENSURE_MEDIUM_BUCKET"), True),
        medium_retention_seconds=int(os.getenv("DOWNSAMPLE_MEDIUM_RETENTION_SECONDS", "0")),
        abs_bounds=abs_bounds,
        initial_minute_thresholds=minute_thresholds,
    )

    required_env = {
        "INFLUX_URL": config.influx_url,
        "INFLUX_TOKEN": config.influx_token,
        "INFLUX_ORG": config.influx_org,
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    if config.chunk_minutes <= 0:
        raise ValueError("DOWNSAMPLE_MEDIUM_CHUNK_MINUTES must be positive")
    if config.interval_seconds <= 0:
        raise ValueError("DOWNSAMPLE_MEDIUM_INTERVAL_SECONDS must be positive")
    if config.delay_seconds < 0:
        raise ValueError("DOWNSAMPLE_MEDIUM_DELAY_SECONDS cannot be negative")
    if config.overlap_minutes < 0:
        raise ValueError("DOWNSAMPLE_MEDIUM_OVERLAP_MINUTES cannot be negative")
    return config


def load_state(config: Config) -> dict[str, Any]:
    if not config.state_file.exists():
        return {
            "minute_thresholds": copy.deepcopy(config.initial_minute_thresholds),
            "last_stop_iso": None,
            "coverage_start_iso": None,
            "coverage_stop_iso": None,
        }
    try:
        raw = json.loads(config.state_file.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"State file is not valid JSON: {config.state_file}") from exc

    return {
        "minute_thresholds": _normalize_minute_thresholds(raw.get("minute_thresholds") or {}, config.params),
        "last_stop_iso": raw.get("last_stop_iso"),
        "coverage_start_iso": raw.get("coverage_start_iso"),
        "coverage_stop_iso": raw.get("coverage_stop_iso"),
    }


def save_state(config: Config, state: dict[str, Any]) -> None:
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "minute_thresholds": _normalize_minute_thresholds(state.get("minute_thresholds") or {}, config.params),
        "last_stop_iso": state.get("last_stop_iso"),
        "coverage_start_iso": state.get("coverage_start_iso"),
        "coverage_stop_iso": state.get("coverage_stop_iso"),
        "updated_at": dt.datetime.now(tz=tzutc()).isoformat(),
    }
    tmp_path = config.state_file.with_suffix(config.state_file.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(config.state_file)


def _parse_time(value: str) -> dt.datetime:
    parsed = pd.to_datetime(value, utc=True).to_pydatetime()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzutc())
    return parsed


def _floor_minute(value: dt.datetime) -> dt.datetime:
    return value.astimezone(tzutc()).replace(second=0, microsecond=0)


def select_window(config: Config, state: dict[str, Any]) -> tuple[dt.datetime, dt.datetime, bool]:
    explicit = bool(config.start_iso and config.end_iso)
    if explicit:
        start = _parse_time(config.start_iso or "")
        stop = _parse_time(config.end_iso or "")
        return start, stop, True

    now = dt.datetime.now(tz=tzutc())
    stop = _floor_minute(now - dt.timedelta(seconds=config.delay_seconds))
    if state.get("last_stop_iso"):
        start = _parse_time(state["last_stop_iso"]) - dt.timedelta(minutes=config.overlap_minutes)
        start = _floor_minute(start)
        return start, stop, config.learn_incremental

    start = stop - dt.timedelta(days=config.days_back)
    return start, stop, True


def _iso(value: dt.datetime) -> str:
    return value.astimezone(tzutc()).isoformat().replace("+00:00", "Z")


def _point() -> type:
    if Point is None:
        raise RuntimeError("influxdb-client is required to create points")
    return Point


def _require_influx_client() -> type:
    if InfluxDBClient is None:
        raise RuntimeError("influxdb-client is required at runtime")
    return InfluxDBClient


def _write_sync(config: Config, ic: Any, points: list[Any]) -> int:
    if not points:
        return 0
    if SYNCHRONOUS is None:
        raise RuntimeError("influxdb-client synchronous write API is required at runtime")
    with ic.write_api(write_options=SYNCHRONOUS) as write_api:
        write_api.write(bucket=config.medium_bucket, record=points)
    return len(points)


def _influx_api_json(config: Config, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{config.influx_url.rstrip('/')}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Token {config.influx_token}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"InfluxDB API {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def ensure_medium_bucket(config: Config) -> None:
    if not config.ensure_medium_bucket:
        return

    query = urllib.parse.urlencode({"name": config.medium_bucket, "org": config.influx_org})
    buckets = _influx_api_json(config, "GET", f"/api/v2/buckets?{query}").get("buckets") or []
    if buckets:
        log.info("medium bucket exists: %s", config.medium_bucket)
        return

    org_query = urllib.parse.urlencode({"org": config.influx_org})
    orgs = _influx_api_json(config, "GET", f"/api/v2/orgs?{org_query}").get("orgs") or []
    if not orgs:
        raise RuntimeError(f"InfluxDB organization not found: {config.influx_org}")

    retention_rules = []
    if config.medium_retention_seconds > 0:
        retention_rules.append({"type": "expire", "everySeconds": config.medium_retention_seconds})

    _influx_api_json(
        config,
        "POST",
        "/api/v2/buckets",
        {
            "orgID": orgs[0]["id"],
            "name": config.medium_bucket,
            "retentionRules": retention_rules,
        },
    )
    log.info("medium bucket created: %s", config.medium_bucket)


def _field_regex(fields: list[str]) -> str:
    return "^(" + "|".join(re.escape(field) for field in fields) + ")$"


def _empty_raw_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["_time", "_value", "_field", "device_name", "device_id", "location"])


def _query_raw(config: Config, ic: Any, bucket: str, measurement: str, fields: list[str], start: dt.datetime, stop: dt.datetime) -> pd.DataFrame:
    flux = f"""
from(bucket: "{bucket}")
  |> range(start: time(v: "{_iso(start)}"), stop: time(v: "{_iso(stop)}"))
  |> filter(fn: (r) => r._measurement == "{measurement}")
  |> filter(fn: (r) => r._field =~ /{_field_regex(fields)}/)
  |> keep(columns: ["_time", "_value", "_field", "device_name", "device_id", "location"])
"""
    rows: list[dict[str, Any]] = []
    for table in ic.query_api().query(flux):
        for record in table.records:
            rows.append(
                {
                    "_time": record.get_time(),
                    "_value": record.get_value(),
                    "_field": record.values.get("_field"),
                    "device_name": record.values.get("device_name"),
                    "device_id": record.values.get("device_id"),
                    "location": record.values.get("location"),
                }
            )
    if not rows:
        return _empty_raw_frame()
    frame = pd.DataFrame(rows)
    frame["_time"] = pd.to_datetime(frame["_time"], utc=True)
    frame["_value"] = pd.to_numeric(frame["_value"], errors="coerce")
    return frame.dropna(subset=["_value", "_field"])


def _caps_underscore(value: str | None) -> str:
    if value is None:
        return "NONE"
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", str(value))
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized.upper() if normalized else "NONE"


def _tag_value(value: Any) -> str:
    if _missing(value):
        return "unknown"
    return str(value)


def _tags_power(row: pd.Series) -> dict[str, str]:
    device_name = row.get("device_name")
    location = row.get("location")
    name = _tag_value(device_name if not _missing(device_name) else location)
    return {
        "domain": "power",
        "sensor_class": os.getenv("DOWNSAMPLE_POWER_SENSOR_CLASS", "KP125M"),
        "sensor_location": name,
        "sensor_id": _caps_underscore(name),
    }


def _tags_env(row: pd.Series) -> dict[str, str]:
    return {
        "domain": "environment",
        "sensor_class": os.getenv("DOWNSAMPLE_ENV_SENSOR_CLASS", "ENV3"),
        "sensor_location": _tag_value(row.get("location")),
        "sensor_id": _tag_value(row.get("device_id")),
    }


def _oscillation_count(values: np.ndarray) -> int:
    """Count sign changes of first difference, ignoring zeros."""
    if len(values) < 3:
        return 0
    diffs = np.diff(values)
    diffs = diffs[diffs != 0]
    if len(diffs) < 2:
        return 0
    signs = np.sign(diffs)
    return int(np.sum(signs[1:] * signs[:-1] < 0))


def minute_is_anomaly(
    param: str,
    minute_min: float,
    minute_max: float,
    spread: float,
    std: float,
    osc: int,
    abs_bounds: dict[str, dict[str, float | None]],
    thresholds: dict[str, dict[str, float | int | None]],
) -> bool:
    bounds = abs_bounds.get(param, {"min": None, "max": None})
    threshold = thresholds.get(param, {"spreadT": None, "stdT": None, "oscT": 8})

    lo = bounds.get("min")
    hi = bounds.get("max")
    spread_t = threshold.get("spreadT")
    std_t = threshold.get("stdT")
    osc_t = threshold.get("oscT", 8)

    if not _missing(lo) and minute_min < float(lo):
        return True
    if not _missing(hi) and minute_max > float(hi):
        return True
    if not _missing(spread_t) and spread > float(spread_t):
        return True
    if not _missing(std_t) and std > float(std_t):
        return True
    return osc >= int(osc_t or 8)


def _ema(old: float | int | None, new: float, alpha: float) -> float:
    if _missing(old):
        return float(new)
    return float((1 - alpha) * float(old) + alpha * float(new))


def update_minute_thresholds(
    observed_df: pd.DataFrame,
    abs_bounds: dict[str, dict[str, float | None]],
    thresholds: dict[str, dict[str, float | int | None]],
    quantile: float,
    alpha: float,
    target_rate: float,
    rate_beta: float,
) -> dict[str, dict[str, float | int | None]]:
    updated = copy.deepcopy(thresholds)

    for param, group in observed_df.groupby("_field"):
        bounds = abs_bounds.get(param, {"min": None, "max": None})
        inside = group.copy()
        if not _missing(bounds.get("min")):
            inside = inside[~inside["minute_min"].lt(float(bounds["min"]))]
        if not _missing(bounds.get("max")):
            inside = inside[~inside["minute_max"].gt(float(bounds["max"]))]
        if inside.empty:
            continue

        updated.setdefault(param, {"spreadT": None, "stdT": None, "oscT": 8})
        spread_q = float(np.nanquantile(inside["spread"].to_numpy(dtype=float), quantile))
        std_q = float(np.nanquantile(inside["std"].to_numpy(dtype=float), quantile))
        osc_q = int(np.ceil(np.nanquantile(inside["osc"].to_numpy(dtype=float), quantile)))

        if not np.isnan(spread_q):
            updated[param]["spreadT"] = _ema(updated[param].get("spreadT"), spread_q, alpha)
        if not np.isnan(std_q):
            updated[param]["stdT"] = _ema(updated[param].get("stdT"), std_q, alpha)
        updated[param]["oscT"] = int(round(_ema(updated[param].get("oscT", 8), float(osc_q), alpha)))

    for param, group in observed_df.groupby("_field"):
        total = int(group.shape[0])
        if total == 0:
            continue
        observed = float(np.nansum(group["is_anom"])) / float(total)
        err = observed - target_rate
        if abs(err) < 0.001:
            continue
        factor = max(0.5, min(1.5, 1.0 + rate_beta * err))
        updated.setdefault(param, {"spreadT": None, "stdT": None, "oscT": 8})

        for key in ("spreadT", "stdT"):
            value = updated[param].get(key)
            if not _missing(value):
                updated[param][key] = float(max(1e-9, float(value) * factor))

        osc_t = updated[param].get("oscT", 8)
        if not _missing(osc_t):
            updated[param]["oscT"] = max(1, int(round(float(osc_t) * factor)))

    return updated


def _medium_point(config: Config, timestamp: dt.datetime, tags: dict[str, Any], resolution: str, stat: str, field: str, value: float) -> Any:
    point = _point()(config.medium_measurement).time(timestamp)
    for key, tag_value in tags.items():
        point = point.tag(key, _tag_value(tag_value))
    return point.tag("resolution", resolution).tag("stat", stat).field(field, float(value))


def _empty_observed_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "_field": pd.Series(dtype="string"),
            "minute_min": pd.Series(dtype="float64"),
            "minute_max": pd.Series(dtype="float64"),
            "spread": pd.Series(dtype="float64"),
            "std": pd.Series(dtype="float64"),
            "osc": pd.Series(dtype="int64"),
            "is_anom": pd.Series(dtype="int64"),
        }
    )


def _prepare_raw_frame(config: Config, frame: pd.DataFrame, is_power: bool) -> tuple[pd.DataFrame, list[str]]:
    if frame.empty:
        return frame, []

    prepared = frame.copy()
    prepared["minute"] = prepared["_time"].dt.floor("min")
    if is_power:
        prepared["sensor_location"] = prepared.apply(lambda row: _tags_power(row)["sensor_location"], axis=1)
        prepared["sensor_id"] = prepared.apply(lambda row: _tags_power(row)["sensor_id"], axis=1)
        prepared["sensor_class"] = os.getenv("DOWNSAMPLE_POWER_SENSOR_CLASS", "KP125M")
        prepared["domain"] = "power"
    else:
        prepared["_field"] = prepared["_field"].map(lambda value: config.env_field_map.get(str(value), str(value)))
        prepared["sensor_location"] = prepared.apply(lambda row: _tags_env(row)["sensor_location"], axis=1)
        prepared["sensor_id"] = prepared.apply(lambda row: _tags_env(row)["sensor_id"], axis=1)
        prepared["sensor_class"] = os.getenv("DOWNSAMPLE_ENV_SENSOR_CLASS", "ENV3")
        prepared["domain"] = "environment"

    group_cols = ["minute", "_field", "domain", "sensor_class", "sensor_location", "sensor_id"]
    return prepared, group_cols


def build_medium_points(
    config: Config,
    frame: pd.DataFrame,
    is_power: bool,
    abs_bounds: dict[str, dict[str, float | None]],
    minute_thresholds: dict[str, dict[str, float | int | None]],
) -> tuple[list[Any], pd.DataFrame, dict[str, int]]:
    prepared, group_cols = _prepare_raw_frame(config, frame, is_power)
    if prepared.empty:
        return [], _empty_observed_frame(), {"series": 0, "anomalies": 0}

    agg = (
        prepared.groupby(group_cols, dropna=False)["_value"]
        .agg(
            minute_min="min",
            minute_max="max",
            mean="mean",
            std=lambda series: float(np.nanstd(series.to_numpy(dtype=float), ddof=0)),
        )
        .reset_index()
    )
    agg["spread"] = agg["minute_max"] - agg["minute_min"]
    osc = prepared.groupby(group_cols, dropna=False)["_value"].apply(lambda series: _oscillation_count(series.to_numpy(dtype=float)))
    agg = agg.merge(osc.rename("osc").reset_index(), on=group_cols, how="left")

    def decide(row: pd.Series) -> bool:
        return minute_is_anomaly(
            param=str(row["_field"]),
            minute_min=float(row["minute_min"]),
            minute_max=float(row["minute_max"]),
            spread=float(row["spread"]),
            std=0.0 if pd.isna(row["std"]) else float(row["std"]),
            osc=int(row["osc"] if not pd.isna(row["osc"]) else 0),
            abs_bounds=abs_bounds,
            thresholds=minute_thresholds,
        )

    agg["is_anom"] = agg.apply(lambda row: 1 if decide(row) else 0, axis=1)
    points: list[Any] = []

    calm = agg[agg["is_anom"] == 0]
    for _, row in calm.iterrows():
        tags = {key: row[key] for key in ("domain", "sensor_class", "sensor_location", "sensor_id")}
        timestamp = pd.to_datetime(row["minute"]).to_pydatetime()
        field = str(row["_field"])
        mean_value = float(row["mean"]) if not pd.isna(row["mean"]) else float((row["minute_min"] + row["minute_max"]) / 2.0)
        points.append(_medium_point(config, timestamp, tags, "1m", "min", field, float(row["minute_min"])))
        points.append(_medium_point(config, timestamp, tags, "1m", "max", field, float(row["minute_max"])))
        points.append(_medium_point(config, timestamp, tags, "1m", "mean", field, mean_value))
        points.append(_medium_point(config, timestamp, tags, "1m", "computed", field, mean_value))

    anomalous_keys = agg.loc[agg["is_anom"] == 1, group_cols].drop_duplicates()
    if not anomalous_keys.empty:
        anomalous_raw = prepared.merge(anomalous_keys, on=group_cols, how="inner")
        for _, row in anomalous_raw.iterrows():
            tags = {key: row[key] for key in ("domain", "sensor_class", "sensor_location", "sensor_id")}
            timestamp = pd.to_datetime(row["_time"]).to_pydatetime()
            field = str(row["_field"])
            value = float(row["_value"])
            points.append(_medium_point(config, timestamp, tags, "1s", "raw", field, value))
            points.append(_medium_point(config, timestamp, tags, "1s", "computed", field, value))

    observed = agg[["_field", "minute_min", "minute_max", "spread", "std", "osc", "is_anom"]].copy()
    return points, observed, {"series": int(agg.shape[0]), "anomalies": int(agg["is_anom"].sum())}


def process_chunk(
    config: Config,
    ic: Any,
    start: dt.datetime,
    stop: dt.datetime,
    minute_thresholds: dict[str, dict[str, float | int | None]],
    learn_thresholds: bool,
) -> tuple[dict[str, int], dict[str, dict[str, float | int | None]]]:
    power = _query_raw(config, ic, config.raw_power_bucket, config.power_measurement, config.power_fields, start, stop)
    env = _query_raw(config, ic, config.raw_env_bucket, config.env_measurement, config.env_fields, start, stop)

    power_points, power_observed, power_stats = build_medium_points(config, power, True, config.abs_bounds, minute_thresholds)
    env_points, env_observed, env_stats = build_medium_points(config, env, False, config.abs_bounds, minute_thresholds)
    points = [*power_points, *env_points]
    wrote = 0 if config.dry_run else _write_sync(config, ic, points)

    observed_frames = [frame for frame in (power_observed, env_observed) if not frame.empty]
    if learn_thresholds and observed_frames:
        observed = pd.concat(observed_frames, ignore_index=True)
        minute_thresholds = update_minute_thresholds(
            observed,
            config.abs_bounds,
            minute_thresholds,
            config.quantile,
            config.alpha,
            config.target_anomaly_rate,
            config.rate_beta,
        )

    return {
        "series": power_stats["series"] + env_stats["series"],
        "anomalies": power_stats["anomalies"] + env_stats["anomalies"],
        "wrote": wrote,
    }, minute_thresholds


def run_once(config: Config) -> dict[str, Any]:
    state = load_state(config)
    start, stop, window_can_learn = select_window(config, state)
    if start >= stop:
        raise ValueError(f"Invalid or empty window: start={_iso(start)} stop={_iso(stop)}")

    chunk = dt.timedelta(minutes=config.chunk_minutes)
    totals = {"chunks": 0, "minutes": 0, "series": 0, "anomalies": 0, "wrote": 0}
    minute_thresholds = _normalize_minute_thresholds(state.get("minute_thresholds") or {}, config.params)
    should_learn = config.learn_thresholds and window_can_learn
    if not state.get("coverage_start_iso"):
        state["coverage_start_iso"] = _iso(start)
    else:
        existing_start = _parse_time(state["coverage_start_iso"])
        if start < existing_start:
            state["coverage_start_iso"] = _iso(start)

    log.info(
        "raw-to-medium downsample | window=[%s -> %s] | power=%s env=%s medium=%s | chunk=%sm | dry_run=%s | learn=%s",
        _iso(start),
        _iso(stop),
        config.raw_power_bucket,
        config.raw_env_bucket,
        config.medium_bucket,
        config.chunk_minutes,
        config.dry_run,
        should_learn,
    )
    log.info("minute thresholds initial: %s", minute_thresholds)
    log.info("absolute bounds: %s", config.abs_bounds)

    client_type = _require_influx_client()
    started_at = time.time()
    if not config.dry_run:
        ensure_medium_bucket(config)
    with client_type(url=config.influx_url, token=config.influx_token, org=config.influx_org) as ic:
        cur = start
        while cur < stop:
            nxt = min(cur + chunk, stop)
            stats, minute_thresholds = process_chunk(config, ic, cur, nxt, minute_thresholds, should_learn)
            minutes = max(0, int((nxt - cur).total_seconds() // 60))
            totals["chunks"] += 1
            totals["minutes"] += minutes
            for key in ("series", "anomalies", "wrote"):
                totals[key] += stats[key]

            if not config.dry_run:
                state["minute_thresholds"] = minute_thresholds
                state["last_stop_iso"] = _iso(nxt)
                state["coverage_stop_iso"] = _iso(nxt)
                save_state(config, state)

            rate = stats["anomalies"] / max(1, stats["series"]) if stats["series"] else 0.0
            log.info(
                "chunk [%s -> %s] | minutes=%s series=%s anomalies=%s rate=%.2f%% | wrote=%s | thresholds=%s",
                _iso(cur),
                _iso(nxt),
                minutes,
                stats["series"],
                stats["anomalies"],
                rate * 100,
                stats["wrote"],
                minute_thresholds,
            )
            cur = nxt

    elapsed = round(time.time() - started_at, 2)
    overall_rate = totals["anomalies"] / max(1, totals["series"]) if totals["series"] else 0.0
    log.info(
        "raw-to-medium complete | chunks=%s minutes=%s series=%s anomalies=%s rate=%.2f%% | wrote=%s | elapsed=%ss",
        totals["chunks"],
        totals["minutes"],
        totals["series"],
        totals["anomalies"],
        overall_rate * 100,
        totals["wrote"],
        elapsed,
    )
    attrs = {"operation.type": "background", "job": "raw_to_medium", "outcome": "success"}
    telemetry.count("house_sensors.job_cycles", attributes=attrs)
    telemetry.record("house_sensors.job_duration_ms", elapsed * 1000, attrs)
    telemetry.record("house_sensors.job_chunks", totals["chunks"], attrs)
    telemetry.record("house_sensors.job_series", totals["series"], attrs)
    telemetry.record("house_sensors.job_anomalies", totals["anomalies"], attrs)
    telemetry.record("house_sensors.job_points_written", totals["wrote"], attrs)
    return {**totals, "elapsed_seconds": elapsed}


def run_loop(config: Config) -> None:
    while True:
        try:
            run_once(config)
        except ValueError as exc:
            log.info("no raw-to-medium work this cycle: %s", exc)
            telemetry.count("house_sensors.job_cycles", attributes={"operation.type": "background", "job": "raw_to_medium", "outcome": "no_work"})
        except Exception as exc:
            log.error("raw-to-medium cycle failed: %s", exc)
            telemetry.count("house_sensors.job_cycles", attributes={"operation.type": "background", "job": "raw_to_medium", "outcome": "error"})
            traceback.print_exc()
        time.sleep(config.interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Downsample raw sensor data into medium storage")
    parser.add_argument("mode", nargs="?", choices=["run-once", "run-loop"], default=os.getenv("DOWNSAMPLE_MODE", "run-once"))
    parser.add_argument("--start", help="override window start ISO timestamp")
    parser.add_argument("--end", help="override window end ISO timestamp")
    parser.add_argument("--days-back", type=int, help="initial backfill days when no watermark exists")
    parser.add_argument("--dry-run", action="store_true", help="query and plan writes without writing or saving state")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_config(args)
    if args.mode == "run-loop":
        run_loop(config)
    else:
        run_once(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
