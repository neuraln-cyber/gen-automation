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

resource "aws_iam_openid_connect_provider" "github_actions" {
  count = local.github_actions_manages_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  tags = {
    Name = "${local.name}-github-actions"
  }
}

data "aws_iam_openid_connect_provider" "github_actions_existing" {
  count = local.github_actions_uses_existing_oidc_provider ? 1 : 0
  arn   = var.github_actions_oidc_provider_arn
}

data "aws_iam_policy_document" "github_actions_deploy_assume" {
  count = var.github_actions_deploy_enabled ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_actions_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_actions_deploy_subject]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository"
      values   = [var.github_actions_repository]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository_owner_id"
      values   = [tostring(var.github_actions_repository_owner_id)]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository_id"
      values   = [tostring(var.github_actions_repository_id)]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:workflow"
      values   = [local.github_actions_deploy_workflow]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:ref"
      values   = ["refs/heads/main"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  count = var.github_actions_deploy_enabled ? 1 : 0

  name                 = "${local.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_actions_deploy_assume[0].json
  max_session_duration = 3600

  tags = {
    Name = "${local.name}-github-deploy"
  }
}

data "aws_iam_policy_document" "github_actions_deploy" {
  count = var.github_actions_deploy_enabled ? 1 : 0

  statement {
    sid     = "SendExactStagingDeployCommand"
    effect  = "Allow"
    actions = ["ssm:SendCommand"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::document/AWS-RunShellScript",
      aws_instance.control_plane.arn,
    ]
  }

  # This Run Command API does not support resource-level IAM permissions. The
  # trust policy above and SendCommand statement still bind command creation to
  # this repository's main branch, the exact instance, and one AWS document.
  statement {
    sid       = "ReadSubmittedCommand"
    effect    = "Allow"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "github_actions_deploy" {
  count = var.github_actions_deploy_enabled ? 1 : 0

  name   = "${local.name}-exact-ssm-deploy"
  role   = aws_iam_role.github_actions_deploy[0].id
  policy = data.aws_iam_policy_document.github_actions_deploy[0].json
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

  # Managed LoRAs are immutable, content-addressed objects. The signed worker
  # manifest still pins the exact key, VersionId, byte size, and SHA-256; this
  # prefix grant removes the need for an OpenTofu/IAM rollout per LoRA.
  statement {
    sid       = "ReadVersionPinnedManagedLoras"
    actions   = ["s3:GetObjectVersion"]
    resources = ["${aws_s3_bucket.models.arn}/worker/managed-loras/sha256/*"]
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

  # HeadObject needs ListBucket to distinguish a missing object from an
  # unauthorized one. Keep that visibility inside the managed LoRA namespaces.
  statement {
    sid       = "ManagedLoraObjectListing"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.models.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "onboarding/loras/*",
        "worker/managed-loras/sha256/*",
      ]
    }
  }

  statement {
    sid = "ManagedLoraObjects"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:DeleteObjectVersion",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = [
      "${aws_s3_bucket.models.arn}/onboarding/loras/*",
      "${aws_s3_bucket.models.arn}/worker/managed-loras/sha256/*",
    ]
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
      sid = "XOAuthSecretAccess"
      actions = concat(
        [
          "secretsmanager:DescribeSecret",
          "secretsmanager:GetSecretValue",
        ],
        var.x_oauth_auth_mode == "oauth2" ? [
          "secretsmanager:PutSecretValue",
          "secretsmanager:UpdateSecretVersionStage",
        ] : []
      )
      resources = [statement.value]
    }
  }


  dynamic "statement" {
    for_each = var.civitai_api_secret_arn == null ? [] : [var.civitai_api_secret_arn]

    content {
      sid = "CivitaiApiSecretRead"
      actions = [
        "secretsmanager:DescribeSecret",
        "secretsmanager:GetSecretValue",
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

data "aws_iam_policy_document" "runpod_inference_key_read" {
  statement {
    sid     = "ReadExactRuntimeParameters"
    actions = ["ssm:GetParameter"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/runpod/inference-api-key",
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/a14b/ghcr-pull-once",
    ]
  }

  # AmazonSSMManagedInstanceCore currently grants broad Parameter Store reads.
  # This explicit boundary leaves the SSM agent's unrelated management APIs
  # intact while denying runtime reads of every parameter except these two.
  statement {
    sid    = "DenyOtherRuntimeParameterReads"
    effect = "Deny"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameterHistory",
      "ssm:GetParameters",
      "ssm:GetParametersByPath",
    ]
    not_resources = [
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/runpod/inference-api-key",
      "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/a14b/ghcr-pull-once",
    ]
  }
}

resource "aws_iam_role_policy" "runpod_inference_key_read" {
  name   = "${local.name}-runpod-inference-key-read"
  role   = aws_iam_role.control_plane.id
  policy = data.aws_iam_policy_document.runpod_inference_key_read.json
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
