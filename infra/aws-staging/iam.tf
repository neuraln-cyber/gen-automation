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
    sid = "ModelObjects"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
    ]
    resources = ["${aws_s3_bucket.models.arn}/*"]
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
