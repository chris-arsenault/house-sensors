from __future__ import annotations

import logging
from datetime import UTC, datetime

from conftest import load_module

volt_collector = load_module("voltage_collector_test", "collectors/volt/voltage_collector.py")


def test_load_config_merges_file_config_with_env_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLECTOR_URL", "http://collector.local:8850")
    monkeypatch.setenv("COLLECTOR_TOKEN", "secret-token")
    monkeypatch.setenv("INFLUXDB_URL", "http://influx.local:8086")
    monkeypatch.setenv("INFLUXDB_TOKEN", "")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
drain_interval: 5
influxdb:
  bucket: file-bucket
collector:
  module: kasa
""".strip()
    )

    collector = volt_collector.VoltageCollector(config_path=str(config_path))

    assert collector.config["drain_interval"] == 5
    assert collector.config["collector"]["url"] == "http://collector.local:8850"
    assert collector.config["collector"]["token"] == "secret-token"
    assert collector.config["collector"]["module"] == "kasa"
    assert collector.config["influxdb"]["url"] == "http://influx.local:8086"
    assert collector.config["influxdb"]["bucket"] == "file-bucket"


def test_load_config_logs_secret_presence_without_secret_value(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("COLLECTOR_TOKEN", "secret-token")
    monkeypatch.setenv("INFLUXDB_TOKEN", "")

    missing_config = tmp_path / "missing.yaml"

    with caplog.at_level(logging.INFO):
        volt_collector.VoltageCollector(config_path=str(missing_config))

    assert "secret-token" not in caplog.text
    assert "'collector_token_set': True" in caplog.text


def test_envelope_to_data_converts_vendor_units():
    data = volt_collector.VoltageCollector.envelope_to_data(
        {
            "module": "kasa",
            "device": {
                "ip": "192.168.65.40",
                "name": "Office Plug",
                "deviceId": "8012ABC",
                "model": "KP125M(US)",
            },
            "timestampNs": 1_700_000_000_000_000_000,
            "values": {
                "current_power": 42_250,
                "today_energy": 1234,
                "voltage_mv": 120_500,
                "current_ma": 1250,
                "today_runtime": 300,
            },
        }
    )

    assert data is not None
    assert data["device_name"] == "Office Plug"
    assert data["device_ip"] == "192.168.65.40"
    assert data["location"] == "unknown"
    assert data["voltage"] == 120.5
    assert data["current"] == 1.25
    assert data["power"] == 42.25
    assert data["total"] == 1.234
    assert data["timestamp"] == datetime.fromtimestamp(1_700_000_000, UTC)


def test_envelope_to_data_falls_back_to_device_id_for_the_name():
    data = volt_collector.VoltageCollector.envelope_to_data(
        {
            "module": "kasa",
            "device": {"ip": "192.168.65.40", "deviceId": "8012ABC"},
            "timestampNs": 42,
            "values": {"current_power": 1500},
        }
    )

    assert data is not None
    assert data["device_name"] == "8012ABC"
    assert data["power"] == 1.5
    assert data["voltage"] == 0.0


def test_envelope_to_data_rejects_envelopes_without_energy_values():
    assert (
        volt_collector.VoltageCollector.envelope_to_data(
            {
                "module": "kasa",
                "device": {"ip": "192.168.65.40"},
                "timestampNs": 42,
                "values": {"today_runtime": 300},
            }
        )
        is None
    )
    assert (
        volt_collector.VoltageCollector.envelope_to_data(
            {"device": {"ip": "192.168.65.40"}, "values": {"current_power": 1}}
        )
        is None
    )
