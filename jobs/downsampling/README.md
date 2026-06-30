# Downsampling Job

This directory contains the sensor rollup jobs. Both are direct Python
processes for the Komodo-managed TrueNAS stack.

`raw_to_medium.py` reads raw environment and voltage/power buckets, writes the
normalized medium schema, learns minute thresholds during the initial backfill,
and then uses the saved thresholds for incremental per-minute population.

`medium_to_long_backfill.py` reads `resolution=1m` and `resolution=1s` points
from the medium bucket and writes long-term points into the long bucket.

Medium-to-long behavior:

- calm hours become `resolution=1h` `min`, `max`, `mean`, and `computed` points;
- anomalous hours preserve the source `resolution=1m` points;
- source `resolution=1s` anomaly points always pass through;
- learned hour thresholds and the processed watermark are stored in
  `DOWNSAMPLE_STATE_FILE`.
- the destination bucket is created at startup when
  `DOWNSAMPLE_ENSURE_DST_BUCKET=true`.

The container runs `run-loop` by default. Use `run-once` for backfills or local
inspection:

```bash
python raw_to_medium.py run-once --start 2025-08-01T00:00:00Z --end 2025-10-01T00:00:00Z
python medium_to_long_backfill.py run-once --start 2025-08-01T00:00:00Z --end 2025-10-01T00:00:00Z
```
