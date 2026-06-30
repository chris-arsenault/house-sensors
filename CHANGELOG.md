# Changelog

All notable user-visible changes are recorded here.

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
