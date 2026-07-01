#!/usr/bin/env python3
"""Archive raw InfluxDB data to S3 and enforce bucket retention.

The job backs up raw InfluxDB buckets to gzipped line-protocol objects in S3.
It then deletes raw data only after that bucket has been archived and both
downsampling jobs have recorded coverage for the same time range. Medium data
is not archived; it is retained only until the medium-to-long job has covered
the retention window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import logging
import math
import os
import re
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_telemetry import telemetry_from_env
from dateutil.relativedelta import relativedelta
from dateutil.tz import tzutc

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - runtime image installs this dependency.
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment,misc]

try:
    from influxdb_client import InfluxDBClient
except ImportError:  # pragma: no cover - runtime image installs this dependency.
    InfluxDBClient = None  # type: ignore[assignment]


DEFAULT_STATE_FILE = "/state/raw_archive_cleanup_state.json"
DEFAULT_MEDIUM_STATE_FILE = "/downsampling-medium-state/raw_to_medium_state.json"
DEFAULT_LONG_STATE_FILE = "/downsampling-long-state/medium_to_long_state.json"
DEFAULT_RAW_BUCKETS = ["environment-data", "voltage-data"]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("raw_archive_cleanup")
telemetry = telemetry_from_env("house-sensors.raw-archive-cleanup")


@dataclass(frozen=True)
class Coverage:
    start: dt.datetime | None
    stop: dt.datetime | None


@dataclass(frozen=True)
class Config:
    influx_url: str
    influx_token: str
    influx_org: str
    raw_buckets: list[str]
    medium_bucket: str
    medium_measurement: str
    state_file: Path
    medium_state_file: Path
    long_state_file: Path
    s3_bucket: str
    s3_prefix: str
    aws_region: str
    create_s3_bucket: bool
    raw_retention_days: int
    medium_retention_months: int
    chunk_hours: int
    chunk_sleep_seconds: float
    interval_seconds: int
    archive_start_iso: str | None
    dry_run: bool
    delete_enabled: bool


def _split_csv(value: str | None, default: list[str]) -> list[str]:
    if not value:
        return list(default)
    return [part.strip() for part in value.split(",") if part.strip()]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_config(args: argparse.Namespace) -> Config:
    config = Config(
        influx_url=os.getenv("INFLUX_URL", ""),
        influx_token=os.getenv("INFLUX_TOKEN", ""),
        influx_org=os.getenv("INFLUX_ORG", "ahara"),
        raw_buckets=_split_csv(os.getenv("RAW_ARCHIVE_RAW_BUCKETS"), DEFAULT_RAW_BUCKETS),
        medium_bucket=os.getenv("RAW_ARCHIVE_MEDIUM_BUCKET", "sensors-medium"),
        medium_measurement=os.getenv("RAW_ARCHIVE_MEDIUM_MEASUREMENT", "sensors"),
        state_file=Path(os.getenv("RAW_ARCHIVE_STATE_FILE", DEFAULT_STATE_FILE)),
        medium_state_file=Path(os.getenv("RAW_ARCHIVE_MEDIUM_STATE_FILE", DEFAULT_MEDIUM_STATE_FILE)),
        long_state_file=Path(os.getenv("RAW_ARCHIVE_LONG_STATE_FILE", DEFAULT_LONG_STATE_FILE)),
        s3_bucket=os.getenv("RAW_ARCHIVE_S3_BUCKET", ""),
        s3_prefix=os.getenv("RAW_ARCHIVE_S3_PREFIX", "house-sensors/raw").strip("/"),
        aws_region=os.getenv("RAW_ARCHIVE_AWS_REGION") or os.getenv("AWS_REGION", "us-east-1"),
        create_s3_bucket=_as_bool(os.getenv("RAW_ARCHIVE_CREATE_S3_BUCKET"), False),
        raw_retention_days=int(os.getenv("RAW_ARCHIVE_RAW_RETENTION_DAYS", "30")),
        medium_retention_months=int(os.getenv("RAW_ARCHIVE_MEDIUM_RETENTION_MONTHS", "6")),
        chunk_hours=int(os.getenv("RAW_ARCHIVE_CHUNK_HOURS", "24")),
        chunk_sleep_seconds=float(os.getenv("RAW_ARCHIVE_CHUNK_SLEEP_SECONDS", "0")),
        interval_seconds=int(os.getenv("RAW_ARCHIVE_INTERVAL_SECONDS", "3600")),
        archive_start_iso=args.start or os.getenv("RAW_ARCHIVE_START_ISO"),
        dry_run=args.dry_run or _as_bool(os.getenv("RAW_ARCHIVE_DRY_RUN")),
        delete_enabled=_as_bool(os.getenv("RAW_ARCHIVE_DELETE_ENABLED"), True),
    )

    required_env = {
        "INFLUX_URL": config.influx_url,
        "INFLUX_TOKEN": config.influx_token,
        "INFLUX_ORG": config.influx_org,
        "RAW_ARCHIVE_S3_BUCKET": config.s3_bucket,
    }
    missing = [name for name, value in required_env.items() if not value]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
    if config.raw_retention_days <= 0:
        raise ValueError("RAW_ARCHIVE_RAW_RETENTION_DAYS must be positive")
    if config.medium_retention_months <= 0:
        raise ValueError("RAW_ARCHIVE_MEDIUM_RETENTION_MONTHS must be positive")
    if config.chunk_hours <= 0:
        raise ValueError("RAW_ARCHIVE_CHUNK_HOURS must be positive")
    if config.chunk_sleep_seconds < 0:
        raise ValueError("RAW_ARCHIVE_CHUNK_SLEEP_SECONDS cannot be negative")
    if config.interval_seconds <= 0:
        raise ValueError("RAW_ARCHIVE_INTERVAL_SECONDS must be positive")
    return config


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzutc())
    return parsed.astimezone(tzutc())


def _iso(value: dt.datetime) -> str:
    return value.astimezone(tzutc()).isoformat().replace("+00:00", "Z")


def _floor_hour(value: dt.datetime) -> dt.datetime:
    return value.astimezone(tzutc()).replace(minute=0, second=0, microsecond=0)


def _json_read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"State file is not valid JSON: {path}") from exc


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "raw_exports": raw.get("raw_exports") or {},
        "raw_deletes": raw.get("raw_deletes") or {},
        "medium_delete": raw.get("medium_delete") or {},
    }


def load_state(config: Config) -> dict[str, Any]:
    return _normalize_state(_json_read(config.state_file))


def save_state(config: Config, state: dict[str, Any]) -> None:
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    payload = _normalize_state(state)
    payload["updated_at"] = _iso(dt.datetime.now(tz=tzutc()))
    tmp_path = config.state_file.with_suffix(config.state_file.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp_path.replace(config.state_file)


def load_coverage(path: Path) -> Coverage:
    raw = _json_read(path)
    start = raw.get("coverage_start_iso")
    stop = raw.get("coverage_stop_iso") or raw.get("last_stop_iso")
    return Coverage(
        start=_parse_time(start) if start else None,
        stop=_parse_time(stop) if stop else None,
    )


def _require_influx_client() -> type:
    if InfluxDBClient is None:
        raise RuntimeError("influxdb-client is required at runtime")
    return InfluxDBClient


def _require_boto3() -> Any:
    if boto3 is None:
        raise RuntimeError("boto3 is required at runtime")
    return boto3


def _s3_client(config: Config) -> Any:
    return _require_boto3().client("s3", region_name=config.aws_region)


def ensure_s3_bucket(config: Config, s3: Any) -> None:
    if not config.create_s3_bucket:
        return
    try:
        s3.head_bucket(Bucket=config.s3_bucket)
        log.info("raw archive bucket exists: %s", config.s3_bucket)
        return
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise

    create_args: dict[str, Any] = {"Bucket": config.s3_bucket}
    if config.aws_region != "us-east-1":
        create_args["CreateBucketConfiguration"] = {"LocationConstraint": config.aws_region}
    s3.create_bucket(**create_args)
    s3.put_public_access_block(
        Bucket=config.s3_bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=config.s3_bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {
                        "SSEAlgorithm": "AES256",
                    },
                }
            ],
        },
    )
    log.info("raw archive bucket created: %s", config.s3_bucket)


def _flux_string(value: str) -> str:
    return json.dumps(value)


def _line_escape(value: str, chars: str) -> str:
    escaped = str(value).replace("\\", "\\\\")
    for char in chars:
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _field_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value}i"
    if isinstance(value, float):
        if math.isnan(value):
            raise ValueError("NaN cannot be represented in Influx line protocol")
        return repr(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _timestamp_ns(value: dt.datetime) -> int:
    timestamp = value.astimezone(tzutc()).timestamp()
    return int(round(timestamp * 1_000_000_000))


def record_to_line_protocol(record: Any) -> str:
    values = dict(record.values)
    measurement = values.get("_measurement")
    field = values.get("_field")
    value = record.get_value()
    timestamp = record.get_time()
    if measurement is None or field is None or value is None or timestamp is None:
        raise ValueError("Influx record is missing required line protocol fields")

    tags = {
        key: tag_value
        for key, tag_value in values.items()
        if not key.startswith("_") and key not in {"result", "table"} and tag_value is not None
    }
    tag_text = "".join(
        f",{_line_escape(key, ',= ')}={_line_escape(str(tags[key]), ',= ')}"
        for key in sorted(tags)
    )
    return (
        f"{_line_escape(str(measurement), ', ')}{tag_text} "
        f"{_line_escape(str(field), ',= ')}={_field_value(value)} "
        f"{_timestamp_ns(timestamp)}"
    )


def _archive_key(config: Config, bucket: str, start: dt.datetime, stop: dt.datetime) -> str:
    safe_bucket = re.sub(r"[^0-9A-Za-z_.=-]+", "_", bucket).strip("_")
    start_part = start.strftime("%Y%m%dT%H%M%SZ")
    stop_part = stop.strftime("%Y%m%dT%H%M%SZ")
    return (
        f"{config.s3_prefix}/{safe_bucket}/"
        f"year={start:%Y}/month={start:%m}/day={start:%d}/"
        f"{start_part}_{stop_part}.lp.gz"
    )


def discover_bucket_start(config: Config, ic: Any, bucket: str, stop: dt.datetime) -> dt.datetime:
    if config.archive_start_iso:
        return _floor_hour(_parse_time(config.archive_start_iso))

    flux = f"""
from(bucket: {_flux_string(bucket)})
  |> range(start: 0, stop: time(v: {_flux_string(_iso(stop))}))
  |> keep(columns: ["_time"])
  |> sort(columns: ["_time"])
  |> limit(n: 1)
"""
    for table in ic.query_api().query(flux):
        for record in table.records:
            return _floor_hour(record.get_time())
    return stop


def export_raw_chunk(config: Config, ic: Any, s3: Any, bucket: str, start: dt.datetime, stop: dt.datetime) -> int:
    flux = f"""
from(bucket: {_flux_string(bucket)})
  |> range(start: time(v: {_flux_string(_iso(start))}), stop: time(v: {_flux_string(_iso(stop))}))
"""
    rows = 0
    with tempfile.NamedTemporaryFile("wb", suffix=".lp.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)

    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as output:
            for record in ic.query_api().query_stream(flux):
                output.write(record_to_line_protocol(record))
                output.write("\n")
                rows += 1

        if rows == 0:
            log.info("raw archive chunk empty | bucket=%s window=[%s -> %s]", bucket, _iso(start), _iso(stop))
            return 0

        key = _archive_key(config, bucket, start, stop)
        if config.dry_run:
            log.info("dry-run raw archive upload skipped | bucket=%s rows=%s key=s3://%s/%s", bucket, rows, config.s3_bucket, key)
            return rows

        s3.upload_file(
            str(tmp_path),
            config.s3_bucket,
            key,
            ExtraArgs={
                "ContentType": "text/plain",
                "ContentEncoding": "gzip",
                "Metadata": {
                    "influx-bucket": bucket,
                    "start": _iso(start),
                    "stop": _iso(stop),
                    "format": "influx-line-protocol",
                },
            },
        )
        log.info("raw archive uploaded | bucket=%s rows=%s key=s3://%s/%s", bucket, rows, config.s3_bucket, key)
        return rows
    finally:
        tmp_path.unlink(missing_ok=True)


def raw_export_stop(config: Config, now: dt.datetime, medium: Coverage, long: Coverage) -> dt.datetime | None:
    if medium.stop is None or long.stop is None:
        return None
    retention_stop = _floor_hour(now - dt.timedelta(days=config.raw_retention_days))
    return min(retention_stop, _floor_hour(medium.stop), _floor_hour(long.stop))


def medium_cleanup_stop(config: Config, now: dt.datetime, long: Coverage) -> dt.datetime | None:
    if long.stop is None:
        return None
    retention_stop = _floor_hour(now - relativedelta(months=config.medium_retention_months))
    return min(retention_stop, _floor_hour(long.stop))


def coverage_gated_start(base_start: dt.datetime, *coverages: Coverage) -> dt.datetime | None:
    starts = [coverage.start for coverage in coverages]
    if any(start is None for start in starts):
        return None
    return max([base_start, *(start for start in starts if start is not None)])


def _state_stop(state: dict[str, Any], section: str, name: str, fallback: dt.datetime) -> dt.datetime:
    raw_value = ((state.get(section) or {}).get(name) or {}).get("last_stop_iso")
    return _parse_time(raw_value) if raw_value else fallback


def _set_state_stop(state: dict[str, Any], section: str, name: str, stop: dt.datetime) -> None:
    state.setdefault(section, {})
    state[section].setdefault(name, {})
    state[section][name]["last_stop_iso"] = _iso(stop)


def _influx_api(config: Config, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
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
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"InfluxDB API {method} {path} failed with HTTP {exc.code}: {detail}") from exc


def delete_window(config: Config, bucket: str, start: dt.datetime, stop: dt.datetime, predicate: str = "") -> bool:
    if not config.delete_enabled:
        log.info("delete disabled | bucket=%s window=[%s -> %s]", bucket, _iso(start), _iso(stop))
        return False
    if config.dry_run:
        log.info("dry-run delete skipped | bucket=%s window=[%s -> %s] predicate=%s", bucket, _iso(start), _iso(stop), predicate)
        return False

    query = urllib.parse.urlencode({"org": config.influx_org, "bucket": bucket})
    _influx_api(
        config,
        "POST",
        f"/api/v2/delete?{query}",
        {
            "start": _iso(start),
            "stop": _iso(stop),
            "predicate": predicate,
        },
    )
    log.info("deleted Influx window | bucket=%s window=[%s -> %s] predicate=%s", bucket, _iso(start), _iso(stop), predicate)
    return True


def archive_raw(config: Config, state: dict[str, Any], ic: Any, s3: Any, stop: dt.datetime) -> dict[str, int]:
    chunk = dt.timedelta(hours=config.chunk_hours)
    totals: dict[str, int] = {"chunks": 0, "rows": 0}

    for bucket in config.raw_buckets:
        export_info = (state.get("raw_exports") or {}).get(bucket) or {}
        if export_info.get("last_stop_iso"):
            start = _parse_time(export_info["last_stop_iso"])
        else:
            start = discover_bucket_start(config, ic, bucket, stop)
        start = _floor_hour(start)
        if start >= stop:
            log.info("raw archive current | bucket=%s watermark=%s stop=%s", bucket, _iso(start), _iso(stop))
            continue

        cur = start
        while cur < stop:
            nxt = min(cur + chunk, stop)
            rows = export_raw_chunk(config, ic, s3, bucket, cur, nxt)
            totals["chunks"] += 1
            totals["rows"] += rows
            if not config.dry_run:
                _set_state_stop(state, "raw_exports", bucket, nxt)
                save_state(config, state)
            cur = nxt
            if not config.dry_run and config.chunk_sleep_seconds > 0 and cur < stop:
                time.sleep(config.chunk_sleep_seconds)

    return totals


def cleanup_raw(config: Config, state: dict[str, Any], medium: Coverage, long: Coverage) -> int:
    deleted = 0
    for bucket in config.raw_buckets:
        export_info = (state.get("raw_exports") or {}).get(bucket) or {}
        exported_stop = export_info.get("last_stop_iso")
        if not exported_stop:
            log.info("raw cleanup waiting for archive watermark | bucket=%s", bucket)
            continue

        exported_stop_dt = _parse_time(exported_stop)
        base_start = _state_stop(state, "raw_deletes", bucket, _parse_time(export_info.get("start_iso", "1970-01-01T00:00:00Z")))
        start = coverage_gated_start(base_start, medium, long)
        if start is None:
            log.info("raw cleanup waiting for downsampling coverage start | bucket=%s", bucket)
            continue
        stop = min(exported_stop_dt, _floor_hour(medium.stop or exported_stop_dt), _floor_hour(long.stop or exported_stop_dt))
        if start >= stop:
            continue

        if delete_window(config, bucket, start, stop):
            _set_state_stop(state, "raw_deletes", bucket, stop)
            save_state(config, state)
            deleted += 1
    return deleted


def cleanup_medium(config: Config, state: dict[str, Any], long: Coverage, stop: dt.datetime | None) -> int:
    if stop is None:
        log.info("medium cleanup waiting for long downsampling watermark")
        return 0
    base_start = _parse_time(((state.get("medium_delete") or {}).get(config.medium_bucket) or {}).get("last_stop_iso", "1970-01-01T00:00:00Z"))
    start = coverage_gated_start(base_start, long)
    if start is None:
        log.info("medium cleanup waiting for long downsampling coverage start")
        return 0
    if start >= stop:
        return 0

    predicate = f'_measurement="{config.medium_measurement}"'
    if delete_window(config, config.medium_bucket, start, stop, predicate):
        _set_state_stop(state, "medium_delete", config.medium_bucket, stop)
        save_state(config, state)
        return 1
    return 0


def run_once(config: Config) -> dict[str, Any]:
    started_at = time.time()
    state = load_state(config)
    medium = load_coverage(config.medium_state_file)
    long = load_coverage(config.long_state_file)
    now = dt.datetime.now(tz=tzutc())
    raw_stop = raw_export_stop(config, now, medium, long)
    medium_stop = medium_cleanup_stop(config, now, long)
    totals = {"archived_chunks": 0, "archived_rows": 0, "raw_delete_windows": 0, "medium_delete_windows": 0}

    log.info(
        "raw archive cleanup | raw_stop=%s medium_stop=%s medium_coverage=%s long_coverage=%s dry_run=%s delete=%s",
        _iso(raw_stop) if raw_stop else "waiting",
        _iso(medium_stop) if medium_stop else "waiting",
        medium,
        long,
        config.dry_run,
        config.delete_enabled,
    )

    client_type = _require_influx_client()
    s3 = None if config.dry_run else _s3_client(config)
    if not config.dry_run:
        ensure_s3_bucket(config, s3)

    with client_type(url=config.influx_url, token=config.influx_token, org=config.influx_org) as ic:
        if raw_stop is not None:
            archive_stats = archive_raw(config, state, ic, s3, raw_stop)
            totals["archived_chunks"] += archive_stats["chunks"]
            totals["archived_rows"] += archive_stats["rows"]
        else:
            log.info("raw archive waiting for downsampling watermarks")

        totals["raw_delete_windows"] += cleanup_raw(config, state, medium, long)
        totals["medium_delete_windows"] += cleanup_medium(config, state, long, medium_stop)

    log.info("raw archive cleanup complete | %s", totals)
    elapsed_ms = (time.time() - started_at) * 1000
    attrs = {"operation.type": "background", "job": "raw_archive_cleanup", "outcome": "success"}
    telemetry.count("house_sensors.job_cycles", attributes=attrs)
    telemetry.record("house_sensors.job_duration_ms", elapsed_ms, attrs)
    telemetry.record("house_sensors.archive_chunks", totals["archived_chunks"], attrs)
    telemetry.record("house_sensors.archive_rows", totals["archived_rows"], attrs)
    telemetry.record("house_sensors.delete_windows", totals["raw_delete_windows"] + totals["medium_delete_windows"], attrs)
    return totals


def run_loop(config: Config) -> None:
    while True:
        try:
            run_once(config)
        except Exception as exc:
            log.error("raw archive cleanup cycle failed: %s", exc)
            telemetry.count("house_sensors.job_cycles", attributes={"operation.type": "background", "job": "raw_archive_cleanup", "outcome": "error"})
            traceback.print_exc()
        time.sleep(config.interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Archive raw InfluxDB data to S3 and clean retained buckets")
    parser.add_argument("mode", nargs="?", choices=["run-once", "run-loop"], default=os.getenv("RAW_ARCHIVE_MODE", "run-once"))
    parser.add_argument("--start", help="override initial raw archive start ISO timestamp")
    parser.add_argument("--dry-run", action="store_true", help="plan archive and delete work without uploads, deletes, or state writes")
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
