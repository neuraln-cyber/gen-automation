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
  salad_worker_artifact_role_enabled = (
    length(var.salad_worker_artifact_object_versions) > 0
    || length(var.salad_worker_artifact_extension_object_versions) > 0
  )
  # IAM permits at most 10,240 aggregate inline-policy characters on a role.
  # This dedicated reader role has exactly one inline policy, whose rendered
  # minified document is checked against this limit before any write.
  salad_worker_artifact_inline_policy_max_characters = 10240
  salad_worker_artifact_sorted_keys = sort(
    keys(var.salad_worker_artifact_object_versions)
  )
  salad_worker_artifact_extension_policy_enabled = (
    length(var.salad_worker_artifact_extension_object_versions) > 0
  )
  salad_worker_artifact_extension_policy_max_characters = 6144
  salad_worker_artifact_extension_sorted_keys = sort(
    keys(var.salad_worker_artifact_extension_object_versions)
  )
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
