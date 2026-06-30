# 0001 — Komodo-managed TrueNAS stack

- Status: Accepted
- Date: 2026-06-30

## Context

House Sensors runs on TrueNAS and needs long-running collectors with local network discovery. The stack also needs secret-backed InfluxDB and device credentials without committing values to the repository.

The Ahara platform standard for TrueNAS services is Docker Compose managed by Komodo, with image tags supplied by the deploy workflow and secret values resolved from SSM.

## Decision

Package each deployable component as a self-contained image and deploy the stack through Komodo using `compose.yaml`, `${IMAGE_TAG}`, `platform.yml`, and `secret-paths.yml`.

Expose the management UI only on the TrueNAS LAN/VPN address. Do not register a reverse-proxy route unless the access model changes.

## Alternatives considered

- **TrueNAS custom apps with host-mounted source** — simple for manual iteration, but source and runtime state are coupled to a specific host path.
- **Single combined image** — reduces image count, but couples independent collectors and the UI into one build and deployment unit.
- **Direct secret values in Compose** — easy to run manually, but does not meet the repository secret-handling requirements.

## Consequences

Each component has its own Docker context and can be tested independently. Runtime secrets are resolved by the deploy path rather than stored in files. Compose validation requires placeholder values in `.env.example`. The deployed UI remains reachable only from the LAN or WireGuard VPN.
