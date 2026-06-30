# Architecture

House Sensors is a Komodo-managed Docker Compose stack for TrueNAS. It runs two Python polling collectors and one nginx-hosted management UI.

## Components

| Service | Source | Image | Runtime network |
| ---- | ---- | ---- | ---- |
| `environment-sensors` | `collectors/environment-sensors/` | `ghcr.io/chris-arsenault/house-sensors/environment-sensors:${IMAGE_TAG}` | `host` |
| `volt` | `collectors/volt/` | `ghcr.io/chris-arsenault/house-sensors/volt:${IMAGE_TAG}` | `host` |
| `volt-event` | `management/volt-event/` | `ghcr.io/chris-arsenault/house-sensors/volt-event:${IMAGE_TAG}` | Published on host port `8085` |

## Data Flow

`environment-sensors` sends UDP discovery packets on the sensor subnet, validates `/sensors` responses, converts readings to Influx line protocol, and writes to the `environment-data` bucket.

`volt` discovers authenticated Kasa devices with energy-monitoring support, reads voltage/current/power/total energy data, and writes points to the `voltage-data` bucket.

`volt-event` serves a static event logger UI. Browser requests post line protocol to `/api/influx/write`; nginx proxies those writes to InfluxDB with the token supplied by Komodo.

## Configuration

Runtime configuration is supplied through Compose environment variables. Secret values are not committed. `secret-paths.yml` maps stack environment variables to SSM paths resolved by the Ahara Komodo deploy action.

| Variable | Service | Source |
| ---- | ---- | ---- |
| `ENV_SENSOR_INFLUX_TOKEN` | `environment-sensors` | SSM |
| `ENV_SENSOR_DEVICE_USER` | `environment-sensors` | SSM |
| `ENV_SENSOR_DEVICE_PASS` | `environment-sensors` | SSM |
| `VOLT_INFLUXDB_TOKEN` | `volt` | SSM |
| `KASA_USERNAME` | `volt` | SSM |
| `KASA_PASSWORD` | `volt` | SSM |
| `VOLT_EVENT_INFLUX_TOKEN` | `volt-event` | SSM |

## Image Packaging

Each component builds from its own directory. The Python collector images install only their component requirements and copy the collector source into `/app`. The nginx image copies `index.html` and `nginx.conf.template` into the image and validates the rendered nginx config during the build.

The stack uses `${IMAGE_TAG}` so all services deploy the same git SHA when published by the Ahara TrueNAS workflow.
