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
| `raw-archive-cleanup` | Looping archive/retention job with a persistent `raw-archive-state` Docker volume and read-only access to both downsampling state volumes. |

Deploys run from the shared Ahara workflow declared by [.github/workflows/ci.yml](../.github/workflows/ci.yml) and [platform.yml](../platform.yml). The workflow builds the component images, pushes them to GHCR under `ghcr.io/chris-arsenault/house-sensors/...`, sets `IMAGE_TAG` to the git SHA, resolves SSM-backed variables, and asks Komodo to deploy.

The stack is intentionally VPN-only. It does not have a `reverse_proxy_routes` entry in `ahara-infra`; reach `volt-event` through the LAN or WireGuard VPN at `http://192.168.66.3:8085/`.

## Grafana Dashboards

Dashboard source lives in [observability/dashboards](../observability/dashboards). The shared Ahara workflow reads the `observability.dashboards` registration in [platform.yml](../platform.yml) and deploys those dashboards to the `House Sensors` folder in shared Grafana through the platform dashboard deploy Lambda.

Do not copy these product dashboards into `ahara-observability`; that repo owns the Grafana runtime, datasources, and platform-level dashboards.

## App Telemetry

The Python collectors and background jobs emit application metrics over OTLP
HTTP to the local TrueNAS collector:

```text
http://192.168.66.3:4318
```

This is separate from the sensor readings written to InfluxDB. App telemetry
tracks service health and behavior: discovery cycles, polling results, Influx
write outcomes, downsampling cycle duration, rows/points processed, archive
uploads, and delete windows. The local Alloy collector in `ahara-observability`
routes those metrics to VictoriaMetrics.

The OTLP endpoint is fronted by the `ahara-observability` ingest gateway, which
requires a Cognito machine-to-machine (client_credentials) token with the
`observability/ingest` scope. Each telemetry service reads `OBS_INGEST_CLIENT_ID`
/ `OBS_INGEST_CLIENT_SECRET` (SSM `/ahara/observability/ingest-*`, see
[secret-paths.yml](../secret-paths.yml)); `app_telemetry.py` fetches and
auto-refreshes the token and attaches it as a bearer credential on OTLP export.
Without credentials configured (local/dev), export is unauthenticated as before.

Each service sets a stable `OTEL_SERVICE_NAME` in [compose.yaml](../compose.yaml)
under the `house-sensors` namespace. Keep metric attributes low-cardinality;
do not attach raw device IPs, sensor IDs, or S3 object keys to app telemetry.
Operational health appears in the shared `Ahara Telemetry Overview` dashboard;
the dashboards in this repo stay focused on sensor and power data.

## Secrets

Committed secret references live in [secret-paths.yml](../secret-paths.yml). Komodo resolves those paths into stack environment variables before deployment.

Use `.env.example` only for local Compose validation. Keep real tokens, passwords, and usernames in SSM.

The stack currently uses `/ahara/observability/influxdb-admin-token` for InfluxDB writes and deletes. The raw archive S3 bucket name is created by project Terraform and resolved from `/ahara/house-sensors/raw-archive/s3-bucket`. S3 credentials are obtained through Ahara TrueNAS IAM Roles Anywhere at container boot, not through SSM-stored AWS access keys. The Grafana, OTLP, and InfluxDB admin password observability parameters are not bound because no house-sensors service reads them.

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

The configured Influx URL is the Ahara observability compatibility endpoint,
`http://192.168.66.3:18086`, defined once in `compose.yaml` and reused by all
writers.

## Downsampling

The `downsampling-medium` service reads both raw sensor buckets, writes normalized per-minute points to `sensors-medium`, and preserves anomalous seconds. On first boot, it backfills `DOWNSAMPLE_MEDIUM_DAYS_BACK` days. After that, it uses the saved `last_stop_iso` watermark in `/state/raw_to_medium_state.json`, overlaps the last few minutes for late points, and processes newly completed minutes.

The `downsampling-long` service reads `sensors-medium`, writes hourly points to `sensors-long`, and preserves anomalous minute and second detail. On first boot, it backfills `DOWNSAMPLE_DAYS_BACK` days. After that, it uses the saved `last_stop_iso` watermark in `/state/medium_to_long_state.json` and processes newly completed hours.

Both jobs learn thresholds from observed data and store thresholds, coverage bounds, and watermarks in their state files. Delete or edit the `downsampling-medium-state` or `downsampling-long-state` volumes only when intentionally resetting learned thresholds and watermarks.

## Archive And Retention

The `raw-archive-cleanup` service archives only raw buckets: `environment-data` and `voltage-data`. It writes gzipped Influx line-protocol objects under `s3://${RAW_ARCHIVE_S3_BUCKET}/house-sensors/raw/...`. The S3 bucket is Terraform-managed; the runtime container does not create or configure it.

The archive container starts through `truenas-roles-anywhere-bootstrap`. The helper stores its private key and certificate in the `raw-archive-aws-identity` volume, writes an AWS SDK `credential_process` profile, and then runs the Python job with temporary AWS credentials.

Current deployment is in archive-validation mode: `RAW_ARCHIVE_DELETE_ENABLED=false`. The job uploads raw windows to S3 and advances archive watermarks, but it does not call the InfluxDB delete API or advance raw/medium delete watermarks. Turn deletion on only after the S3 archive contents have been validated.

The service deletes raw InfluxDB data only after all of these are true:

1. The raw window is older than `RAW_ARCHIVE_RAW_RETENTION_DAYS`, currently 30 days.
2. `downsampling-medium` has recorded coverage for the raw window.
3. `downsampling-long` has recorded coverage for the same window.
4. The raw window has been uploaded to S3 and recorded in `raw-archive-state`.

Medium data is not backed up. The same service deletes `sensors-medium` data older than `RAW_ARCHIVE_MEDIUM_RETENTION_MONTHS`, currently six months, only after `downsampling-long` has recorded coverage for that window. `sensors-long` has no cleanup path and is retained indefinitely.

## Health Checks

The Python images define import healthchecks so missing runtime dependencies are visible at container level. The nginx template exposes `/health`, returning `OK`.

## Local Validation

Run:

```bash
make ci
```

The `volt-event` Dockerfile validates the rendered nginx template during image build with placeholder values. This catches template syntax errors without requiring a live container.
