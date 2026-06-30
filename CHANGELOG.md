# Changelog

All notable user-visible changes are recorded here.

## v0.2.0 - 2026-06-30

### Stack

- Added the Windmill-free `downsampling` service for medium-to-long sensor rollups with persisted threshold state.
- Added Ahara CI/CD wiring for local validation, GHCR image publishing, and Komodo deployment from `main`.
- Updated the deployed UI port binding to the VPN-only TrueNAS LAN address.
- Bound all InfluxDB writer tokens to the shared observability SSM parameter `/ahara/observability/influxdb-admin-token`.

### Firmware

- Added AtomS3U ENV-III MicroPython firmware with UDP discovery, Basic Auth HTTP API, sensor reads, NTP timestamps, and persistent tags.
- Added firmware-local secret templating and syntax validation in `make ci`.

## v0.1.0 - 2026-06-30

### Stack

- Added a TrueNAS Compose stack for environment sensing, voltage collection, and house event logging.
- Added SSM-backed secret path mapping for Komodo deployment.

### Collectors

- Added the environment sensor collector image and tests for discovery parsing and Influx line protocol generation.
- Added the voltage collector image and tests for config handling and Kasa energy unit conversion.

### Management UI

- Added the nginx-hosted house event logger UI and InfluxDB proxy template.

### Documentation

- Added the repository documentation surface, operations guide, backlog, and architecture decision records.
