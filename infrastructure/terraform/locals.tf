locals {
  prefix                  = "house-sensors"
  raw_archive_bucket_name = "${local.prefix}-raw-archive-${data.aws_caller_identity.current.account_id}"
  raw_archive_prefix      = "house-sensors/raw/"
  ssm_prefix              = "/ahara/house-sensors"
}
