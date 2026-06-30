data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "raw_archive" {
  bucket = local.raw_archive_bucket_name
}

resource "aws_s3_bucket_public_access_block" "raw_archive" {
  bucket                  = aws_s3_bucket.raw_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {
      prefix = local.raw_archive_prefix
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "raw_archive_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.raw_archive.arn,
      "${aws_s3_bucket.raw_archive.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "raw_archive" {
  bucket = aws_s3_bucket.raw_archive.id
  policy = data.aws_iam_policy_document.raw_archive_bucket.json
}

data "aws_iam_policy_document" "raw_archive_runtime" {
  statement {
    sid    = "ReadArchiveBucketMetadata"
    effect = "Allow"
    actions = [
      "s3:GetBucketLocation",
      "s3:ListBucketMultipartUploads",
    ]
    resources = [aws_s3_bucket.raw_archive.arn]
  }

  statement {
    sid    = "WriteArchiveObjects"
    effect = "Allow"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.raw_archive.arn}/${local.raw_archive_prefix}*"]
  }
}

module "truenas_raw_archive_role" {
  source = "git::https://github.com/chris-arsenault/ahara-infra.git//infrastructure/terraform/modules/truenas-roles-anywhere-workload?ref=main"

  prefix      = local.prefix
  name        = "raw-archive"
  policy_json = data.aws_iam_policy_document.raw_archive_runtime.json
}

resource "aws_ssm_parameter" "raw_archive_s3_bucket" {
  name  = "${local.ssm_prefix}/raw-archive/s3-bucket"
  type  = "String"
  value = aws_s3_bucket.raw_archive.bucket
}
