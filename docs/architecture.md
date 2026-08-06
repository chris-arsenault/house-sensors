# Architecture

House Sensors includes MicroPython sensor firmware and a Komodo-managed Docker Compose stack for TrueNAS. The TrueNAS stack runs two Python drain collectors, three Python sensor data jobs, and one nginx-hosted management UI. Device-facing work — discovery, credentials, polling — lives on the ahara-collector appliance on the home LAN; this stack owns the data schema and everything downstream (ahara-collector ADR-0006).

## Components

| Service | Source | Image | Runtime network |
| ---- | ---- | ---- | ---- |
| `environment-sensors` | `collectors/environment-sensors/` | `ghcr.io/chris-arsenault/house-sensors/collectors/environment-sensors:${IMAGE_TAG}` | Default bridge network |
| `volt` | `collectors/volt/` | `ghcr.io/chris-arsenault/house-sensors/collectors/volt:${IMAGE_TAG}` | Default bridge network |
| `volt-event` | `management/volt-event/` | `ghcr.io/chris-arsenault/house-sensors/management/volt-event:${IMAGE_TAG}` | Published on `192.168.66.3:8085` |
| `downsampling-medium` | `jobs/downsampling/` | `ghcr.io/chris-arsenault/house-sensors/jobs/downsampling:${IMAGE_TAG}` | Default bridge network |
| `downsampling-long` | `jobs/downsampling/` | `ghcr.io/chris-arsenault/house-sensors/jobs/downsampling:${IMAGE_TAG}` | Default bridge network |
| `raw-archive-cleanup` | `jobs/downsampling/` | `ghcr.io/chris-arsenault/house-sensors/jobs/downsampling:${IMAGE_TAG}` | Default bridge network |

## Firmware

`firmware/atoms3u-env3/main.py` runs on M5 AtomS3U ENV-III devices. It connects to Wi-Fi, serves Basic Auth protected HTTP endpoints, responds to UDP discovery on port `12343`, and persists tag key/value pairs on the device.

The firmware exposes `/sensors` with temperature, humidity, pressure, timestamps, and tags. The ahara-collector appliance discovers and polls these devices with the shared device credentials; the `environment-sensors` collector consumes the resulting readings.

## Data Flow

The devices sit on the home LAN behind the ahara-collector appliance, which discovers them, holds their credentials, polls them, and spools one device-native JSON reading envelope per reading — one bounded stream per module. Each collector here drains its own stream over the appliance's authenticated API (`GET /readings/next?module=<name>`, then `POST /readings/ack` after every write lands; unacked batches are re-served, and duplicate writes are idempotent). The envelope format and drain contract are specified in ahara-collector `docs/integration.md`.

`environment-sensors` drains the `envSensors` stream, maps each envelope to the `environment` measurement — field names, firmware key aliases, and the corrected-sample audit field — and writes to the `environment-data` bucket.

`volt` drains the `kasa` stream, converts the vendor-unit energy payload (mW, Wh, mV, mA) to `voltage_monitoring` fields in W, kWh, V, and A, and writes points to the `voltage-data` bucket.

`volt-event` serves a static event logger UI. Browser requests post line protocol to `/api/influx/write`; nginx proxies those writes to InfluxDB with the token supplied by Komodo.

`downsampling-medium` reads the raw `environment-data` and `voltage-data` buckets, normalizes both domains into the `sensors-medium` bucket, writes calm series as `resolution=1m` aggregate points, and preserves anomalous readings as `resolution=1s` points. It stores learned minute thresholds, coverage bounds, and a processed watermark in the `downsampling-medium-state` Docker volume.

`downsampling-long` reads the normalized `sensors-medium` bucket and writes long-term points to `sensors-long`. It writes calm series as `resolution=1h` aggregate points and preserves anomalous `resolution=1m` and `resolution=1s` detail. It stores learned hour thresholds, coverage bounds, and a processed watermark in the `downsampling-long-state` Docker volume.

`raw-archive-cleanup` reads the downsampling coverage state, exports covered raw bucket windows to gzipped Influx line-protocol objects in a Terraform-managed S3 bucket, and then deletes only archived raw data older than 30 days. It also deletes medium data older than six months after the long rollup state covers that window. Medium data is not backed up to S3; long data is retained indefinitely.

The stack follows Harbor's VPN-only TrueNAS pattern. The UI is reachable on the TrueNAS LAN/VPN address, and no `reverse_proxy_routes` entry is registered in `ahara-infra`.

## Configuration

Runtime configuration is supplied through Compose environment variables. Secret values are not committed. `secret-paths.yml` maps stack environment variables to SSM paths resolved by the Ahara Komodo deploy action.

Firmware device credentials are supplied through an ignored `secrets.py` copied to each device. The committed `secrets.example.py` shows the expected fields.

Project Terraform owns the raw archive S3 bucket and the least-privilege TrueNAS IAM Roles Anywhere workload role. Komodo resolves the bucket name from SSM and injects the short-lived Roles Anywhere enrollment values declared in `platform.yml`; no long-lived AWS access keys are passed to the container.

| Variable | Service | SSM path |
| ---- | ---- | ---- |
| `ENV_SENSOR_INFLUX_TOKEN` | `environment-sensors` | `/ahara/observability/influxdb-admin-token` |
| `VOLT_INFLUXDB_TOKEN` | `volt` | `/ahara/observability/influxdb-admin-token` |
| `COLLECTOR_TOKEN` | `environment-sensors`, `volt` | `/ahara/house-sensors/collector/api-token` |
| `VOLT_EVENT_INFLUX_TOKEN` | `volt-event` | `/ahara/observability/influxdb-admin-token` |
| `DOWNSAMPLER_INFLUX_TOKEN` | `downsampling-medium`, `downsampling-long`, `raw-archive-cleanup` | `/ahara/observability/influxdb-admin-token` |
| `RAW_ARCHIVE_S3_BUCKET` | `raw-archive-cleanup` | `/ahara/house-sensors/raw-archive/s3-bucket` |

The device credential parameters (`/ahara/house-sensors/environment-sensors/device-*`, `/ahara/house-sensors/volt/kasa-*`) stay in SSM but are no longer stack environment: the ahara-collector appliance renders them into its host credentials file at install.

`raw-archive-cleanup` also receives `AWS_RA_RAW_ARCHIVE_*` environment variables from the shared Ahara TrueNAS deploy workflow. Those values are generated at deploy time from the `truenas_roles_anywhere.raw-archive` entry in `platform.yml`, not from `secret-paths.yml`.

## Image Packaging

Each component builds from its own directory. The Python collector images install only their component requirements and copy the collector source into `/app`. The nginx image copies `index.html` and `nginx.conf.template` into the image and validates the rendered nginx config during the build.

The stack uses `${IMAGE_TAG}` so all services deploy the same git SHA when published by the Ahara TrueNAS workflow. Local Compose rendering falls back to `latest` if `IMAGE_TAG` is not set.
