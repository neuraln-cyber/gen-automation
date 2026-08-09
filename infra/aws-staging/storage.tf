resource "aws_s3_bucket" "assets" {
  bucket        = local.asset_bucket_name
  force_destroy = false

  tags = {
    Name               = local.asset_bucket_name
    DataClassification = "private-generated-assets"
  }
}

resource "aws_s3_bucket_ownership_controls" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket = aws_s3_bucket.assets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # A successful collector promotes the exact upload version into masters/ and
  # deletes that staging version. Anything left under staging/ after this
  # bounded grace period is an abandoned upload attempt, not a raw master or a
  # user-facing derivative/package. Prefix scoping is deliberately explicit:
  # masters/, derivatives/, finished-set-archives/, and publication-packages/
  # have no expiry rule.
  rule {
    id     = "expire-abandoned-staging-uploads"
    status = "Enabled"

    filter {
      prefix = "staging/"
    }

    expiration {
      days = var.abandoned_staging_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.abandoned_staging_retention_days
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "assets" {
  count = local.browser_upload_origin == null ? 0 : 1

  bucket = aws_s3_bucket.assets.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["GET", "HEAD", "POST", "PUT"]
    allowed_origins = [local.browser_upload_origin]
    expose_headers  = ["ETag", "x-amz-version-id"]
    max_age_seconds = 600
  }
}

data "aws_iam_policy_document" "assets_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.assets.arn,
      "${aws_s3_bucket.assets.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "assets" {
  bucket = aws_s3_bucket.assets.id
  policy = data.aws_iam_policy_document.assets_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.assets]
}

resource "aws_s3_bucket" "models" {
  bucket        = local.model_bucket_name
  force_destroy = false

  tags = {
    Name               = local.model_bucket_name
    DataClassification = "private-model-artifacts"
  }
}

resource "aws_s3_bucket_ownership_controls" "models" {
  bucket = aws_s3_bucket.models.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "models" {
  bucket = aws_s3_bucket.models.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "models" {
  bucket = aws_s3_bucket.models.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "models" {
  bucket = aws_s3_bucket.models.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "models" {
  bucket = aws_s3_bucket.models.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }


  # Browser uploads and interrupted provider imports are quarantine objects,
  # never durable catalog entries. The controller normally deletes the exact
  # version immediately after promotion; this bounds storage after a crash.
  rule {
    id     = "expire-abandoned-lora-onboarding"
    status = "Enabled"

    filter {
      prefix = "onboarding/loras/"
    }

    expiration {
      days = var.abandoned_staging_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = var.abandoned_staging_retention_days
    }
  }
}

resource "aws_s3_bucket_cors_configuration" "models" {
  count = local.browser_upload_origin == null ? 0 : 1

  bucket = aws_s3_bucket.models.id

  cors_rule {
    allowed_headers = ["*"]
    allowed_methods = ["POST"]
    allowed_origins = [local.browser_upload_origin]
    expose_headers  = ["ETag", "x-amz-version-id"]
    max_age_seconds = 600
  }
}

data "aws_iam_policy_document" "models_bucket" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.models.arn,
      "${aws_s3_bucket.models.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "models" {
  bucket = aws_s3_bucket.models.id
  policy = data.aws_iam_policy_document.models_bucket.json

  depends_on = [aws_s3_bucket_public_access_block.models]
}
