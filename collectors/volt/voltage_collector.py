#!/usr/bin/env python3
"""
Voltage reading drain -> InfluxDB.

Drains the `kasa` stream of the ahara-collector appliance (the host that
discovers and polls the KP125M plugs over KLAP on the home LAN) and maps
each device-native reading envelope to the `voltage_monitoring`
measurement. This collector owns the schema: field names and the
vendor-unit conversions (mW -> W, Wh -> kWh, mV -> V, mA -> A) live here
(ahara-collector ADR-0006); the appliance ships the KLAP payload verbatim.

Envelope contract (ahara-collector docs/integration.md):
  {"module": "kasa",
   "device": {"ip": ..., "name": ..., "model": ..., "deviceId": ...},
   "timestampNs": <poll time>,
   "values": {<the get_energy_usage result, verbatim>}}
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from datetime import UTC, datetime
from typing import Dict, Optional

import requests
import yaml
from app_telemetry import telemetry_from_env

# InfluxDB imports
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS

    INFLUX_AVAILABLE = True
    print("InfluxDB client loaded successfully")
except ImportError as e:
    INFLUX_AVAILABLE = False
    print(f"InfluxDB client not available: {e}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
telemetry = telemetry_from_env("house-sensors.volt")

shutdown_event = threading.Event()


def _signal_handler(sig, frame):
    logger.info("Received shutdown signal. Stopping...")
    shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


class VoltageCollector:
    def __init__(self, config_path: str = "/app/config.yaml"):
        self.config = self.load_config(config_path)
        self.influx_client: Optional[InfluxDBClient] = None

        self.setup_backends()

    def load_config(self, config_path: str = "/app/config.yaml") -> dict:
        """Load configuration from YAML file or environment variables"""
        default_config = {
            "drain_interval": float(os.getenv("DRAIN_INTERVAL_SECONDS", "10")),
            "drain_timeout": float(os.getenv("DRAIN_TIMEOUT_SECONDS", "10")),
            "collector": {
                "url": os.getenv("COLLECTOR_URL", "http://192.168.30.2:8850"),
                "token": os.getenv("COLLECTOR_TOKEN", ""),
                "module": os.getenv("COLLECTOR_MODULE", "kasa"),
            },
            "influxdb": {
                "url": os.getenv("INFLUXDB_URL", "http://localhost:8086"),
                "token": os.getenv("INFLUXDB_TOKEN", ""),
                "org": os.getenv("INFLUXDB_ORG", "voltage-monitoring"),
                "bucket": os.getenv("INFLUXDB_BUCKET", "voltage-data"),
            },
        }

        # Try to load from file, but don't fail if file doesn't exist
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    file_config = yaml.safe_load(f) or {}

                # Merge file config with defaults
                for key, value in file_config.items():
                    if key in default_config and isinstance(value, dict) and isinstance(default_config[key], dict):
                        default_config[key].update(value)
                    else:
                        default_config[key] = value
                logger.info(f"Loaded configuration from {config_path}")
            except Exception as e:
                logger.warning(f"Could not load config file {config_path}: {e}")
                logger.info("Using environment variables and defaults")
        else:
            logger.info(f"Config file {config_path} not found, using environment variables and defaults")

        # Log the configuration (without sensitive data)
        config_summary = {
            "drain_interval": default_config["drain_interval"],
            "collector_url": default_config["collector"]["url"],
            "collector_module": default_config["collector"]["module"],
            "collector_token_set": bool(default_config["collector"]["token"]),
            "influxdb_url": default_config["influxdb"]["url"],
            "influxdb_org": default_config["influxdb"]["org"],
            "influxdb_bucket": default_config["influxdb"]["bucket"],
            "influxdb_token_set": bool(default_config["influxdb"]["token"]),
        }
        logger.info(f"Configuration: {config_summary}")

        return default_config

    def setup_backends(self):
        """Initialize the InfluxDB client"""
        influx_config = self.config.get("influxdb", {})
        influx_url = influx_config.get("url", "http://localhost:8086")
        influx_token = influx_config.get("token", "")
        influx_org = influx_config.get("org", "voltage-monitoring")

        if INFLUX_AVAILABLE and influx_token:
            try:
                self.influx_client = InfluxDBClient(
                    url=influx_url,
                    token=influx_token,
                    org=influx_org,
                )
                # Test connection
                self.influx_client.ping()
                logger.info(f"InfluxDB client initialized and connected to {influx_url}")
                telemetry.count(
                    "house_sensors.backend_connections",
                    attributes={"backend": "influxdb", "operation.type": "background", "outcome": "success"},
                )
            except Exception as e:
                logger.error(f"Failed to initialize InfluxDB client: {e}")
                logger.error(f"InfluxDB config - URL: {influx_url}, Org: {influx_org}, Token set: {bool(influx_token)}")
                telemetry.count(
                    "house_sensors.backend_connections",
                    attributes={"backend": "influxdb", "operation.type": "background", "outcome": "error"},
                )
                self.influx_client = None
        elif not INFLUX_AVAILABLE:
            logger.warning("InfluxDB client library not available - install influxdb-client")
        elif not influx_token:
            logger.warning(f"InfluxDB token not configured - skipping InfluxDB setup. URL: {influx_url}")

        if not self.influx_client:
            logger.warning("No data storage backend available - data will only be logged to console")

    @staticmethod
    def envelope_to_data(envelope: Dict) -> Optional[dict]:
        """Map one kasa reading envelope to the voltage_monitoring shape.

        The values object is the device's get_energy_usage result verbatim
        (current_power in mW, today_energy in Wh, voltage_mv, current_ma);
        the unit conversions to W/kWh/V/A happen here.
        """
        device = envelope.get("device") or {}
        values = envelope.get("values") or {}
        timestamp_ns = envelope.get("timestampNs")
        if not isinstance(device, dict) or not isinstance(values, dict):
            return None
        if not isinstance(timestamp_ns, int):
            return None
        ip = device.get("ip")
        if not ip:
            return None

        def milli(key: str) -> Optional[float]:
            value = values.get(key)
            return value / 1000.0 if isinstance(value, (int, float)) else None

        voltage = milli("voltage_mv")
        current = milli("current_ma")
        power = milli("current_power")
        total = milli("today_energy")

        if voltage is None and current is None and power is None and total is None:
            return None

        return {
            "timestamp": datetime.fromtimestamp(timestamp_ns / 1_000_000_000, UTC),
            "device_name": device.get("name") or device.get("deviceId") or ip,
            "device_ip": ip,
            "location": "unknown",
            "voltage": voltage if voltage is not None else 0.0,
            "current": current if current is not None else 0.0,
            "power": power if power is not None else 0.0,
            "total": total if total is not None else 0.0,
        }

    def write_to_influxdb(self, data: dict) -> bool:
        """Write data point to InfluxDB"""
        if not self.influx_client:
            return False

        try:
            influx_config = self.config.get("influxdb", {})
            bucket = influx_config.get("bucket", "voltage-data")

            point = (
                Point("voltage_monitoring")
                .tag("device_name", data["device_name"])
                .tag("device_ip", data["device_ip"])
                .tag("location", data["location"])
                .field("voltage", data["voltage"])
                .field("current", data["current"])
                .field("power", data["power"])
                .field("total", data["total"])
                .time(data["timestamp"])
            )

            write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=bucket, record=point)
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "drain", "outcome": "success"})
            return True

        except Exception as e:
            logger.error(f"Error writing to InfluxDB: {e}")
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "drain", "outcome": "error"})
            return False

    def drain_once(self, session: requests.Session) -> bool:
        """One drain cycle. Returns True when a batch was fully processed
        and acked (the caller retries immediately to clear a backlog)."""
        collector = self.config["collector"]
        base = collector["url"].rstrip("/")
        timeout = self.config["drain_timeout"]
        try:
            r = session.get(
                f"{base}/readings/next",
                params={"module": collector["module"]},
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"Collector fetch failed: {e}")
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
            return False
        if r.status_code == 204:
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "empty"})
            return False
        if r.status_code != 200:
            logger.warning(f"Collector fetch failed: HTTP {r.status_code} {r.text[:200]}")
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
            return False

        try:
            doc = r.json()
            batch_id = doc["batchId"]
            raw_lines = doc.get("lines", "").splitlines()
        except Exception as e:
            logger.warning(f"Collector batch unparseable: {e}")
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "error"})
            return False

        written = 0
        skipped = 0
        for raw in raw_lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                envelope = json.loads(raw)
            except Exception:
                skipped += 1
                continue
            data = self.envelope_to_data(envelope) if isinstance(envelope, dict) else None
            if data is None:
                skipped += 1
                continue
            # Write before ack: an unacked batch is re-served, and
            # duplicate writes are idempotent per measurement/tags/timestamp.
            if not self.write_to_influxdb(data):
                telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "write_failed"})
                return False
            logger.info(f"Drained: {data['device_name']} - {data['voltage']:.1f}V, {data['power']:.1f}W")
            written += 1

        if skipped:
            logger.warning(f"Skipped {skipped} unmappable envelope(s) in batch {batch_id}")
            telemetry.record("house_sensors.drain_skipped_envelopes", skipped, {"operation.type": "drain"})

        try:
            ack = session.post(
                f"{base}/readings/ack",
                json={"module": collector["module"], "batchId": batch_id},
                timeout=timeout,
            )
            if ack.status_code != 200:
                logger.warning(f"Ack failed: HTTP {ack.status_code}")
                telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "ack_failed"})
                return False
        except Exception as e:
            logger.warning(f"Ack failed: {e}")
            telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "ack_failed"})
            return False

        telemetry.count("house_sensors.drain_batches", attributes={"operation.type": "drain", "outcome": "success"})
        telemetry.record("house_sensors.drain_batch_lines", written, {"operation.type": "drain"})
        return True

    def run(self):
        """Main run method"""
        logger.info("Starting Voltage Collector drain...")
        collector = self.config["collector"]
        if not collector["token"]:
            logger.warning("COLLECTOR_TOKEN not set; the collector will refuse the drain.")

        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {collector['token']}"

        while not shutdown_event.is_set():
            started_at = time.monotonic()
            processed = self.drain_once(session)
            telemetry.record(
                "house_sensors.collection_loop_duration_ms",
                (time.monotonic() - started_at) * 1000,
                {"operation.type": "drain"},
            )
            if not processed:
                shutdown_event.wait(self.config["drain_interval"])

        logger.info("Shutting down...")


def main():
    collector = VoltageCollector()
    collector.run()


if __name__ == "__main__":
    main()
