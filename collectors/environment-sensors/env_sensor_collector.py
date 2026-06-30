#!/usr/bin/env python3
"""
IoT discovery + sensor poller -> InfluxDB (with full field/tag support and sample time correction)

Adds:
- Writes ALL provided fields from device payload:
  temperature_c, temperature_f, humidity, pressure_pa, pressure_hpa,
  device, model, device_id, timestamp_ms, timestamp_iso, tags (kvp), sample_age_ms
- Uses sample_age_ms to correct the point timestamp (server_now - sample_age_ms)
- Also writes sample_time_corrected_ms (field) for auditability
- Merges device/model/device_id and 'tags' into Influx tags
"""

import json
import logging
import os
import signal
import socket
import threading
import time
from typing import Dict, Optional, Tuple

import requests
from app_telemetry import telemetry_from_env
from requests.auth import HTTPBasicAuth

# -----------------------------
# Environment / configuration
# -----------------------------
DISCOVERY_ADDRESS = os.getenv("DISCOVERY_ADDRESS", "192.168.66.255")
DISCOVERY_PORT = int(os.getenv("DISCOVERY_PORT", "12343"))
DISCOVERY_PAYLOAD = os.getenv("DISCOVERY_PAYLOAD", '{"discover":true}')

DISCOVERY_TIMEOUT_SECONDS = float(os.getenv("DISCOVERY_TIMEOUT_SECONDS", "10.0"))
DISCOVERY_INTERVAL_HOURS = float(os.getenv("DISCOVERY_INTERVAL_HOURS", "4"))

DEVICE_USER = os.getenv("DEVICE_USER", "")
DEVICE_PASS = os.getenv("DEVICE_PASS", "")
SENSORS_PATH = os.getenv("SENSORS_PATH", "/sensors")
DEFAULT_HTTP_SCHEME = os.getenv("DEVICE_HTTP_SCHEME", "http")
DEFAULT_HTTP_PORT = int(os.getenv("DEVICE_HTTP_PORT", "80"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "1"))

# InfluxDB v2
INFLUX_URL = os.getenv("INFLUX_URL", "http://localhost:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "")
INFLUX_PRECISION = os.getenv("INFLUX_PRECISION", "ns")  # use 'ns' for best alignment with corrected times

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
logger = logging.getLogger("iot-poller")
telemetry = telemetry_from_env("house-sensors.environment-sensors")

shutdown_event = threading.Event()
devices_lock = threading.Lock()

# devices[ip] -> dict with ip, name, http_port, scheme
devices: Dict[str, Dict] = {}

# -----------------------------
# Utilities
# -----------------------------
def signal_handler(sig, frame):
    logger.info("Received shutdown signal. Stopping...")
    shutdown_event.set()


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def now_ns() -> int:
    return time.time_ns()


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


def safe_get(d: Dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


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
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "polling", "outcome": "error"})
            return False
        telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "polling", "outcome": "success"})
        telemetry.record("house_sensors.influx_write_lines", line_count, {"operation.type": "polling"})
        return True
    except Exception as e:
        logger.warning("Influx write exception: %s", e)
        telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "polling", "outcome": "error"})
        return False

# -----------------------------
# Discovery
# -----------------------------
def build_device_from_reply(addr: Tuple[str, int], payload: bytes) -> Optional[Dict]:
    ip = addr[0]
    try:
        data = json.loads(payload.decode("utf-8", errors="ignore"))
    except Exception:
        data = {}
    name = data.get("deviceId") or data.get("device") or data.get("id") or ip
    http_port = int(data.get("http_port") or data.get("port") or DEFAULT_HTTP_PORT)
    scheme = (data.get("scheme") or DEFAULT_HTTP_SCHEME).lower()
    return {"ip": ip, "name": str(name), "http_port": http_port, "scheme": scheme}


def run_discovery() -> Dict[str, Dict]:
    started_at = time.monotonic()
    logger.info("Starting discovery: UDP -> %s:%d (timeout=%.1fs)", DISCOVERY_ADDRESS, DISCOVERY_PORT, DISCOVERY_TIMEOUT_SECONDS)
    discovered: Dict[str, Dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("", 0))
    sock.settimeout(DISCOVERY_TIMEOUT_SECONDS)

    try:
        sock.sendto(DISCOVERY_PAYLOAD.encode("utf-8"), (DISCOVERY_ADDRESS, DISCOVERY_PORT))
        start = time.monotonic()
        while True:
            remaining = DISCOVERY_TIMEOUT_SECONDS - (time.monotonic() - start)
            if remaining <= 0:
                break
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                break
            except Exception as e:
                logger.warning("Discovery recv error: %s", e)
                break
            print(data)
            d = build_device_from_reply(addr, data)
            if d and d["ip"] not in discovered:
                discovered[d["ip"]] = d
                logger.info("Discovered device: %s (name=%s, port=%s, scheme=%s)", d["ip"], d.get("name"), d.get("http_port"), d.get("scheme"))
    finally:
        sock.close()

    if not discovered:
        logger.warning("Discovery completed: no devices found.")
    else:
        logger.info("Discovery completed: %d device(s) found.", len(discovered))
    telemetry.count("house_sensors.discovery_runs", attributes={"operation.type": "background", "outcome": "success"})
    telemetry.record("house_sensors.discovery_duration_ms", (time.monotonic() - started_at) * 1000, {"operation.type": "background"})
    telemetry.record("house_sensors.discovered_devices", len(discovered), {"operation.type": "background"})
    return discovered

# -----------------------------
# Polling helpers
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


def validate_device(session: requests.Session, d: Dict) -> bool:
    url = f"{d.get('scheme','http')}://{d['ip']}:{d.get('http_port', DEFAULT_HTTP_PORT)}{SENSORS_PATH}"
    try:
        r = session.get(url, timeout=3)
        if r.status_code != 200:
            print(f"non 200 response {r.status_code}, {r}")
            telemetry.count("house_sensors.device_validation", attributes={"operation.type": "background", "outcome": "error"})
            return False
        print(r)
        js = r.json()
        # Consider valid if any of the expected fields exist
        keys = set(js.keys())
        ok = any(k in keys for k in (
            "temperature", "pressure", "humidity",
            "temperature_c", "temperature_f", "pressure_pa", "pressure_hpa"
        ))
        if not ok and "sensors" in js and isinstance(js["sensors"], dict):
            keys = set(js["sensors"].keys())
            ok = any(k in keys for k in (
                "temperature", "pressure", "humidity",
                "temperature_c", "temperature_f", "pressure_pa", "pressure_hpa"
            ))
        telemetry.count("house_sensors.device_validation", attributes={"operation.type": "background", "outcome": "success" if ok else "invalid"})
        return ok
    except Exception as e:
        print(f"exception in validate {e}")
        telemetry.count("house_sensors.device_validation", attributes={"operation.type": "background", "outcome": "error"})
        return False


def build_line_protocol(d: Dict, reading: Dict, corrected_ts_ns: int) -> Optional[str]:
    """
    Build Influx line with:
      Tags:
        - device/model/device_id (if present in payload)
        - ip (from discovery)
        - TAG_DEVICE_KEY (friendly name)
        - all kvp from 'tags'
      Fields:
        - temperature_c, temperature_f, humidity, pressure_pa, pressure_hpa
        - timestamp_ms (as numeric, if present)
        - timestamp_iso (as string, if present)
        - sample_age_ms (as numeric, if present)
        - sample_time_corrected_ms (computed from corrected_ts_ns)
    Timestamp:
      - set to corrected_ts_ns converted to INFLUX_PRECISION
    """
    # Merge nested "sensors" if present
    if "sensors" in reading and isinstance(reading["sensors"], dict):
        reading = {**reading, **reading["sensors"]}

    # Tags
    tag_items = {}

    # friendly name + ip
    tag_items[TAG_DEVICE_KEY] = d.get("name", d["ip"])
    tag_items[TAG_IP_KEY] = d["ip"]

    # device/model/device_id from payload (as tags)
    for tk in ("device", "model", "device_id"):
        if tk in reading and reading[tk] is not None:
            tag_items[tk] = str(reading[tk])

    # user-provided tags
    tag_items.update(extract_kvp_tags(reading.get("tags")))

    # Fields: numeric where possible, timestamp_iso as string
    numeric_fields = {}
    string_fields = {}

    def try_add_numeric(key: str, source_key: Optional[str] = None):
        k = source_key or key
        if k in reading and reading[k] is not None:
            try:
                numeric_fields[key] = float(reading[k])
            except Exception:
                # if not numeric, drop; we don't auto-cast to string for known numeric keys
                pass

    # Preferred explicit keys from your payload
    for k in ("temperature_c", "temperature_f", "humidity", "pressure_pa", "pressure_hpa"):
        try_add_numeric(k)

    # Backward-compat: if only generic keys exist
    if "temperature" in reading and "temperature_c" not in numeric_fields:
        try_add_numeric("temperature_c", "temperature")
    if "pressure" in reading and "pressure_pa" not in numeric_fields:
        try_add_numeric("pressure_pa", "pressure")

    # Device timestamps / age
    try_add_numeric("timestamp_ms")
    try_add_numeric("sample_age_ms")

    # ISO timestamp (string field)
    if "timestamp_iso" in reading and reading["timestamp_iso"] is not None:
        string_fields["timestamp_iso"] = str(reading["timestamp_iso"])

    # No fields? bail
    if not numeric_fields and not string_fields:
        return None

    # Computed corrected time (as a field, ms since epoch)
    corrected_ms = corrected_ts_ns // 1_000_000
    numeric_fields["sample_time_corrected_ms"] = float(corrected_ms)

    # Assemble line protocol
    tag_str = ",".join(f"{escape_tag(str(k))}={escape_tag(str(v))}" for k, v in sorted(tag_items.items()))
    field_parts = []
    # numeric fields
    for k, v in sorted(numeric_fields.items()):
        field_parts.append(f"{k}={v}")
    # string fields
    for k, v in sorted(string_fields.items()):
        field_parts.append(f"{k}={escape_str_field(v)}")

    ts = convert_ts_precision(corrected_ts_ns, INFLUX_PRECISION)
    return f"{MEASUREMENT},{tag_str} {','.join(field_parts)} {ts}"

# -----------------------------
# Threads
# -----------------------------
def discovery_thread():
    interval_sec = max(1, int(DISCOVERY_INTERVAL_HOURS * 3600))
    sess = requests.Session()
    if DEVICE_USER or DEVICE_PASS:
        sess.auth = HTTPBasicAuth(DEVICE_USER, DEVICE_PASS)

    next_run = 0
    while not shutdown_event.is_set():
        now = time.monotonic()
        if now >= next_run:
            found = run_discovery()
            validated = {}
            for ip, info in found.items():
                if validate_device(sess, info):
                    validated[ip] = info
                else:
                    logger.info("Skipping device (validation failed): %s", ip)
            with devices_lock:
                before = set(devices.keys())
                devices.update(validated)
                added = set(devices.keys()) - before
                if added:
                    logger.info("Devices added after discovery: %s", ", ".join(sorted(added)) or "none")
            next_run = now + interval_sec
        shutdown_event.wait(1.0)


def polling_thread():
    read_sess = requests.Session()
    if DEVICE_USER or DEVICE_PASS:
        read_sess.auth = HTTPBasicAuth(DEVICE_USER, DEVICE_PASS)
    write_sess = requests.Session()

    last_poll = 0.0
    while not shutdown_event.is_set():
        now = time.monotonic()
        if now - last_poll >= POLL_INTERVAL_SECONDS:
            last_poll = now
            with devices_lock:
                snapshot = dict(devices)

            lines = []
            server_now_ns = now_ns()

            for ip, d in snapshot.items():
                url = f"{d.get('scheme','http')}://{ip}:{d.get('http_port', DEFAULT_HTTP_PORT)}{SENSORS_PATH}"
                try:
                    r = read_sess.get(url, timeout=3)
                    if r.status_code != 200:
                        logger.debug("Poll %s HTTP %s", url, r.status_code)
                        continue
                    js = r.json()

                    # Flatten if nested
                    payload = js
                    if "sensors" in js and isinstance(js["sensors"], dict):
                        payload = {**js, **js["sensors"]}

                    # Compute corrected timestamp:
                    # default to server_now_ns minus sample_age_ms if present,
                    # else use device timestamp_ms if present,
                    # else fallback to server_now_ns
                    corrected_ts_ns = server_now_ns
                    sample_age_ms = safe_get(payload, "sample_age_ms", default=None)
                    timestamp_ms = safe_get(payload, "timestamp_ms", default=None)

                    if isinstance(sample_age_ms, (int, float)):
                        corrected_ts_ns = server_now_ns - int(float(sample_age_ms) * 1_000_000)
                    elif isinstance(timestamp_ms, (int, float)):
                        corrected_ts_ns = int(float(timestamp_ms) * 1_000_000)

                    lp = build_line_protocol(d, payload, corrected_ts_ns)
                    if lp:
                        lines.append(lp)
                        telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "success"})
                        logger.info("Reading %s: %s", ip, {k: payload.get(k) for k in (
                            "temperature_c","temperature_f","humidity","pressure_pa","pressure_hpa",
                            "device","model","device_id","timestamp_ms","timestamp_iso","sample_age_ms","tags"
                        )})
                    else:
                        logger.debug("No expected fields in %s payload: %s", ip, payload)
                        telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "empty"})
                except Exception as e:
                    logger.warning("Polling error for %s: %s", ip, e)
                    telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "error"})

            if lines:
                batch = "\n".join(lines)
                influx_write(write_sess, batch)
            telemetry.record("house_sensors.poll_batch_lines", len(lines), {"operation.type": "polling"})

        shutdown_event.wait(0.05)

# -----------------------------
# Main
# -----------------------------
def main():
    if not INFLUX_TOKEN or not INFLUX_BUCKET or not INFLUX_ORG:
        logger.warning("InfluxDB env not fully set. Set INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, INFLUX_BUCKET.")
    if not DEVICE_USER and not DEVICE_PASS:
        logger.info("DEVICE_USER / DEVICE_PASS not set; proceeding without Basic Auth.")

    # Initial discovery + validation
    initial = run_discovery()
    sess = requests.Session()
    if DEVICE_USER or DEVICE_PASS:
        sess.auth = HTTPBasicAuth(DEVICE_USER, DEVICE_PASS)
    validated = {ip: d for ip, d in initial.items() if validate_device(sess, d)}
    with devices_lock:
        devices.update(validated)

    # Threads
    t1 = threading.Thread(target=polling_thread, name="poller", daemon=True)
    t2 = threading.Thread(target=discovery_thread, name="discovery", daemon=True)
    t1.start()
    t2.start()

    logger.info("Service started. Poll interval=%.3fs, rediscovery every %.2f hour(s). Precision=%s",
                POLL_INTERVAL_SECONDS, DISCOVERY_INTERVAL_HOURS, INFLUX_PRECISION)

    try:
        while not shutdown_event.is_set():
            time.sleep(0.5)
    finally:
        logger.info("Shutting down...")


if __name__ == "__main__":
    main()
