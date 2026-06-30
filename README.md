# House Sensors

TrueNAS-hosted environment sensor stack and device firmware for collecting temperature, humidity, voltage, and house event data into InfluxDB.

## Quickstart

```bash
make dev-install
make ci
```

`make ci` validates the Compose stack, checks firmware syntax, runs Python lint and tests, and builds the collector and management UI images.

## Components

| Component | Purpose |
| ---- | ---- |
| `firmware/atoms3u-env3` | MicroPython firmware for M5 AtomS3U ENV-III sensor devices. |
| `environment-sensors` | Discovers HTTP environment sensors and writes temperature, humidity, and pressure readings to InfluxDB. |
| `volt` | Discovers Kasa energy-monitoring plugs and writes voltage, current, power, and total energy readings to InfluxDB. |
| `volt-event` | Nginx-hosted house event logger UI that proxies event writes to InfluxDB. |

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

The deployable surface is [compose.yaml](compose.yaml). [platform.yml](platform.yml) and [.github/workflows/ci.yml](.github/workflows/ci.yml) use the shared Ahara TrueNAS workflow to build GHCR images, resolve [secret-paths.yml](secret-paths.yml), and deploy the Komodo stack from `main`.

The stack is VPN-only like Harbor: `volt-event` binds to `192.168.66.3:8085`, and this repo does not register an Ahara reverse-proxy route.

## License

Private repository. No open-source license is granted.
