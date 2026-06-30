# Development

## Setup

```bash
make dev-install
```

The dev dependencies are intentionally small: Ruff, pytest, PyYAML, and requests.

## Validation

```bash
make ci
```

This command runs:

| Step | Command |
| ---- | ---- |
| Compose validation | `docker compose --env-file .env.example -f compose.yaml config` |
| Python lint | `python3 -m ruff check collectors tests` |
| Firmware syntax | `python3 -c "compile(...)"` |
| Shell syntax | `sh -n management/volt-event/docker-entrypoint.sh` |
| Tests | `python3 -m pytest` |
| Image builds | `docker build` for every component image |

## Adding Collector Code

Keep each deployable collector in its own component directory under `collectors/`. A collector directory contains:

| File | Purpose |
| ---- | ---- |
| `Dockerfile` | Component image packaging. |
| `requirements.txt` | Runtime Python dependencies for that component. |
| Collector source | Long-running collector entry point copied into the image. |
| `.dockerignore` | Build-context exclusions. |

Add unit tests for parsing, conversion, and config behavior under `tests/`. Keep tests network-free.

## Adding Firmware Code

Keep device firmware under `firmware/<device>/`. A firmware directory contains:

| File | Purpose |
| ---- | ---- |
| `main.py` | Device firmware entry point copied to the board. |
| `secrets.example.py` | Template for local device credentials and identifiers. |
| `README.md` | Device behavior and deployment notes. |

Keep real `secrets.py` files out of git. Run `make lint` after firmware edits so syntax is checked without importing MicroPython-only modules.

## Adding Management UI Code

Keep nginx-hosted UI code under `management/<name>/`. The `volt-event` component bakes static files and the nginx template into the image. Its Dockerfile renders the template with placeholder values and runs `nginx -t` during build.

## Compose Changes

When adding an environment variable:

1. Add the runtime variable to `compose.yaml`.
2. Add a safe placeholder to `.env.example`.
3. Add an SSM path to `secret-paths.yml` when the value is secret-backed.
4. Run `make ci`.
