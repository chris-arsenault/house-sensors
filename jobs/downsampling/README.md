# Sensor Data Jobs

This directory contains the sensor rollup and retention jobs. They are direct
Python processes for the Komodo-managed TrueNAS stack.

`raw_to_medium.py` reads raw environment and voltage/power buckets, writes the
normalized medium schema, learns minute thresholds during the initial backfill,
and then uses the saved thresholds for incremental per-minute population.

`medium_to_long_backfill.py` reads `resolution=1m` and `resolution=1s` points
from the medium bucket and writes long-term points into the long bucket.

`raw_archive_cleanup.py` backs up raw buckets to the Terraform-managed S3
archive bucket as gzipped Influx line protocol, then deletes archived raw data
older than 30 days. It also deletes medium data older than six months after long
rollups cover that window. It does not back up medium data. In TrueNAS, S3
access is provided by Ahara IAM Roles Anywhere through the container bootstrap
helper, not by static AWS access keys.

Medium-to-long behavior:

- calm hours become `resolution=1h` `min`, `max`, `mean`, and `computed` points;
- anomalous hours preserve the source `resolution=1m` points;
- source `resolution=1s` anomaly points always pass through;
- learned hour thresholds and the processed watermark are stored in
  `DOWNSAMPLE_STATE_FILE`.
- the destination bucket is created at startup when
  `DOWNSAMPLE_ENSURE_DST_BUCKET=true`.

Retention behavior:

- current deployment sets `RAW_ARCHIVE_DELETE_ENABLED=false` while S3 archives are validated;
- raw buckets are retained in InfluxDB for 30 days and backed up to S3 before deletion;
- medium bucket data is retained for six months and is not backed up;
- long bucket data is retained indefinitely;
- cleanup waits for downsampling coverage state before deleting source data.

The container runs `run-loop` by default. Use `run-once` for backfills or local
inspection:

```bash
python raw_to_medium.py run-once --start 2025-08-01T00:00:00Z --end 2025-10-01T00:00:00Z
python medium_to_long_backfill.py run-once --start 2025-08-01T00:00:00Z --end 2025-10-01T00:00:00Z
python raw_archive_cleanup.py run-once --dry-run
```
