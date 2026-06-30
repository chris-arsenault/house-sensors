from __future__ import annotations

from conftest import load_module

env_collector = load_module(
    "env_sensor_collector_test",
    "collectors/environment-sensors/env_sensor_collector.py",
)


def test_build_device_from_reply_uses_payload_metadata():
    device = env_collector.build_device_from_reply(
        ("192.168.66.42", 12343),
        b'{"deviceId":"office-sensor","http_port":8080,"scheme":"HTTPS"}',
    )

    assert device == {
        "ip": "192.168.66.42",
        "name": "office-sensor",
        "http_port": 8080,
        "scheme": "https",
    }


def test_build_device_from_reply_falls_back_to_defaults_for_bad_json():
    device = env_collector.build_device_from_reply(("192.168.66.43", 12343), b"not json")

    assert device == {
        "ip": "192.168.66.43",
        "name": "192.168.66.43",
        "http_port": 80,
        "scheme": "http",
    }


def test_extract_kvp_tags_accepts_supported_shapes():
    tags = env_collector.extract_kvp_tags(
        [
            {"room": "office"},
            "zone=upstairs",
            "ignored",
            {"rack": 2},
        ]
    )

    assert tags == {
        "room": "office",
        "zone": "upstairs",
        "rack": "2",
    }


def test_build_line_protocol_flattens_payload_and_escapes_values():
    line = env_collector.build_line_protocol(
        {"name": "Office Sensor", "ip": "192.168.66.42"},
        {
            "sensors": {
                "temperature": 21.5,
                "pressure": 101325,
            },
            "humidity": 45.1,
            "sample_age_ms": 50,
            "timestamp_iso": "2026-06-30T03:00:00Z",
            "tags": {"room": "office lab"},
        },
        1_700_000_000_123_456_789,
    )

    assert line == (
        r"environment,device=Office\ Sensor,ip=192.168.66.42,room=office\ lab "
        r"humidity=45.1,pressure_pa=101325.0,sample_age_ms=50.0,sample_time_corrected_ms=1700000000123.0,temperature_c=21.5,"
        r'timestamp_iso="2026-06-30T03:00:00Z" 1700000000123456789'
    )


def test_build_line_protocol_skips_payloads_without_sensor_fields():
    line = env_collector.build_line_protocol(
        {"name": "Office Sensor", "ip": "192.168.66.42"},
        {"tags": {"room": "office"}},
        1_700_000_000_123_456_789,
    )

    assert line is None
