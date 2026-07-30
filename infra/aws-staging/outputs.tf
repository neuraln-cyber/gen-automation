output "control_plane_instance_id" {
  description = "EC2 instance ID used for Systems Manager sessions."
  value       = aws_instance.control_plane.id
}

output "control_plane_elastic_ip" {
  description = "Stable public IPv4 address for HTTP/HTTPS ingress."
  value       = aws_eip.control_plane.public_ip
}

output "ssm_start_session_command" {
  description = "Credential-free command shape; AWS CLI resolves the operator's ambient identity."
  value       = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.control_plane.id}"
}

output "public_base_url" {
  description = "HTTPS application origin when Route53 is enabled."
  value       = local.dns_enabled ? "https://${var.hostname}" : null
}

output "asset_bucket_name" {
  description = "Private, versioned application asset bucket."
  value       = aws_s3_bucket.assets.id
}

output "model_bucket_name" {
  description = "Private, versioned model-artifact bucket."
  value       = aws_s3_bucket.models.id
}

output "salad_worker_artifact_role_arn" {
  description = "Optional one-hour STS reader role for exact model-artifact versions."
  value       = try(aws_iam_role.salad_worker_artifact_reader[0].arn, null)
}

output "database_endpoint" {
  description = "Private PostgreSQL endpoint; it is reachable only from the control-plane SG."
  value       = aws_db_instance.postgresql.address
}

output "database_port" {
  description = "Private PostgreSQL port."
  value       = aws_db_instance.postgresql.port
}

output "database_name" {
  description = "Initial PostgreSQL database name."
  value       = aws_db_instance.postgresql.db_name
}

output "rds_master_secret_arn" {
  description = "RDS-managed bootstrap secret ARN. Secret content is not present in IaC."
  value       = aws_db_instance.postgresql.master_user_secret[0].secret_arn
}

output "integration_profiles_volume_id" {
  description = "Encrypted EBS volume holding private MEGA and Patreon integration state."
  value       = aws_ebs_volume.integration_profiles.id
}

output "mega_profile_host_path" {
  description = "Private MEGAcmd profile directory to bind into the MEGA uploader."
  value       = "/var/lib/gen-automation/integration-profiles/mega"
}

output "patreon_browser_profiles_host_path" {
  description = "Private Chromium profile directory to bind to /profiles in the browser sidecar."
  value       = "/var/lib/gen-automation/integration-profiles/patreon-browser/profiles"
}

output "patreon_browser_state_host_path" {
  description = "Durable idempotency SQLite directory to bind to /state in the browser sidecar."
  value       = "/var/lib/gen-automation/integration-profiles/patreon-browser/state"
}

output "control_plane_role_arn" {
  description = "Ambient AWS workload identity for the control-plane host."
  value       = aws_iam_role.control_plane.arn
}

output "cloudwatch_log_group_name" {
  description = "Bootstrap and runtime CloudWatch log group."
  value       = aws_cloudwatch_log_group.staging.name
}

output "alert_topic_arn" {
  description = "SNS topic used by infrastructure alarms and the monthly budget."
  value       = aws_sns_topic.alerts.arn
}
