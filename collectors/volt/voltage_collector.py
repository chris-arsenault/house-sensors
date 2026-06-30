#!/usr/bin/env python3
"""
Voltage monitoring collector for Kasa smart plugs
Supports both InfluxDB and Prometheus backends
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from typing import Dict, Optional

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

# Prometheus imports
try:
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

    PROMETHEUS_AVAILABLE = True
    print("Prometheus client loaded successfully")
except ImportError as e:
    PROMETHEUS_AVAILABLE = False
    print(f"Prometheus client not available: {e}")

# Kasa imports
try:
    from kasa import Device, Discover
    from kasa.credentials import Credentials

    KASA_AVAILABLE = True
    print("Python-kasa loaded successfully")
except ImportError as e:
    KASA_AVAILABLE = False
    print(f"Python-kasa not available: {e}")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
telemetry = telemetry_from_env("house-sensors.volt")


class VoltageCollector:
    def __init__(self, config_path: str = "/app/config.yaml"):
        self.config = self.load_config(config_path)
        self.devices: Dict[str, Device] = {}
        self.influx_client: Optional[InfluxDBClient] = None
        self.prometheus_registry: Optional[CollectorRegistry] = None
        self.voltage_gauge: Optional[Gauge] = None
        self.current_gauge: Optional[Gauge] = None
        self.power_gauge: Optional[Gauge] = None

        self.setup_backends()

    def load_config(self, config_path: str = "/app/config.yaml") -> dict:
        """Load configuration from YAML file or environment variables"""
        default_config = {
            "collection_interval": int(os.getenv("COLLECTION_INTERVAL", "1")),
            "kasa_auth": {
                "username": os.getenv("KASA_USERNAME", ""),
                "password": os.getenv("KASA_PASSWORD", ""),
            },
            "influxdb": {
                "url": os.getenv("INFLUXDB_URL", "http://localhost:8086"),
                "token": os.getenv("INFLUXDB_TOKEN", ""),
                "org": os.getenv("INFLUXDB_ORG", "voltage-monitoring"),
                "bucket": os.getenv("INFLUXDB_BUCKET", "voltage-data"),
            },
            "prometheus": {
                "pushgateway": os.getenv("PROMETHEUS_PUSHGATEWAY", "http://localhost:9091"),
                "job_name": "voltage-monitoring",
            },
            "devices": {
                # Will be auto-discovered if empty
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
            "collection_interval": default_config["collection_interval"],
            "kasa_username_set": bool(default_config["kasa_auth"]["username"]),
            "kasa_password_set": bool(default_config["kasa_auth"]["password"]),
            "influxdb_url": default_config["influxdb"]["url"],
            "influxdb_org": default_config["influxdb"]["org"],
            "influxdb_bucket": default_config["influxdb"]["bucket"],
            "influxdb_token_set": bool(default_config["influxdb"]["token"]),
            "prometheus_pushgateway": default_config["prometheus"]["pushgateway"],
        }
        logger.info(f"Configuration: {config_summary}")

        return default_config

    def setup_backends(self):
        """Initialize InfluxDB and Prometheus clients"""
        # InfluxDB setup
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
        else:
            logger.warning("InfluxDB setup skipped for unknown reason")

        # Check if at least one backend is available
        if not self.influx_client and not self.prometheus_registry:
            logger.warning("No data storage backends available - data will only be logged to console")

    async def discover_devices(self):
        """Discover Kasa devices on the network using new API"""
        started_at = time.monotonic()
        if not KASA_AVAILABLE:
            logger.error("Python-kasa not available, cannot discover devices")
            telemetry.count("house_sensors.discovery_runs", attributes={"operation.type": "background", "outcome": "dependency_missing"})
            return

        # Get authentication credentials
        auth_config = self.config.get("kasa_auth", {})
        username = auth_config.get("username", "")
        password = auth_config.get("password", "")

        # Create credentials if provided
        credentials = None
        if username and password:
            try:
                credentials = Credentials(username=username, password=password)
                logger.info("Using authentication for configured Kasa account")
            except Exception as e:
                logger.warning(f"Could not create credentials: {e}")
                return
        else:
            logger.info("No authentication credentials provided")
            telemetry.count("house_sensors.discovery_runs", attributes={"operation.type": "background", "outcome": "missing_credentials"})
            return

        # Auto-discovery - use all found devices
        logger.info("Attempting automatic device discovery with authentication...")
        try:
            found_devices = await Discover.discover(timeout=10, credentials=credentials)
            logger.info(f"Auto-discovery found {len(found_devices)} authenticated devices")
            telemetry.record("house_sensors.discovered_devices", len(found_devices), {"operation.type": "background"})

            for ip, device in found_devices.items():
                if device:
                    try:
                        await device.update()

                        # Check if device has energy monitoring
                        has_emeter = (
                            hasattr(device, "has_emeter") and device.has_emeter
                            or hasattr(device, "modules") and "energy" in device.modules
                            or hasattr(device, "emeter")
                            or "emeter" in str(type(device)).lower()
                        )

                        if has_emeter:
                            self.devices[ip] = device
                            device_name = device.alias if hasattr(device, "alias") else f"device_{ip}"
                            logger.info(f"Auto-discovered energy monitoring device: {device_name} ({ip})")
                        else:
                            device_name = device.alias if hasattr(device, "alias") else f"device_{ip}"
                            logger.info(f"Skipped non-energy monitoring device: {device_name} ({ip})")
                    except Exception as e:
                        logger.error(f"Could not update device at {ip}: {e}")

        except Exception as e:
            logger.error(f"Auto-discovery with authentication failed: {e}")
            logger.info("Make sure your username/password are correct")
            telemetry.count("house_sensors.discovery_runs", attributes={"operation.type": "background", "outcome": "error"})
            telemetry.record("house_sensors.discovery_duration_ms", (time.monotonic() - started_at) * 1000, {"operation.type": "background"})
            return

        telemetry.count("house_sensors.discovery_runs", attributes={"operation.type": "background", "outcome": "success"})
        telemetry.record("house_sensors.discovery_duration_ms", (time.monotonic() - started_at) * 1000, {"operation.type": "background"})
        telemetry.record("house_sensors.monitored_devices", len(self.devices), {"operation.type": "background"})

    async def collect_device_data(self, ip: str, device: Device) -> Optional[dict]:
        """Collect voltage/power data from a single device using new API"""
        try:
            await device.update()

            # Try different methods to get energy data with new API
            emeter_data = None

            # Method 1: Check for emeter module (new API)
            if hasattr(device, "modules") and "energy" in device.modules:
                energy_module = device.modules["energy"]
                if hasattr(energy_module, "emeter_realtime"):
                    emeter_data = energy_module.emeter_realtime
                elif hasattr(energy_module, "get_realtime"):
                    emeter_data = energy_module.get_realtime()

            # Method 2: Direct emeter access (legacy support)
            elif hasattr(device, "emeter_realtime"):
                emeter_data = device.emeter_realtime

            # Method 3: Current consumption method
            elif hasattr(device, "current_consumption"):
                emeter_data = device.current_consumption()

            # Method 4: Try emeter property
            elif hasattr(device, "emeter"):
                emeter = device.emeter
                if hasattr(emeter, "get_realtime"):
                    emeter_data = await emeter.get_realtime()
                elif hasattr(emeter, "realtime"):
                    emeter_data = emeter.realtime

            if not emeter_data:
                logger.warning(f"No energy data available for {device.alias if hasattr(device, 'alias') else ip}")
                telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "empty"})
                return None

            # Handle different data formats
            voltage = 0.0
            current = 0.0
            power = 0.0
            total = 0.0

            # Extract voltage (handle both mV and V formats)
            if "voltage_mv" in emeter_data:
                voltage = emeter_data["voltage_mv"] / 1000.0
            elif "voltage" in emeter_data:
                voltage = float(emeter_data["voltage"])

            # Extract current (handle both mA and A formats)
            if "current_ma" in emeter_data:
                current = emeter_data["current_ma"] / 1000.0
            elif "current" in emeter_data:
                current = float(emeter_data["current"])

            # Extract power (handle both mW and W formats)
            if "power_mw" in emeter_data:
                power = emeter_data["power_mw"] / 1000.0
            elif "power" in emeter_data:
                power = float(emeter_data["power"])

            # Extract total energy (handle both Wh and kWh formats)
            if "total_wh" in emeter_data:
                total = emeter_data["total_wh"] / 1000.0
            elif "total" in emeter_data:
                total = float(emeter_data["total"])

            device_name = device.alias if hasattr(device, "alias") else f"device_{ip.replace('.', '_')}"
            device_location = "unknown"
            if hasattr(device, "location") and device.location:
                device_location = device.location

            data = {
                "timestamp": datetime.now(UTC),
                "device_name": device_name,
                "device_ip": ip,
                "location": device_location,
                "voltage": voltage,
                "current": current,
                "power": power,
                "total": total,
            }

            telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "success"})
            return data

        except Exception as e:
            device_name = device.alias if hasattr(device, "alias") else ip
            logger.error(f"Error collecting data from {device_name} ({ip}): {e}")
            telemetry.count("house_sensors.poll_results", attributes={"operation.type": "polling", "outcome": "error"})
            return None

    def write_to_influxdb(self, data: dict):
        """Write data point to InfluxDB"""
        if not self.influx_client:
            return

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
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "polling", "outcome": "success"})

        except Exception as e:
            logger.error(f"Error writing to InfluxDB: {e}")
            telemetry.count("house_sensors.influx_writes", attributes={"operation.type": "polling", "outcome": "error"})

    def write_to_prometheus(self, data: dict):
        """Update Prometheus metrics"""
        if not self.prometheus_registry:
            return

        try:
            prometheus_config = self.config.get("prometheus", {})
            pushgateway_url = prometheus_config.get("pushgateway", "http://localhost:9091")
            job_name = prometheus_config.get("job_name", "voltage-monitoring")

            labels = {
                "device_name": data["device_name"],
                "device_ip": data["device_ip"],
                "location": data["location"],
            }

            self.voltage_gauge.labels(**labels).set(data["voltage"])
            self.current_gauge.labels(**labels).set(data["current"])
            self.power_gauge.labels(**labels).set(data["power"])

            # Push to gateway
            push_to_gateway(pushgateway_url, job=job_name, registry=self.prometheus_registry)

        except Exception as e:
            logger.error(f"Error writing to Prometheus: {e}")

    async def collection_loop(self):
        """Main collection loop"""
        logger.info("Starting voltage collection loop...")

        while True:
            try:
                started_at = time.monotonic()
                # Collect data from all devices
                tasks = []
                for ip, device in self.devices.items():
                    tasks.append(self.collect_device_data(ip, device))

                results = await asyncio.gather(*tasks, return_exceptions=True)

                # Write successful results to backends
                for result in results:
                    if isinstance(result, dict):
                        logger.info(f"Collected: {result['device_name']} - {result['voltage']:.1f}V, {result['power']:.1f}W")

                        # Write to backends
                        self.write_to_influxdb(result)
                        self.write_to_prometheus(result)

                telemetry.record("house_sensors.collection_loop_duration_ms", (time.monotonic() - started_at) * 1000, {"operation.type": "polling"})
                successful_results = len([result for result in results if isinstance(result, dict)])
                telemetry.record("house_sensors.collection_loop_results", successful_results, {"operation.type": "polling"})
                telemetry.count("house_sensors.collection_loops", attributes={"operation.type": "polling", "outcome": "success"})
                await asyncio.sleep(self.config["collection_interval"])

            except Exception as e:
                logger.error(f"Error in collection loop: {e}")
                telemetry.count("house_sensors.collection_loops", attributes={"operation.type": "polling", "outcome": "error"})
                await asyncio.sleep(5)  # Wait before retrying

    async def run(self):
        """Main run method"""
        logger.info("Starting Voltage Collector...")

        # Discover devices
        await self.discover_devices()

        if not self.devices:
            logger.error("No energy monitoring devices found. Exiting.")
            return

        # Start collection loop
        await self.collection_loop()


async def main():
    collector = VoltageCollector()
    await collector.run()


if __name__ == "__main__":
    asyncio.run(main())
