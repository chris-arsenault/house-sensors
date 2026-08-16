# Backlog

Planned-but-not-built work. Each item is a positive assertion of future-state behavior.

## Platform

- Add deployment smoke checks through the VPN-only UI endpoint at `192.168.66.3:8085`.
- Add reverse proxy routing for `volt-event` only if the UI is intentionally exposed outside the TrueNAS LAN/VPN network.

## Sensors

- Add drain integration tests with mocked ahara-collector and InfluxDB HTTP endpoints.

## Management UI

- Add browser-level tests for event selection, timestamp offsets, and error display.
- Add a denser event history view backed by InfluxDB queries.
