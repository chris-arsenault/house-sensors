# 0002 — Device-facing collection runs on the IoT appliance

- Status: Accepted
- Date: 2026-08-16

## Context

The original TrueNAS collectors discovered and polled devices across routed
network boundaries. That required protocol-specific firewall flows, put IoT
credentials on a general service host, and coupled downstream storage code to
each device's network behavior.

The ahara-collector appliance now resides on the IoT LAN and owns device
discovery, credentials, polling, and bounded reading spools.

## Decision

The `environment-sensors` and `volt` services remain independently deployable
TrueNAS containers, but act as drain consumers. Each pulls only its assigned
module stream from ahara-collector, acknowledges a batch after its InfluxDB
write succeeds, and owns the conversion from device-native readings to the
House Sensors schema.

TrueNAS receives no direct IoT-device flow. A scoped bearer token authorizes the
collector API; device credentials remain on the appliance.

## Alternatives considered

- Direct polling from TrueNAS would retain cross-VLAN discovery and device
  credential access on the service host.
- Moving the InfluxDB schema into ahara-collector would couple a network
  boundary appliance to downstream storage semantics.
- Pushing readings from the appliance into InfluxDB would place an upstream
  database credential on the IoT LAN.

## Consequences

- The TrueNAS containers use the default bridge network and need only the
  collector API plus InfluxDB.
- ahara-collector can evolve device protocols without changing the stored data
  contract, while House Sensors retains schema ownership.
- At-least-once batch delivery requires idempotent writes before acknowledgment.
