from __future__ import annotations

import json

from conftest import load_module

env_collector = load_module(
    "env_sensor_collector_test",
    "collectors/environment-sensors/env_sensor_collector.py",
)


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


def test_envelope_to_line_maps_device_native_values_to_the_schema():
    line = env_collector.envelope_to_line(
        {
            "module": "envSensors",
            "device": {
                "ip": "192.168.65.42",
                "name": "Office Sensor",
                "tags": {"room": "office lab"},
            },
            "timestampNs": 1_700_000_000_123_456_789,
            "values": {
                "sensors": {
                    "temperature": 21.5,
                    "pressure": 101325,
                },
                "humidity": 45.1,
                "sample_age_ms": 50,
                "timestamp_iso": "2026-06-30T03:00:00Z",
            },
        }
    )

    assert line == (
        r"environment,device=Office\ Sensor,ip=192.168.65.42,room=office\ lab "
        r"humidity=45.1,pressure_pa=101325.0,sample_age_ms=50.0,sample_time_corrected_ms=1700000000123.0,temperature_c=21.5,"
        r'timestamp_iso="2026-06-30T03:00:00Z" 1700000000123456789'
    )


def test_envelope_to_line_carries_device_identity_tags():
    line = env_collector.envelope_to_line(
        {
            "module": "envSensors",
            "device": {
                "ip": "192.168.65.42",
                "name": "ATOM3U-ENV3-005",
                "model": "ENV3",
                "deviceId": "ATOM3U-ENV3-005",
            },
            "timestampNs": 42,
            "values": {"temperature_c": 20.0},
        }
    )

    assert line is not None
    assert line.startswith(
        r"environment,device=ATOM3U-ENV3-005,device_id=ATOM3U-ENV3-005,ip=192.168.65.42,model=ENV3 "
    )
    assert line.endswith(" 42")


def test_envelope_to_line_skips_envelopes_without_sensor_fields():
    assert (
        env_collector.envelope_to_line(
            {
                "module": "envSensors",
                "device": {"ip": "192.168.65.42"},
                "timestampNs": 42,
                "values": {},
            }
        )
        is None
    )
    assert (
        env_collector.envelope_to_line({"device": {"ip": "x"}, "values": {"temperature_c": 1}})
        is None
    )


def test_map_batch_maps_lines_and_counts_the_unmappable():
    good = json.dumps(
        {
            "module": "envSensors",
            "device": {"ip": "192.168.65.42", "name": "s"},
            "timestampNs": 7,
            "values": {"temperature_c": 21.0},
        }
    )
    lines, skipped = env_collector.map_batch(f"{good}\nnot json\n\n{good}\n")

    assert len(lines) == 2
    assert skipped == 1
    assert all(line.startswith("environment,") for line in lines)
