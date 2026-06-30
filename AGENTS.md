# Agent Guide

TrueNAS-hosted environment sensor stack with MicroPython firmware, Python collectors, an nginx event UI, and Komodo-managed Docker Compose deployment.

## Read first

| Topic | Link |
| ---- | ---- |
| Workspace overview | [README.md](README.md) |
| Documentation index | [docs/README.md](docs/README.md) |
| Architecture | [docs/architecture.md](docs/architecture.md) |
| Development | [docs/development.md](docs/development.md) |
| Operations | [docs/operations.md](docs/operations.md) |
| Architecture decisions | [docs/adr/README.md](docs/adr/README.md) |
| Backlog | [docs/backlog.md](docs/backlog.md) |
| Changelog | [CHANGELOG.md](CHANGELOG.md) |

## Critical rules

- Follow `../ahara/TRUENAS-DEPLOY.md` for TrueNAS, Komodo, Compose, GHCR, and SSM secret handling.
- Use the `main` branch for this repo's commits and pushes unless the user explicitly directs otherwise.
- Keep real secrets out of the repo. Add committed placeholders to `.env.example` and SSM paths to `secret-paths.yml`.
- Keep firmware device credentials in ignored `firmware/**/secrets.py` files and commit only `secrets.example.py` templates.
- Use `/opt/sulion/bin/with-cred -- ...` for commands that require API keys, cloud credentials, service tokens, or other broker-backed secrets.
- Run `make ci` before committing changes.
- Build deployed services as self-contained images. Keep runtime bind mounts out of `compose.yaml` unless the service intentionally needs host data.
- Do not run live collectors unless the user asks; they perform network discovery and poll devices.
- Preserve user work in the git tree. Do not use destructive git commands unless the user explicitly requests them.

## Code map

| Path | Purpose |
| ---- | ---- |
| `compose.yaml` | Komodo-deployed TrueNAS stack definition. |
| `platform.yml` | Ahara CI/CD manifest for the Komodo deploy path. |
| `.github/workflows/ci.yml` | Local repo checks plus the shared Ahara CI/CD workflow. |
| `secret-paths.yml` | SSM parameter paths for Komodo stack environment variables. |
| `.env.example` | Safe local placeholders for Compose validation. |
| `firmware/atoms3u-env3/` | MicroPython firmware for M5 AtomS3U ENV-III sensor devices. |
| `collectors/environment-sensors/` | Python environment sensor collector and image packaging. |
| `collectors/volt/` | Python Kasa voltage collector and image packaging. |
| `jobs/downsampling/` | Lightweight medium-to-long downsampling service and image packaging. |
| `management/volt-event/` | Nginx event logger UI and image packaging. |
| `tests/` | Unit tests for collector parsing, config, and conversion behavior. |
| `docs/` | Current-state architecture, development, operations, ADRs, and backlog. |

## Commands

| Command | Purpose |
| ---- | ---- |
| `make dev-install` | Install local test and lint dependencies. |
| `make lint` | Validate Compose, run Ruff, check firmware syntax, and shell-check the nginx entrypoint syntax. |
| `make test` | Run pytest. |
| `make ci` | Run lint, tests, and local image builds. |
| `docker compose --env-file .env.example -f compose.yaml config` | Validate rendered Compose configuration. |
