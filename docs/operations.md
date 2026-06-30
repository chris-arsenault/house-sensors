# Operations

## TrueNAS Deployment

Komodo deploys [compose.yaml](../compose.yaml) on TrueNAS. The stack follows the Ahara TrueNAS deployment model documented in `../ahara/TRUENAS-DEPLOY.md`.

| Service | Host exposure |
| ---- | ---- |
| `environment-sensors` | Host network for UDP discovery and device polling. |
| `volt` | Host network for Kasa discovery and polling. |
| `volt-event` | Host port `8085` mapped to container port `80`. |

## Secrets

Committed secret references live in [secret-paths.yml](../secret-paths.yml). Komodo resolves those paths into stack environment variables before deployment.

Use `.env.example` only for local Compose validation. Keep real tokens, passwords, and usernames in SSM.

## InfluxDB

The collectors and event UI write to InfluxDB buckets:

| Bucket | Writer |
| ---- | ---- |
| `environment-data` | `environment-sensors` |
| `voltage-data` | `volt`, `volt-event` |

The configured Influx URLs are in `compose.yaml`.

## Health Checks

The Python images define import healthchecks so missing runtime dependencies are visible at container level. The nginx template exposes `/health`, returning `OK`.

## Local Validation

Run:

```bash
make ci
```

The `volt-event` Dockerfile validates the rendered nginx template during image build with placeholder values. This catches template syntax errors without requiring a live container.
