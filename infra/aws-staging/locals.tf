locals {
  availability_zones = slice(sort(data.aws_availability_zones.available.names), 0, 2)
  name               = var.name_prefix
  dns_enabled        = var.route53_zone_id != null && var.hostname != null
  browser_upload_origin = (
    var.browser_upload_origin != null
    ? var.browser_upload_origin
    : (local.dns_enabled ? "https://${var.hostname}" : null)
  )

  asset_bucket_name = lower(
    "${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-assets"
  )
  model_bucket_name = lower(
    "${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-models"
  )
  salad_worker_artifact_role_enabled = length(var.salad_worker_artifact_object_versions) > 0
  # Customer-managed policies have a 6,144-character document quota. Keep each
  # exact key/VersionId grant in a deterministic, conservatively small shard;
  # the variable bounds below keep at most eight exact-reader policies plus the
  # separate managed-LoRA policy under IAM's ten-policy role attachment quota.
  salad_worker_artifact_policy_shard_size = 5
  salad_worker_artifact_sorted_keys = sort(
    keys(var.salad_worker_artifact_object_versions)
  )
  salad_worker_artifact_policy_shards = {
    for shard_index in range(ceil(
      length(local.salad_worker_artifact_sorted_keys)
      / local.salad_worker_artifact_policy_shard_size
    )) :
    format("%02d", shard_index) => {
      for object_key in slice(
        local.salad_worker_artifact_sorted_keys,
        shard_index * local.salad_worker_artifact_policy_shard_size,
        min(
          (shard_index + 1) * local.salad_worker_artifact_policy_shard_size,
          length(local.salad_worker_artifact_sorted_keys),
        ),
      ) :
      object_key => var.salad_worker_artifact_object_versions[object_key]
    }
  }
  github_actions_manages_oidc_provider = (
    var.github_actions_deploy_enabled && var.github_actions_oidc_provider_arn == null
  )
  github_actions_uses_existing_oidc_provider = (
    var.github_actions_deploy_enabled && var.github_actions_oidc_provider_arn != null
  )
  github_actions_repository_parts = split("/", var.github_actions_repository)
  github_actions_deploy_subject = format(
    "repo:%s@%d/%s@%d:ref:refs/heads/main",
    local.github_actions_repository_parts[0],
    var.github_actions_repository_owner_id,
    local.github_actions_repository_parts[1],
    var.github_actions_repository_id,
  )
  github_actions_deploy_workflow = "Deploy staging control plane"
  github_actions_oidc_provider_arn = var.github_actions_deploy_enabled ? (
    var.github_actions_oidc_provider_arn != null
    ? var.github_actions_oidc_provider_arn
    : try(aws_iam_openid_connect_provider.github_actions[0].arn, null)
  ) : null

  alert_topic_name = "${local.name}-alerts"
  log_group_name   = "/gen-automation/staging"
  metric_namespace = "GenAutomation/Staging"
}

check "two_availability_zones" {
  assert {
    condition     = length(data.aws_availability_zones.available.names) >= 2
    error_message = "The selected AWS Region must expose at least two availability zones."
  }
}

check "route53_inputs" {
  assert {
    condition = (
      (var.route53_zone_id == null && var.hostname == null)
      || (var.route53_zone_id != null && var.hostname != null)
    )
    error_message = "route53_zone_id and hostname must be supplied together or both omitted."
  }
}

check "github_actions_oidc_provider_account" {
  assert {
    condition = (
      var.github_actions_oidc_provider_arn == null
      || var.github_actions_oidc_provider_arn == "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
    )
    error_message = "github_actions_oidc_provider_arn must identify GitHub's OIDC provider in this AWS account."
  }
}

check "github_actions_existing_oidc_provider_configuration" {
  assert {
    condition = !local.github_actions_uses_existing_oidc_provider || (
      contains(
        [
          "token.actions.githubusercontent.com",
          "https://token.actions.githubusercontent.com",
        ],
        trimsuffix(try(data.aws_iam_openid_connect_provider.github_actions_existing[0].url, ""), "/"),
      )
      && contains(
        try(data.aws_iam_openid_connect_provider.github_actions_existing[0].client_id_list, []),
        "sts.amazonaws.com",
      )
    )
    error_message = "The existing GitHub OIDC provider must use token.actions.githubusercontent.com and allow the sts.amazonaws.com audience."
  }
}
