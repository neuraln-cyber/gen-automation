data "aws_iam_policy_document" "ec2_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control_plane" {
  name               = "${local.name}-control-plane"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume_role.json

  tags = {
    Name = "${local.name}-control-plane"
  }
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.control_plane.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

data "aws_iam_policy_document" "salad_worker_artifact_reader_assume" {
  count = local.salad_worker_artifact_role_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.control_plane.arn]
    }
  }
}

resource "aws_iam_role" "salad_worker_artifact_reader" {
  count = local.salad_worker_artifact_role_enabled ? 1 : 0

  name                 = "${local.name}-salad-artifact-reader"
  assume_role_policy   = data.aws_iam_policy_document.salad_worker_artifact_reader_assume[0].json
  max_session_duration = 10800

  tags = {
    Name = "${local.name}-salad-artifact-reader"
  }
}

data "aws_iam_policy_document" "salad_worker_artifact_reader" {
  count = local.salad_worker_artifact_role_enabled ? 1 : 0

  dynamic "statement" {
    for_each = var.salad_worker_artifact_object_versions

    content {
      sid       = "ReadPinnedArtifact${substr(sha256(statement.key), 0, 16)}"
      actions   = ["s3:GetObjectVersion"]
      resources = ["${aws_s3_bucket.models.arn}/${statement.key}"]

      condition {
        test     = "StringEquals"
        variable = "s3:VersionId"
        values   = [statement.value]
      }
    }
  }
}

resource "aws_iam_role_policy" "salad_worker_artifact_reader" {
  count = local.salad_worker_artifact_role_enabled ? 1 : 0

  name   = "${local.name}-pinned-artifacts"
  role   = aws_iam_role.salad_worker_artifact_reader[0].id
  policy = data.aws_iam_policy_document.salad_worker_artifact_reader[0].json
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid = "AssetBucketMetadata"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
      "s3:ListBucket",
    ]
    resources = [aws_s3_bucket.assets.arn]
  }

  statement {
    sid = "AssetObjects"
    actions = [
      "s3:DeleteObjectVersion",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.assets.arn}/*"]
  }

  statement {
    sid = "ModelBucketMetadata"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.models.arn]
  }

  statement {
    sid = "RdsManagedMasterSecretRead"
    actions = [
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetSecretValue",
    ]
    resources = [aws_db_instance.postgresql.master_user_secret[0].secret_arn]
  }

  dynamic "statement" {
    for_each = var.x_oauth_secret_arn == null ? [] : [var.x_oauth_secret_arn]

    content {
      sid = "XOAuthSecretRotation"
      actions = [
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "secretsmanager:UpdateSecretVersionStage",
      ]
      resources = [statement.value]
    }
  }

  dynamic "statement" {
    for_each = local.salad_worker_artifact_role_enabled ? [1] : []

    content {
      sid       = "AssumeSaladArtifactReader"
      actions   = ["sts:AssumeRole"]
      resources = [aws_iam_role.salad_worker_artifact_reader[0].arn]
    }
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "${local.name}-runtime"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.runtime.json
}

data "aws_iam_policy_document" "cloudwatch" {
  statement {
    sid = "WriteExactLogGroup"
    actions = [
      "logs:CreateLogStream",
      "logs:DescribeLogStreams",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.staging.arn}:*"]
  }

  statement {
    sid       = "WriteStagingMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.metric_namespace]
    }
  }

  statement {
    sid = "DescribeHostForMetrics"
    actions = [
      "ec2:DescribeTags",
      "ec2:DescribeVolumes",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "cloudwatch" {
  name   = "${local.name}-cloudwatch"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.cloudwatch.json
}

resource "aws_iam_instance_profile" "control_plane" {
  name = "${local.name}-control-plane"
  role = aws_iam_role.control_plane.name
}
