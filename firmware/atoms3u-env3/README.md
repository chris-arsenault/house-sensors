# AtomS3U ENV-III Firmware

MicroPython firmware for an M5 AtomS3U with ENV-III sensor unit.

## Files

| File | Purpose |
| ---- | ---- |
| `main.py` | Device firmware entry point. |
| `secrets.example.py` | Template for local Wi-Fi, API auth, and device ID settings. |
| `secrets.py` | Local device secrets file copied to the board and ignored by git. |

## Device Behavior

- Connects to Wi-Fi as a station.
- Responds to UDP discovery on port `12343`.
- Serves Basic Auth protected HTTP endpoints on port `80`.
- Reads temperature, humidity, and pressure from the ENV-III unit.
- Persists tag key/value pairs in NVS with a file fallback.
- Maintains high-resolution timestamps from NTP plus monotonic ticks.

## HTTP Endpoints

| Endpoint | Purpose |
| ---- | ---- |
| `/` | Device status, uptime, current tags, and endpoint list. |
| `/sensors` | Current temperature, humidity, pressure, timestamps, and tags. |
| `/config` | Tag updates through query parameters or JSON body. |

## Secrets

Create a local `secrets.py` from the template:

```python
WIFI_SSID = "your-wifi-ssid"
WIFI_PASSWORD = "your-wifi-password"
AUTH_USERNAME = "admin"
AUTH_PASSWORD = "device-api-password"
DEVICE_ID = "ATOM3U-ENV3-005"
```

Copy both `main.py` and `secrets.py` to the device. Do not commit `secrets.py`.
