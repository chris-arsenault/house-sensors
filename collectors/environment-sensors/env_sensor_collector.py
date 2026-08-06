#!/usr/bin/env python3
"""
Environment reading drain -> InfluxDB.

Drains the `envSensors` stream of the ahara-collector appliance (the host
that discovers and polls the AtomS3U devices on the home LAN) and maps
each device-native reading envelope to the `environment` measurement.
This collector owns the schema: measurement and field names, firmware key
aliases, and the corrected-sample audit field all live here
(ahara-collector ADR-0006); the appliance never emits a storage name.

Envelope contract (ahara-collector docs/integration.md):
  {"module": "envSensors",
   "device": {"ip": ..., "name": ..., "model": ..., "deviceId": ..., "tags": {...}},
   "timestampNs": <measurement time, sample-age corrected on the appliance>,
   "values": {<the device /sensors payload, verbatim>}}
"""

import json
import logging
import os
import signal
import threading
from typing import Dict, Optional

import requests
from app_telemetry import telemetry_from_env

# -----------------------------
# Environment / configuration
# -----------------------------
COLLECTOR_URL = os.getenv("COLLECTOR_URL", "http://192.168.65.10:8850")
COLLECTOR_TOKEN = os.getenv("COLLECTOR_TOKEN", "")
COLLECTOR_MODULE = os.getenv("COLLECTOR_MODULE", "envSensors")
DRAIN_INTERVAL_SECONDS = float(os.getenv("DRAIN_INTERVAL_SECONDS", "10"))
DRAIN_TIMEOUT_SECONDS = float(os.getenv("DRAIN_TIMEOUT_SECONDS", "10"))

# InfluxDB v2
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "")
INFLUX_PRECISION = os.getenv("INFLUX_PRECISION", "ns")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Measurement / tags
MEASUREMENT = os.getenv("INFLUX_MEASUREMENT", "environment")
TAG_DEVICE_KEY = os.getenv("TAG_DEVICE_KEY", "device")
TAG_IP_KEY = os.getenv("TAG_IP_KEY", "ip")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
logger = logging.getLogger("env-drain")
telemetry = telemetry_from_env("house-sensors.environment-sensors")

shutdown_event = threading.Event()


def signal_handler(sig, frame):
    logger.info("Received shutdown signal. Stopping...")
    shutdown_event.set()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def convert_ts_precision(ts_ns: int, precision: str) -> int:
    if precision == "ns":
        return ts_ns
    if precision == "us":
        return ts_ns // 1_000
    if precision == "ms":
        return ts_ns // 1_000_000
    # seconds
    return ts_ns // 1_000_000_000


def escape_tag(value: str) -> str:
    return str(value).replace(",", r"\,").replace(" ", r"\ ").replace("=", r"\=")


def escape_str_field(value: str) -> str:
    # Influx line protocol: string field values must be in double quotes with inner quotes escaped
    return '"' + str(value).replace("\\", "\\\\").replace('"', r'\"') + '"'


def influx_write(session: requests.Session, lines: str) -> bool:
    line_count = len([line for line in lines.splitlines() if line.strip()])
    if not (INFLUX_URL and INFLUX_TOKEN and INFLUX_ORG and INFLUX_BUCKET):
        logger.error("InfluxDB env not fully configured (INFLUX_URL/TOKEN/ORG/BUCKET). Skipping write.")
        telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "system", "outcome": "misconfigured"})
        return False

    url = f"{INFLUX_URL.rstrip('/')}/api/v2/write"
    params = {"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": INFLUX_PRECISION}
    headers = {"Authorization": f"Token {INFLUX_TOKEN}", "Content-Type": "text/plain; charset=utf-8"}
    try:
        r = session.post(url, params=params, headers=headers, data=lines.encode("utf-8"), timeout=5)
        if r.status_code not in (204, 200):
            logger.warning("Influx write failed: %s %s", r.status_code, r.text[:300])
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "drain", "outcome": "error"})
            return False
        telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "drain", "outcome": "success"})
        telemetry.record("house_sensors.influx_write_lines", line_count, {"operation.type": "drain"})
        return True
    except Exception as e:
        logger.warning("Influx write exception: %s", e)
        telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "drain", "outcome": "error"})
        return False


# -----------------------------
# Envelope mapping
# -----------------------------
def extract_kvp_tags(tags_field) -> Dict[str, str]:
    """
    Accepts multiple shapes:
      - dict: {"room":"kitchen","zone":"north"}
      - list of dicts: [{"room":"kitchen"},{"zone":"north"}]
      - list of strings with '=': ["room=kitchen","zone=north"]
    Invalid entries are ignored.
    """
    out: Dict[str, str] = {}
    if isinstance(tags_field, dict):
        for k, v in tags_field.items():
            out[str(k)] = str(v)
    elif isinstance(tags_field, list):
        for item in tags_field:
            if isinstance(item, dict):
                for k, v in item.items():
                    out[str(k)] = str(v)
            elif isinstance(item, str) and "=" in item:
                k, v = item.split("=", 1)
                out[str(k)] = str(v)
    return out


def envelope_to_line(envelope: Dict) -> Optional[str]:
    """
    One reading envelope -> one `environment` line.
      Tags:
        - TAG_DEVICE_KEY (friendly name), ip
        - model/device_id when the device reported them
        - all kvp from the device's user tags
      Fields:
        - temperature_c, temperature_f, humidity, pressure_pa, pressure_hpa
          (accepting the bare `temperature`/`pressure` firmware variants)
        - timestamp_ms, sample_age_ms (numeric, if present)
        - timestamp_iso (string, if present)
        - sample_time_corrected_ms (from the envelope timestamp, for audit)
      Timestamp:
        - the envelope's timestampNs (sample-age corrected on the appliance)
    """
    device = envelope.get("device") or {}
    values = envelope.get("values") or {}
    timestamp_ns = envelope.get("timestampNs")
    if not isinstance(device, dict) or not isinstance(values, dict):
        return None
    if not isinstance(timestamp_ns, int):
        return None

    # Merge nested "sensors" if a firmware variant nests its readings
    if "sensors" in values and isinstance(values["sensors"], dict):
        values = {**values, **values["sensors"]}

    ip = device.get("ip")
    if not ip:
        return None

    tag_items = {
        TAG_DEVICE_KEY: device.get("name", ip),
        TAG_IP_KEY: ip,
    }
    if device.get("model") is not None:
        tag_items["model"] = str(device["model"])
    if device.get("deviceId") is not None:
        tag_items["device_id"] = str(device["deviceId"])
    tag_items.update(extract_kvp_tags(device.get("tags")))

    numeric_fields = {}
    string_fields = {}

    def try_add_numeric(key: str, source_key: Optional[str] = None):
        k = source_key or key
        if k in values and values[k] is not None:
            try:
                numeric_fields[key] = float(values[k])
            except Exception:
                # if not numeric, drop; we don't auto-cast to string for known numeric keys
                pass

    for k in ("temperature_c", "temperature_f", "humidity", "pressure_pa", "pressure_hpa"):
        try_add_numeric(k)

    # Firmware variants report bare names
    if "temperature" in values and "temperature_c" not in numeric_fields:
        try_add_numeric("temperature_c", "temperature")
    if "pressure" in values and "pressure_pa" not in numeric_fields:
        try_add_numeric("pressure_pa", "pressure")

    try_add_numeric("timestamp_ms")
    try_add_numeric("sample_age_ms")

    if "timestamp_iso" in values and values["timestamp_iso"] is not None:
        string_fields["timestamp_iso"] = str(values["timestamp_iso"])

    # No fields? bail
    if not numeric_fields and not string_fields:
        return None

    # Corrected measurement time as an audit field, ms since epoch
    numeric_fields["sample_time_corrected_ms"] = float(timestamp_ns // 1_000_000)

    tag_str = ",".join(f"{escape_tag(str(k))}={escape_tag(str(v))}" for k, v in sorted(tag_items.items()))
    field_parts = []
    for k, v in sorted(numeric_fields.items()):
        field_parts.append(f"{k}={v}")
    for k, v in sorted(string_fields.items()):
        field_parts.append(f"{k}={escape_str_field(v)}")

    ts = convert_ts_precision(timestamp_ns, INFLUX_PRECISION)
    return f"{MEASUREMENT},{tag_str} {','.join(field_parts)} {ts}"


def map_batch(lines_text: str) -> tuple[list, int]:
    """Map a batch's envelope lines; returns (lines, skipped_count)."""
    lines = []
    skipped = 0
    for raw in lines_text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            envelope = json.loads(raw)
        except Exception:
            skipped += 1
            continue
        line = envelope_to_line(envelope) if isinstance(envelope, dict) else None
        if line:
            lines.append(line)
        else:
            skipped += 1
    return lines, skipped


# -----------------------------
# Drain loop
# -----------------------------
def drain_once(collector_sess: requests.Session, write_sess: requests.Session) -> bool:
    """One drain cycle. Returns True when a batch was fully processed and
    acked (the caller retries immediately to work through a backlog)."""
    url = f"{COLLECTOR_URL.rstrip('/')}/readings/next"
    try:
        r = collector_sess.get(url, params={"module": COLLECTOR_MODULE}, timeout=DRAIN_TIMEOUT_SECONDS)
    except Exception as e:
        logger.warning("Collector fetch failed: %s", e)
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
        return False
    if r.status_code == 204:
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "empty"})
        return False
    if r.status_code != 200:
        logger.warning("Collector fetch failed: HTTP %s %s", r.status_code, r.text[:200])
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
        return False

    try:
        doc = r.json()
        batch_id = doc["batchId"]
        lines, skipped = map_batch(doc.get("lines", ""))
    except Exception as e:
        logger.warning("Collector batch unparseable: %s", e)
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
        return False

    if skipped:
        logger.warning("Skipped %d unmappable envelope(s) in batch %s", skipped, batch_id)
        telemetry.record("house_sensors.drain_skipped_envelopes", skipped, {"operation.type": "drain"})

    # Write before ack: an unacked batch is re-served, and duplicate
    # writes are idempotent per measurement/tags/timestamp.
    if lines and not influx_write(write_sess, "\n".join(lines)):
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "write_failed"})
        return False

    try:
        ack = collector_sess.post(
            f"{COLLECTOR_URL.rstrip('/')}/readings/ack",
            json={"module": COLLECTOR_MODULE, "batchId": batch_id},
            timeout=DRAIN_TIMEOUT_SECONDS,
        )
        if ack.status_code != 200:
            logger.warning("Ack failed: HTTP %s", ack.status_code)
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "ack_failed"})
            return False
    except Exception as e:
        logger.warning("Ack failed: %s", e)
        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "ack_failed"})
        return False

    logger.info("Drained batch %s: %d line(s) written", batch_id, len(lines))
    telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "success"})
    telemetry.record("house_sensors.drain_batch_lines", len(lines), {"operation.type": "drain"})
    return True


def main():
    if not INFLUX_TOKEN or not INFLUX_BUCKET or not INFLUX_ORG:
        logger.warning("InfluxDB env not fully set. Set INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET.")
    if not COLLECTOR_TOKEN:
        logger.warning("COLLECTOR_TOKEN not set; the collector will refuse the drain.")

    collector_sess = requests.Session()
    collector_sess.headers["Authorization"] = f"Bearer {COLLECTOR_TOKEN}"
    write_sess = requests.Session()

    logger.info(
        "Draining %s stream from %s every %.1fs when idle. Precision=%s",
        COLLECTOR_MODULE, COLLECTOR_URL, DRAIN_INTERVAL_SECONDS, INFLUX_PRECISION,
    )

    while not shutdown_event.is_set():
        if not drain_once(collector_sess, write_sess):
            shutdown_event.wait(DRAIN_INTERVAL_SECONDS)

    logger.info("Shutting down...")


if __name__ == "__main__":
    main()
