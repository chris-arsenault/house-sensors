# Operations

## TrueNAS Deployment

Komodo deploys [compose.yaml](../compose.yaml) on TrueNAS. The stack follows the Ahara TrueNAS deployment model documented in `../ahara/TRUENAS-DEPLOY.md`.

| Service | Host exposure |
| ---- | ---- |
| `environment-sensors` | Host network for UDP discovery and device polling. |
| `volt` | Host network for Kasa discovery and polling. |
| `volt-event` | TrueNAS LAN address `192.168.66.3:8085` mapped to container port `80`. |
| `downsampling-medium` | Looping raw-to-medium background job with a persistent `downsampling-medium-state` Docker volume. |
| `downsampling-long` | Looping medium-to-long background job with a persistent `downsampling-long-state` Docker volume. |

Deploys run from the shared Ahara workflow declared by [.github/workflows/ci.yml](../.github/workflows/ci.yml) and [platform.yml](../platform.yml). The workflow builds the component images, pushes them to GHCR under `ghcr.io/chris-arsenault/house-sensors/...`, sets `IMAGE_TAG` to the git SHA, resolves SSM-backed variables, and asks Komodo to deploy.

The stack is intentionally VPN-only. It does not have a `reverse_proxy_routes` entry in `ahara-infra`; reach `volt-event` through the LAN or WireGuard VPN at `http://192.168.66.3:8085/`.

## Secrets

Committed secret references live in [secret-paths.yml](../secret-paths.yml). Komodo resolves those paths into stack environment variables before deployment.

Use `.env.example` only for local Compose validation. Keep real tokens, passwords, and usernames in SSM.

The stack currently uses `/ahara/observability/influxdb-admin-token` for InfluxDB writes. The Grafana, OTLP, and InfluxDB admin password observability parameters are not bound because no house-sensors service reads them.

Firmware credentials live in device-local `secrets.py` files copied to the board. Keep those files out of git and use `firmware/atoms3u-env3/secrets.example.py` as the template.

## Firmware Devices

AtomS3U ENV-III devices run `firmware/atoms3u-env3/main.py`. Each device exposes:

| Endpoint | Purpose |
| ---- | ---- |
| `/` | Device status and tags. |
| `/sensors` | Current sensor reading payload consumed by the collector. |
| `/config` | Tag updates and deletes. |

The UDP discovery response includes device ID, model, current IP, API port, supported endpoints, timestamps, and tags.

## InfluxDB

The collectors and event UI write to InfluxDB buckets:

| Bucket | Writer |
| ---- | ---- |
| `environment-data` | `environment-sensors` |
| `voltage-data` | `volt`, `volt-event` |
| `sensors-medium` | Destination bucket for raw-to-medium rollups and source bucket for medium-to-long rollups. |
| `sensors-long` | Destination bucket for long-term rollups. |

The configured Influx URLs are in `compose.yaml`.

## Downsampling

The `downsampling-medium` service reads both raw sensor buckets, writes normalized per-minute points to `sensors-medium`, and preserves anomalous seconds. On first boot, it backfills `DOWNSAMPLE_MEDIUM_DAYS_BACK` days. After that, it uses the saved `last_stop_iso` watermark in `/state/raw_to_medium_state.json`, overlaps the last few minutes for late points, and processes newly completed minutes.

The `downsampling-long` service reads `sensors-medium`, writes hourly points to `sensors-long`, and preserves anomalous minute and second detail. On first boot, it backfills `DOWNSAMPLE_DAYS_BACK` days. After that, it uses the saved `last_stop_iso` watermark in `/state/medium_to_long_state.json` and processes newly completed hours.

Both jobs learn thresholds from observed data and store them in their state files. Delete or edit the `downsampling-medium-state` or `downsampling-long-state` volumes only when intentionally resetting learned thresholds and watermarks.

## Health Checks

The Python images define import healthchecks so missing runtime dependencies are visible at container level. The nginx template exposes `/health`, returning `OK`.

## Local Validation

Run:

```bash
make ci
```

The `volt-event` Dockerfile validates the rendered nginx template during image build with placeholder values. This catches template syntax errors without requiring a live container.
