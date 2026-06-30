from __future__ import annotations

import asyncio
import logging

from conftest import load_module

volt_collector = load_module("voltage_collector_test", "collectors/volt/voltage_collector.py")


def test_load_config_merges_file_config_with_env_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("KASA_USERNAME", "user@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "secret-password")
    monkeypatch.setenv("INFLUXDB_URL", "http://influx.local:8086")
    monkeypatch.setenv("INFLUXDB_TOKEN", "")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
collection_interval: 5
influxdb:
  bucket: file-bucket
prometheus:
  job_name: file-job
""".strip()
    )

    collector = volt_collector.VoltageCollector(config_path=str(config_path))

    assert collector.config["collection_interval"] == 5
    assert collector.config["kasa_auth"] == {
        "username": "user@example.com",
        "password": "secret-password",
    }
    assert collector.config["influxdb"]["url"] == "http://influx.local:8086"
    assert collector.config["influxdb"]["bucket"] == "file-bucket"
    assert collector.config["prometheus"]["job_name"] == "file-job"


def test_load_config_logs_secret_presence_without_secret_value(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("KASA_USERNAME", "user@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "secret-password")
    monkeypatch.setenv("INFLUXDB_TOKEN", "")

    missing_config = tmp_path / "missing.yaml"

    with caplog.at_level(logging.INFO):
        volt_collector.VoltageCollector(config_path=str(missing_config))

    assert "secret-password" not in caplog.text
    assert "'kasa_password_set': True" in caplog.text


def test_collect_device_data_converts_milli_units():
    class EnergyModule:
        emeter_realtime = {
            "voltage_mv": 120_500,
            "current_ma": 1250,
            "power_mw": 42_250,
            "total_wh": 1234,
        }

    class Device:
        alias = "Office Plug"
        location = "office"
        modules = {"energy": EnergyModule()}

        async def update(self):
            self.updated = True

    collector = object.__new__(volt_collector.VoltageCollector)
    data = asyncio.run(collector.collect_device_data("192.168.66.50", Device()))

    assert data is not None
    assert data["device_name"] == "Office Plug"
    assert data["device_ip"] == "192.168.66.50"
    assert data["location"] == "office"
    assert data["voltage"] == 120.5
    assert data["current"] == 1.25
    assert data["power"] == 42.25
    assert data["total"] == 1.234


def test_collect_device_data_accepts_direct_units():
    class Device:
        alias = "Bench Plug"
        emeter_realtime = {
            "voltage": 119.8,
            "current": 0.5,
            "power": 12.3,
            "total": 9.1,
        }

        async def update(self):
            self.updated = True

    collector = object.__new__(volt_collector.VoltageCollector)
    data = asyncio.run(collector.collect_device_data("192.168.66.51", Device()))

    assert data is not None
    assert data["device_name"] == "Bench Plug"
    assert data["location"] == "unknown"
    assert data["voltage"] == 119.8
    assert data["current"] == 0.5
    assert data["power"] == 12.3
    assert data["total"] == 9.1
