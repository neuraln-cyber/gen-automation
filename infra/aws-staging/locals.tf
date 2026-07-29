locals {
  availability_zones = slice(sort(data.aws_availability_zones.available.names), 0, 2)
  name               = var.name_prefix
  dns_enabled        = var.route53_zone_id != null && var.hostname != null

  asset_bucket_name = lower(
    "${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-assets"
  )
  model_bucket_name = lower(
    "${var.name_prefix}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-models"
  )

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
