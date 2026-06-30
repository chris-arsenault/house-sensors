# Backlog

Planned-but-not-built work. Each item is a positive assertion of future-state behavior.

## Platform

- Add `platform.yml` for the Ahara shared workflow with three TrueNAS images.
- Add `.github/workflows/ci.yml` that calls `chris-arsenault/ahara/.github/workflows/ci.yml@main`.
- Register the project with the Ahara control-layer deployer role for Komodo deployment.
- Add reverse proxy routing for `volt-event` when the UI is exposed outside the TrueNAS network.

## Sensors

- Add explicit device allowlists for collectors that should poll known sensors only.
- Add collector metrics for discovery counts, write failures, and last successful write time.
- Add integration tests with mocked sensor and InfluxDB HTTP endpoints.

## Management UI

- Add browser-level tests for event selection, timestamp offsets, and error display.
- Add a denser event history view backed by InfluxDB queries.
