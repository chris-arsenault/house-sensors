# Machine identities for the containers that hold secrets.
#
# Each of these reads its own configuration at start with the certificate the
# trust appliance issued it, rather than receiving values the deploy pipeline
# resolved and wrote into Komodo (ahara-trust ADR-0002). The role grants
# nothing beyond reading this project's parameters, which the module derives
# from the prefix — no list of parameters is written anywhere.
#
# `raw-archive` is declared in raw_archive.tf because it also writes to S3.

locals {
  # Containers that read a secret. The value is what the container's
  # environment variables are prefixed with, matching platform.yml.
  secret_reading_workloads = {
    environment-sensors = "AWS_RA_ENVIRONMENT_SENSORS"
    volt                = "AWS_RA_VOLT"
    volt-event          = "AWS_RA_VOLT_EVENT"
    downsampling        = "AWS_RA_DOWNSAMPLING"
  }
}

module "workload_role" {
  for_each = local.secret_reading_workloads

  source = "git::https://github.com/chris-arsenault/ahara-infra.git//infrastructure/terraform/modules/machine-role?ref=main"

  prefix = local.prefix
  name   = each.key
  # No policy: reading this project's parameters is all these containers do
  # with credentials, and the module grants that from the prefix.

  # The collectors write to the household InfluxDB and authenticate to the
  # observability ingest gateway. Both are that project's parameters, and the
  # boundary in ahara-infra has to name the same project or this grants
  # nothing.
  cross_project_parameter_prefixes = ["observability"]

  permissions_boundary_arn = (
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/pb-${local.prefix}-truenas-workload"
  )
}
