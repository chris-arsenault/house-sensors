# Architecture

House Sensors includes MicroPython sensor firmware and a Komodo-managed Docker Compose stack for TrueNAS. The TrueNAS stack runs two Python polling collectors, one Python downsampling job, and one nginx-hosted management UI.

## Components

| Service | Source | Image | Runtime network |
| ---- | ---- | ---- | ---- |
| `environment-sensors` | `collectors/environment-sensors/` | `ghcr.io/chris-arsenault/house-sensors/collectors/environment-sensors:${IMAGE_TAG}` | `host` |
| `volt` | `collectors/volt/` | `ghcr.io/chris-arsenault/house-sensors/collectors/volt:${IMAGE_TAG}` | `host` |
| `volt-event` | `management/volt-event/` | `ghcr.io/chris-arsenault/house-sensors/management/volt-event:${IMAGE_TAG}` | Published on `192.168.66.3:8085` |
| `downsampling` | `jobs/downsampling/` | `ghcr.io/chris-arsenault/house-sensors/jobs/downsampling:${IMAGE_TAG}` | Default bridge network |

## Firmware

`firmware/atoms3u-env3/main.py` runs on M5 AtomS3U ENV-III devices. It connects to Wi-Fi, serves Basic Auth protected HTTP endpoints, responds to UDP discovery on port `12343`, and persists tag key/value pairs on the device.

The firmware exposes `/sensors` with temperature, humidity, pressure, timestamps, and tags. The `environment-sensors` collector discovers these devices and writes their readings to InfluxDB.

## Data Flow

`environment-sensors` sends UDP discovery packets on the sensor subnet, validates `/sensors` responses, converts readings to Influx line protocol, and writes to the `environment-data` bucket.

`volt` discovers authenticated Kasa devices with energy-monitoring support, reads voltage/current/power/total energy data, and writes points to the `voltage-data` bucket.

`volt-event` serves a static event logger UI. Browser requests post line protocol to `/api/influx/write`; nginx proxies those writes to InfluxDB with the token supplied by Komodo.

`downsampling` reads the medium-resolution `sensors-medium` bucket and writes long-term points to `sensors-long`. It stores learned hour thresholds and a processed watermark in the `downsampling-state` Docker volume.

The stack follows Harbor's VPN-only TrueNAS pattern. The UI is reachable on the TrueNAS LAN/VPN address, and no `reverse_proxy_routes` entry is registered in `ahara-infra`.

## Configuration

Runtime configuration is supplied through Compose environment variables. Secret values are not committed. `secret-paths.yml` maps stack environment variables to SSM paths resolved by the Ahara Komodo deploy action.

Firmware device credentials are supplied through an ignored `secrets.py` copied to each device. The committed `secrets.example.py` shows the expected fields.

| Variable | Service | SSM path |
| ---- | ---- | ---- |
| `ENV_SENSOR_INFLUX_TOKEN` | `environment-sensors` | `/ahara/observability/influxdb-admin-token` |
| `ENV_SENSOR_DEVICE_USER` | `environment-sensors` | `/ahara/house-sensors/environment-sensors/device-user` |
| `ENV_SENSOR_DEVICE_PASS` | `environment-sensors` | `/ahara/house-sensors/environment-sensors/device-pass` |
| `VOLT_INFLUXDB_TOKEN` | `volt` | `/ahara/observability/influxdb-admin-token` |
| `KASA_USERNAME` | `volt` | `/ahara/house-sensors/volt/kasa-username` |
| `KASA_PASSWORD` | `volt` | `/ahara/house-sensors/volt/kasa-password` |
| `VOLT_EVENT_INFLUX_TOKEN` | `volt-event` | `/ahara/observability/influxdb-admin-token` |
| `DOWNSAMPLER_INFLUX_TOKEN` | `downsampling` | `/ahara/observability/influxdb-admin-token` |

## Image Packaging

Each component builds from its own directory. The Python collector images install only their component requirements and copy the collector source into `/app`. The nginx image copies `index.html` and `nginx.conf.template` into the image and validates the rendered nginx config during the build.

The stack uses `${IMAGE_TAG}` so all services deploy the same git SHA when published by the Ahara TrueNAS workflow. Local Compose rendering falls back to `latest` if `IMAGE_TAG` is not set.
