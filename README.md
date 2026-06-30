# House Sensors

TrueNAS-hosted environment sensor stack and device firmware for collecting temperature, humidity, voltage, and house event data into InfluxDB.

## Quickstart

```bash
make dev-install
make ci
```

`make ci` validates the Compose stack, Grafana dashboard JSON, firmware syntax, Python lint/tests, and collector/management UI image builds.

## Components

| Component | Purpose |
| ---- | ---- |
| `firmware/atoms3u-env3` | MicroPython firmware for M5 AtomS3U ENV-III sensor devices. |
| `environment-sensors` | Discovers HTTP environment sensors and writes temperature, humidity, and pressure readings to InfluxDB. |
| `volt` | Discovers Kasa energy-monitoring plugs and writes voltage, current, power, and total energy readings to InfluxDB. |
| `volt-event` | Nginx-hosted house event logger UI that proxies event writes to InfluxDB. |
| `downsampling-medium` | Raw-to-medium rollup job that reads environment and voltage buckets, emits per-minute aggregates, and preserves anomalous seconds. |
| `downsampling-long` | Medium-to-long rollup job that emits hourly aggregates and preserves anomalous minute/second detail. |
| `raw-archive-cleanup` | Archives raw InfluxDB data to S3, then enforces 30-day raw and 6-month medium retention after rollups are covered. |

## Documentation

| Topic | Link |
| ---- | ---- |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Development | [docs/development.md](docs/development.md) |
| Operations | [docs/operations.md](docs/operations.md) |
| Architecture decisions | [docs/adr/README.md](docs/adr/README.md) |
| Backlog | [docs/backlog.md](docs/backlog.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |
| Agent guide | [AGENTS.md](AGENTS.md) |

## Deployment

The deployable surface is [compose.yaml](compose.yaml), [infrastructure/terraform](infrastructure/terraform), and product-owned Grafana dashboards in [observability/dashboards](observability/dashboards). [platform.yml](platform.yml) and [.github/workflows/ci.yml](.github/workflows/ci.yml) use the shared Ahara workflow to apply Terraform, build GHCR images, resolve [secret-paths.yml](secret-paths.yml), deploy the Komodo stack, and publish dashboards to the shared Grafana instance from `main`.

The stack is VPN-only like Harbor: `volt-event` binds to `192.168.66.3:8085`, and this repo does not register an Ahara reverse-proxy route.

## License

Private repository. No open-source license is granted.
