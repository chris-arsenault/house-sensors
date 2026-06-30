output "raw_archive_bucket" {
  description = "S3 bucket receiving raw InfluxDB archives."
  value       = aws_s3_bucket.raw_archive.bucket
}

output "raw_archive_workload_role" {
  description = "IAM Roles Anywhere workload role used by the TrueNAS raw archive job."
  value       = module.truenas_raw_archive_role.role_name
}
