#!/usr/bin/env python3
"""Medium-to-long sensor downsampling job.

This direct Python process reads 1m/1s records from the medium bucket, writes
calm hours as 1h records to the long bucket, preserves anomalous 1m/1s records,
and stores learned hour thresholds in a local JSON state file.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import logging
import os
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
DEFAULT_TAGS = ["domain", "sensor_class", "sensor_location", "sensor_id"]
DEFAULT_STATE_FILE = "/state/medium_to_long_state.json"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("medium_to_long_downsampler")
telemetry = telemetry_from_env("house-sensors.downsampling-long")


@dataclass(frozen=True)
class Config:
    influx_url: str
    influx_token: str
    influx_org: str
    src_bucket: str
    dst_bucket: str
    measurement: str
    params: list[str]
    tags: list[str]
    start_iso: str | None
    end_iso: str | None
    days_back: int
    chunk_minutes: int
    write_batch_size: int
    write_batch_sleep_seconds: float
    chunk_sleep_seconds: float
    advance_empty_windows: bool
    dry_run: bool
    state_file: Path
    interval_seconds: int
    delay_minutes: int
    quantile: float
    alpha: float
    target_anomaly_rate: float
    rate_beta: float
    ensure_dst_bucket: bool
    dst_retention_seconds: int
    abs_bounds: dict[str, dict[str, float | None]]
    initial_hour_thresholds: dict[str, dict[str, float | int | None]]


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


def _default_hour_thresholds(params: list[str]) -> dict[str, dict[str, float | int | None]]:
    return {param: {"spreadT": None, "stdT": None, "oscT": 6} for param in params}


def _normalize_abs_bounds(raw: dict[str, Any], params: list[str]) -> dict[str, dict[str, float | None]]:
    normalized = _default_abs_bounds(params)
    for param in params:
        value = raw.get(param) or {}
        normalized[param] = {
            "min": _to_float_or_none(value.get("min")),
            "max": _to_float_or_none(value.get("max")),
        }
    return normalized


def _normalize_hour_thresholds(raw: dict[str, Any], params: list[str]) -> dict[str, dict[str, float | int | None]]:
    normalized = _default_hour_thresholds(params)
    for param in params:
        value = raw.get(param) or {}
        osc = value.get("oscT", normalized[param]["oscT"])
        normalized[param] = {
            "spreadT": _to_float_or_none(value.get("spreadT")),
            "stdT": _to_float_or_none(value.get("stdT")),
            "oscT": int(osc) if not _missing(osc) else 6,
        }
    return normalized


def load_config(args: argparse.Namespace) -> Config:
    params = _split_csv(os.getenv("DOWNSAMPLE_PARAMS"), DEFAULT_PARAMS)
    tags = _split_csv(os.getenv("DOWNSAMPLE_TAGS"), DEFAULT_TAGS)

    abs_bounds = _normalize_abs_bounds(_json_env("DOWNSAMPLE_ABS_BOUNDS_JSON", {}), params)
    hour_thresholds = _normalize_hour_thresholds(_json_env("DOWNSAMPLE_HOUR_THRESH_JSON", {}), params)

    config = Config(
        influx_url=os.getenv("INFLUX_URL", ""),
        influx_token=os.getenv("INFLUX_TOKEN", ""),
        influx_org=os.getenv("INFLUX_ORG", "ahara"),
        src_bucket=os.getenv("DOWNSAMPLE_SRC_BUCKET", "sensors-medium"),
        dst_bucket=os.getenv("DOWNSAMPLE_DST_BUCKET", "sensors-long"),
        measurement=os.getenv("DOWNSAMPLE_MEASUREMENT", "sensors"),
        params=params,
        tags=tags,
        start_iso=args.start or os.getenv("DOWNSAMPLE_START_ISO"),
        end_iso=args.end or os.getenv("DOWNSAMPLE_END_ISO"),
        days_back=int(args.days_back or os.getenv("DOWNSAMPLE_DAYS_BACK", "60")),
        chunk_minutes=int(os.getenv("DOWNSAMPLE_CHUNK_MINUTES", "360")),
        write_batch_size=int(os.getenv("DOWNSAMPLE_WRITE_BATCH_SIZE", "5000")),
        write_batch_sleep_seconds=float(os.getenv("DOWNSAMPLE_WRITE_BATCH_SLEEP_SECONDS", "0")),
        chunk_sleep_seconds=float(os.getenv("DOWNSAMPLE_CHUNK_SLEEP_SECONDS", "0")),
        advance_empty_windows=_as_bool(os.getenv("DOWNSAMPLE_ADVANCE_EMPTY_WINDOWS"), False),
        dry_run=args.dry_run or _as_bool(os.getenv("DOWNSAMPLE_DRY_RUN")),
        state_file=Path(os.getenv("DOWNSAMPLE_STATE_FILE", DEFAULT_STATE_FILE)),
        interval_seconds=int(os.getenv("DOWNSAMPLE_INTERVAL_SECONDS", "3600")),
        delay_minutes=int(os.getenv("DOWNSAMPLE_DELAY_MINUTES", "10")),
        quantile=float(os.getenv("DOWNSAMPLE_THRESHOLD_QUANTILE", "0.95")),
        alpha=float(os.getenv("DOWNSAMPLE_THRESHOLD_ALPHA", "0.2")),
        target_anomaly_rate=float(os.getenv("DOWNSAMPLE_TARGET_ANOM_RATE", "0.05")),
        rate_beta=float(os.getenv("DOWNSAMPLE_RATE_BETA", "0.25")),
        ensure_dst_bucket=_as_bool(os.getenv("DOWNSAMPLE_ENSURE_DST_BUCKET"), True),
        dst_retention_seconds=int(os.getenv("DOWNSAMPLE_DST_RETENTION_SECONDS", "0")),
        abs_bounds=abs_bounds,
        initial_hour_thresholds=hour_thresholds,
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
        raise ValueError("DOWNSAMPLE_CHUNK_MINUTES must be positive")
    if config.write_batch_size <= 0:
        raise ValueError("DOWNSAMPLE_WRITE_BATCH_SIZE must be positive")
    if config.write_batch_sleep_seconds < 0:
        raise ValueError("DOWNSAMPLE_WRITE_BATCH_SLEEP_SECONDS cannot be negative")
    if config.chunk_sleep_seconds < 0:
        raise ValueError("DOWNSAMPLE_CHUNK_SLEEP_SECONDS cannot be negative")
    if config.interval_seconds <= 0:
        raise ValueError("DOWNSAMPLE_INTERVAL_SECONDS must be positive")
    return config


def load_state(config: Config) -> dict[str, Any]:
    if not config.state_file.exists():
        return {
            "hour_thresholds": copy.deepcopy(config.initial_hour_thresholds),
            "last_stop_iso": None,
            "coverage_start_iso": None,
            "coverage_stop_iso": None,
            "dst_bucket_id": None,
            "dst_bucket_name": config.dst_bucket,
        }
    try:
        raw = json.loads(config.state_file.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"State file is not valid JSON: {config.state_file}") from exc

    return {
        "hour_thresholds": _normalize_hour_thresholds(raw.get("hour_thresholds") or {}, config.params),
        "last_stop_iso": raw.get("last_stop_iso"),
        "coverage_start_iso": raw.get("coverage_start_iso"),
        "coverage_stop_iso": raw.get("coverage_stop_iso"),
        "dst_bucket_id": raw.get("dst_bucket_id"),
        "dst_bucket_name": raw.get("dst_bucket_name") or config.dst_bucket,
    }


def save_state(config: Config, state: dict[str, Any]) -> None:
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hour_thresholds": _normalize_hour_thresholds(state.get("hour_thresholds") or {}, config.params),
        "last_stop_iso": state.get("last_stop_iso"),
        "coverage_start_iso": state.get("coverage_start_iso"),
        "coverage_stop_iso": state.get("coverage_stop_iso"),
        "dst_bucket_id": state.get("dst_bucket_id"),
        "dst_bucket_name": state.get("dst_bucket_name") or config.dst_bucket,
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


def _floor_hour(value: dt.datetime) -> dt.datetime:
    return value.astimezone(tzutc()).replace(minute=0, second=0, microsecond=0)


def select_window(config: Config, state: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
    if config.start_iso and config.end_iso:
        start = _parse_time(config.start_iso)
        stop = _parse_time(config.end_iso)
    else:
        now = dt.datetime.now(tz=tzutc())
        stop = _floor_hour(now - dt.timedelta(minutes=config.delay_minutes))
        if state.get("last_stop_iso"):
            start = _parse_time(state["last_stop_iso"])
        else:
            start = stop - dt.timedelta(days=config.days_back)

    if start >= stop:
        raise ValueError(f"Invalid or empty window: start={start.isoformat()} stop={stop.isoformat()}")
    return start, stop


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
    written = 0
    with ic.write_api(write_options=SYNCHRONOUS) as write_api:
        for start in range(0, len(points), config.write_batch_size):
            batch = points[start : start + config.write_batch_size]
            write_api.write(bucket=config.dst_bucket, record=batch)
            written += len(batch)
            if config.write_batch_sleep_seconds > 0 and written < len(points):
                time.sleep(config.write_batch_sleep_seconds)
    return written


def _influx_api_json(
    config: Config,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    not_found_ok: bool = False,
) -> dict[str, Any]:
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
        if exc.code == 404 and not_found_ok:
            return {}
        raise RuntimeError(f"InfluxDB API {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def get_bucket_id(config: Config, bucket_name: str) -> str | None:
    query = urllib.parse.urlencode({"name": bucket_name, "org": config.influx_org})
    buckets = _influx_api_json(config, "GET", f"/api/v2/buckets?{query}", not_found_ok=True).get("buckets") or []
    for bucket in buckets:
        if bucket.get("name") == bucket_name:
            return bucket.get("id")
    return None


def ensure_destination_bucket(config: Config) -> str | None:
    if not config.ensure_dst_bucket:
        return None

    bucket_id = get_bucket_id(config, config.dst_bucket)
    if bucket_id:
        log.info("destination bucket exists: %s", config.dst_bucket)
        return bucket_id

    org_query = urllib.parse.urlencode({"org": config.influx_org})
    orgs = _influx_api_json(config, "GET", f"/api/v2/orgs?{org_query}").get("orgs") or []
    if not orgs:
        raise RuntimeError(f"InfluxDB organization not found: {config.influx_org}")

    retention_rules = []
    if config.dst_retention_seconds > 0:
        retention_rules.append({"type": "expire", "everySeconds": config.dst_retention_seconds})

    created = _influx_api_json(
        config,
        "POST",
        "/api/v2/buckets",
        {
            "orgID": orgs[0]["id"],
            "name": config.dst_bucket,
            "retentionRules": retention_rules,
        },
    )
    log.info("destination bucket created: %s", config.dst_bucket)
    return created.get("id") or get_bucket_id(config, config.dst_bucket)


def sync_destination_bucket_identity(config: Config, state: dict[str, Any], bucket_id: str | None) -> bool:
    if not bucket_id:
        return False

    previous_id = state.get("dst_bucket_id")
    previous_name = state.get("dst_bucket_name")
    has_progress = any(state.get(key) for key in ("last_stop_iso", "coverage_start_iso", "coverage_stop_iso"))
    identity_missing = previous_id is None and has_progress
    identity_changed = previous_id is not None and previous_id != bucket_id
    name_changed = previous_name is not None and previous_name != config.dst_bucket

    state["dst_bucket_id"] = bucket_id
    state["dst_bucket_name"] = config.dst_bucket

    if not (identity_missing or identity_changed or name_changed):
        return False

    log.warning(
        "destination bucket identity changed or was not recorded; resetting derived watermark | previous=%s/%s current=%s/%s",
        previous_name,
        previous_id,
        config.dst_bucket,
        bucket_id,
    )
    state["last_stop_iso"] = None
    state["coverage_start_iso"] = None
    state["coverage_stop_iso"] = None
    return True


def _tag_value(value: Any) -> str:
    if _missing(value):
        return "unknown"
    return str(value)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(tzutc()).isoformat().replace("+00:00", "Z")


def _empty_medium_frame(config: Config) -> pd.DataFrame:
    return pd.DataFrame(columns=["_time", "_value", "_field", "stat", *config.tags])


def _read_medium(config: Config, ic: Any, start_iso: str, stop_iso: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read minute stats/computed rows and second anomaly rows from medium storage."""
    keep_columns = ["_time", "_value", "_field", "stat", *config.tags]
    base_keep = json.dumps(keep_columns)

    flux_1m = f"""
from(bucket: "{config.src_bucket}")
  |> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}"))
  |> filter(fn: (r) => r._measurement == "{config.measurement}")
  |> filter(fn: (r) => r.resolution == "1m")
  |> filter(fn: (r) => r.stat == "min" or r.stat == "max" or r.stat == "mean" or r.stat == "computed")
  |> keep(columns: {base_keep})
"""
    flux_1s = f"""
from(bucket: "{config.src_bucket}")
  |> range(start: time(v: "{start_iso}"), stop: time(v: "{stop_iso}"))
  |> filter(fn: (r) => r._measurement == "{config.measurement}")
  |> filter(fn: (r) => r.resolution == "1s")
  |> filter(fn: (r) => r.stat == "raw" or r.stat == "computed")
  |> keep(columns: {base_keep})
"""

    def run(flux: str) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for table in ic.query_api().query(flux):
            for record in table.records:
                row = {
                    "_time": record.get_time(),
                    "_value": record.get_value(),
                    "_field": record.values.get("_field"),
                    "stat": record.values.get("stat"),
                }
                for tag in config.tags:
                    row[tag] = record.values.get(tag)
                rows.append(row)

        if not rows:
            return _empty_medium_frame(config)

        frame = pd.DataFrame(rows)
        frame["_time"] = pd.to_datetime(frame["_time"], utc=True)
        return frame

    return run(flux_1m), run(flux_1s)


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


def _ema(old: float | int | None, new: float, alpha: float) -> float:
    if _missing(old):
        return float(new)
    return float((1 - alpha) * float(old) + alpha * float(new))


def hour_is_anomaly(
    param: str,
    hour_min: float,
    hour_max: float,
    spread: float,
    std: float,
    osc: int,
    abs_bounds: dict[str, dict[str, float | None]],
    thresholds: dict[str, dict[str, float | int | None]],
) -> bool:
    bounds = abs_bounds.get(param, {"min": None, "max": None})
    threshold = thresholds.get(param, {"spreadT": None, "stdT": None, "oscT": 6})

    lo = bounds.get("min")
    hi = bounds.get("max")
    spread_t = threshold.get("spreadT")
    std_t = threshold.get("stdT")
    osc_t = threshold.get("oscT", 6)

    if not _missing(lo) and hour_min < float(lo):
        return True
    if not _missing(hi) and hour_max > float(hi):
        return True
    if not _missing(spread_t) and spread > float(spread_t):
        return True
    if not _missing(std_t) and std > float(std_t):
        return True
    return osc >= int(osc_t or 6)


def update_hour_thresholds(
    per_hour_df: pd.DataFrame,
    abs_bounds: dict[str, dict[str, float | None]],
    thresholds: dict[str, dict[str, float | int | None]],
    quantile: float,
    alpha: float,
    target_rate: float,
    rate_beta: float,
) -> dict[str, dict[str, float | int | None]]:
    """Update spread/std/oscillation thresholds from observed hourly behavior."""
    updated = copy.deepcopy(thresholds)

    for param, group in per_hour_df.groupby("_field"):
        bounds = abs_bounds.get(param, {"min": None, "max": None})
        inside = group.copy()
        if not _missing(bounds.get("min")):
            inside = inside[~inside["hour_min"].lt(float(bounds["min"]))]
        if not _missing(bounds.get("max")):
            inside = inside[~inside["hour_max"].gt(float(bounds["max"]))]
        if inside.empty:
            continue

        updated.setdefault(param, {"spreadT": None, "stdT": None, "oscT": 6})
        spread_q = float(np.nanquantile(inside["spread"].to_numpy(dtype=float), quantile))
        std_q = float(np.nanquantile(inside["std"].to_numpy(dtype=float), quantile))
        osc_q = int(np.ceil(np.nanquantile(inside["osc"].to_numpy(dtype=float), quantile)))

        if not np.isnan(spread_q):
            updated[param]["spreadT"] = _ema(updated[param].get("spreadT"), spread_q, alpha)
        if not np.isnan(std_q):
            updated[param]["stdT"] = _ema(updated[param].get("stdT"), std_q, alpha)
        updated[param]["oscT"] = int(round(_ema(updated[param].get("oscT", 6), float(osc_q), alpha)))

    for param, group in per_hour_df.groupby("_field"):
        total = int(group.shape[0])
        if total == 0:
            continue
        observed = float(np.nansum(group["is_anom"])) / float(total)
        err = observed - target_rate
        if abs(err) < 0.001:
            continue
        factor = max(0.5, min(1.5, 1.0 + rate_beta * err))
        updated.setdefault(param, {"spreadT": None, "stdT": None, "oscT": 6})

        for key in ("spreadT", "stdT"):
            value = updated[param].get(key)
            if not _missing(value):
                updated[param][key] = float(max(1e-9, float(value) * factor))

        osc_t = updated[param].get("oscT", 6)
        if not _missing(osc_t):
            updated[param]["oscT"] = max(1, int(round(float(osc_t) * factor)))

    return updated


def _filter_series(frame: pd.DataFrame, key_cols: list[str], series_key: pd.Series) -> pd.DataFrame:
    if frame.empty:
        return frame
    mask = np.ones(len(frame), dtype=bool)
    for key in key_cols:
        mask &= (frame[key] == series_key[key]).to_numpy()
    return frame.loc[mask]


def _series_point(config: Config, timestamp: dt.datetime, tags: dict[str, Any], resolution: str, stat: str, field: str, value: float) -> Any:
    point = _point()(config.measurement).time(timestamp)
    for key, tag_value in tags.items():
        point = point.tag(key, _tag_value(tag_value))
    return point.tag("resolution", resolution).tag("stat", stat).field(field, float(value))


def _empty_per_hour_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "_field": pd.Series(dtype="string"),
            "hour_min": pd.Series(dtype="float64"),
            "hour_max": pd.Series(dtype="float64"),
            "spread": pd.Series(dtype="float64"),
            "std": pd.Series(dtype="float64"),
            "osc": pd.Series(dtype="int64"),
            "is_anom": pd.Series(dtype="int64"),
        }
    )


def process_hour(
    config: Config,
    ic: Any,
    hour_start: dt.datetime,
    abs_bounds: dict[str, dict[str, float | None]],
    hour_thresholds: dict[str, dict[str, float | int | None]],
) -> tuple[dict[str, Any], pd.DataFrame]:
    hour_start = hour_start.astimezone(tzutc())
    hour_end = hour_start + dt.timedelta(hours=1)
    df_1m, df_1s = _read_medium(config, ic, _iso(hour_start), _iso(hour_end))

    if df_1m.empty and df_1s.empty:
        return {"hour": _iso(hour_start), "series": 0, "anom_series": 0, "wrote": 0, "wrote_1s": 0}, _empty_per_hour_frame()

    def stat_frame(name: str) -> pd.DataFrame:
        return df_1m[df_1m["stat"] == name].copy()

    minute_min = stat_frame("min")
    minute_max = stat_frame("max")
    minute_mean = stat_frame("mean")
    minute_computed = stat_frame("computed")

    key_cols = [*config.tags, "_field"]
    series_keys = minute_computed[key_cols].drop_duplicates() if not minute_computed.empty else pd.DataFrame(columns=key_cols)

    points: list[Any] = []
    per_hour_rows: list[dict[str, Any]] = []

    wrote_1s = 0
    for _, row in df_1s.iterrows():
        timestamp = pd.to_datetime(row["_time"]).to_pydatetime()
        tags = {tag: row[tag] for tag in config.tags}
        points.append(_series_point(config, timestamp, tags, "1s", row["stat"], row["_field"], float(row["_value"])))
        wrote_1s += 1

    for _, series_key in series_keys.iterrows():
        series = _filter_series(minute_computed, key_cols, series_key)
        if series.empty:
            continue

        values = series["_value"].to_numpy(dtype=float)
        hour_min = float(np.nanmin(values))
        hour_max = float(np.nanmax(values))
        spread = hour_max - hour_min
        std = float(np.nanstd(values, ddof=0))
        osc = _oscillation_count(values)

        field = str(series_key["_field"])
        is_anom = 1 if hour_is_anomaly(field, hour_min, hour_max, spread, std, osc, abs_bounds, hour_thresholds) else 0
        per_hour_rows.append(
            {
                "_field": field,
                "hour_min": hour_min,
                "hour_max": hour_max,
                "spread": spread,
                "std": std,
                "osc": osc,
                "is_anom": is_anom,
            }
        )

        tags = {tag: series_key[tag] for tag in config.tags}

        if is_anom == 0:
            def pick(frame: pd.DataFrame) -> pd.Series:
                selected = _filter_series(frame, key_cols, series_key)
                return selected["_value"].astype(float)

            hour_min_value = float(np.nanmin(pick(minute_min))) if not minute_min.empty else hour_min
            hour_max_value = float(np.nanmax(pick(minute_max))) if not minute_max.empty else hour_max
            hour_mean_value = float(np.nanmean(pick(minute_mean))) if not minute_mean.empty else float(np.nanmean(values))

            timestamp = hour_start
            points.append(_series_point(config, timestamp, tags, "1h", "min", field, hour_min_value))
            points.append(_series_point(config, timestamp, tags, "1h", "max", field, hour_max_value))
            points.append(_series_point(config, timestamp, tags, "1h", "mean", field, hour_mean_value))
            points.append(_series_point(config, timestamp, tags, "1h", "computed", field, hour_mean_value))
        else:
            for frame in (minute_min, minute_max, minute_mean, minute_computed):
                if frame.empty:
                    continue
                subset = _filter_series(frame, key_cols, series_key)
                for _, row in subset.iterrows():
                    timestamp = pd.to_datetime(row["_time"]).to_pydatetime()
                    row_tags = {tag: row[tag] for tag in config.tags}
                    points.append(_series_point(config, timestamp, row_tags, "1m", row["stat"], row["_field"], float(row["_value"])))

    wrote = 0 if config.dry_run else _write_sync(config, ic, points)
    stats = {
        "hour": _iso(hour_start),
        "series": len(series_keys),
        "anom_series": int(np.sum([row["is_anom"] for row in per_hour_rows])) if per_hour_rows else 0,
        "wrote": wrote,
        "wrote_1s": wrote_1s,
    }
    per_hour_df = pd.DataFrame(per_hour_rows) if per_hour_rows else _empty_per_hour_frame()
    return stats, per_hour_df


def process_chunk(
    config: Config,
    ic: Any,
    start: dt.datetime,
    stop: dt.datetime,
    hour_thresholds: dict[str, dict[str, float | int | None]],
) -> tuple[dict[str, int], dict[str, dict[str, float | int | None]]]:
    cur = _floor_hour(start)
    if cur < start:
        cur += dt.timedelta(hours=1)

    totals = {"hours": 0, "series": 0, "anom_series": 0, "wrote": 0, "wrote_1s": 0}
    per_hour_frames: list[pd.DataFrame] = []

    while cur < stop:
        stats, per_hour_df = process_hour(config, ic, cur, config.abs_bounds, hour_thresholds)
        totals["hours"] += 1
        totals["series"] += int(stats["series"])
        totals["anom_series"] += int(stats["anom_series"])
        totals["wrote"] += int(stats["wrote"])
        totals["wrote_1s"] += int(stats["wrote_1s"])
        if not per_hour_df.empty:
            per_hour_frames.append(per_hour_df)
        cur += dt.timedelta(hours=1)

    if per_hour_frames:
        observed = pd.concat(per_hour_frames, ignore_index=True)
        hour_thresholds = update_hour_thresholds(
            observed,
            config.abs_bounds,
            hour_thresholds,
            config.quantile,
            config.alpha,
            config.target_anomaly_rate,
            config.rate_beta,
        )

    return totals, hour_thresholds


def run_once(config: Config) -> dict[str, Any]:
    state = load_state(config)
    if not config.dry_run:
        bucket_id = ensure_destination_bucket(config)
        if sync_destination_bucket_identity(config, state, bucket_id):
            save_state(config, state)
    start, stop = select_window(config, state)
    chunk = dt.timedelta(minutes=config.chunk_minutes)
    totals = {"chunks": 0, "hours": 0, "series": 0, "anom_series": 0, "wrote": 0, "wrote_1s": 0}
    hour_thresholds = _normalize_hour_thresholds(state.get("hour_thresholds") or {}, config.params)
    if not state.get("coverage_start_iso"):
        state["coverage_start_iso"] = _iso(start)
    else:
        existing_start = _parse_time(state["coverage_start_iso"])
        if start < existing_start:
            state["coverage_start_iso"] = _iso(start)

    log.info(
        "medium-to-long downsample | window=[%s -> %s] | src=%s dst=%s | chunk=%sm | dry_run=%s",
        _iso(start),
        _iso(stop),
        config.src_bucket,
        config.dst_bucket,
        config.chunk_minutes,
        config.dry_run,
    )
    log.info("hour thresholds initial: %s", hour_thresholds)
    log.info("absolute bounds: %s", config.abs_bounds)

    client_type = _require_influx_client()
    started_at = time.time()
    with client_type(url=config.influx_url, token=config.influx_token, org=config.influx_org) as ic:
        cur = start
        while cur < stop:
            nxt = min(cur + chunk, stop)
            stats, hour_thresholds = process_chunk(config, ic, cur, nxt, hour_thresholds)
            totals["chunks"] += 1
            for key in ("hours", "series", "anom_series", "wrote", "wrote_1s"):
                totals[key] += stats[key]

            should_advance = config.advance_empty_windows or stats["series"] > 0 or stats["wrote"] > 0 or stats["wrote_1s"] > 0
            if not config.dry_run and should_advance:
                state["hour_thresholds"] = hour_thresholds
                state["last_stop_iso"] = _iso(nxt)
                state["coverage_stop_iso"] = _iso(nxt)
                save_state(config, state)
            elif not should_advance:
                log.info("not advancing state for empty source window [%s -> %s]", _iso(cur), _iso(nxt))

            rate = stats["anom_series"] / max(1, stats["series"]) if stats["series"] else 0.0
            log.info(
                "chunk [%s -> %s] | hours=%s series=%s anom_series=%s rate=%.2f%% | wrote=%s 1s=%s | thresholds=%s",
                _iso(cur),
                _iso(nxt),
                stats["hours"],
                stats["series"],
                stats["anom_series"],
                rate * 100,
                stats["wrote"],
                stats["wrote_1s"],
                hour_thresholds,
            )
            cur = nxt
            if not config.dry_run and config.chunk_sleep_seconds > 0 and cur < stop:
                time.sleep(config.chunk_sleep_seconds)

    elapsed = round(time.time() - started_at, 2)
    overall_rate = totals["anom_series"] / max(1, totals["series"]) if totals["series"] else 0.0
    log.info(
        "downsample complete | chunks=%s hours=%s series=%s anom_series=%s rate=%.2f%% | wrote=%s 1s=%s | elapsed=%ss",
        totals["chunks"],
        totals["hours"],
        totals["series"],
        totals["anom_series"],
        overall_rate * 100,
        totals["wrote"],
        totals["wrote_1s"],
        elapsed,
    )
    attrs = {"operation.type": "background", "job": "medium_to_long", "outcome": "success"}
    telemetry.count("house_sensors.job_cycles", attributes=attrs)
    telemetry.record("house_sensors.job_duration_ms", elapsed * 1000, attrs)
    telemetry.record("house_sensors.job_chunks", totals["chunks"], attrs)
    telemetry.record("house_sensors.job_series", totals["series"], attrs)
    telemetry.record("house_sensors.job_anomalies", totals["anom_series"], attrs)
    telemetry.record("house_sensors.job_points_written", totals["wrote"], attrs)
    telemetry.record("house_sensors.job_second_points_written", totals["wrote_1s"], attrs)
    return {**totals, "elapsed_seconds": elapsed}


def run_loop(config: Config) -> None:
    while True:
        try:
            run_once(config)
        except ValueError as exc:
            log.info("no downsample work this cycle: %s", exc)
            telemetry.count("house_sensors.job_cycles", attributes={"operation.type": "background", "job": "medium_to_long", "outcome": "no_work"})
        except Exception as exc:
            log.error("downsample cycle failed: %s", exc)
            telemetry.count("house_sensors.job_cycles", attributes={"operation.type": "background", "job": "medium_to_long", "outcome": "error"})
            traceback.print_exc()
        time.sleep(config.interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Downsample medium sensor data into long-term storage")
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
