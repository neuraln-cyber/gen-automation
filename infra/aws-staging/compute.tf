resource "aws_ebs_volume" "integration_profiles" {
  availability_zone = aws_subnet.public.availability_zone
  size              = var.integration_profile_volume_gib
  type              = "gp3"
  encrypted         = true

  tags = {
    Name               = "${local.name}-integration-profiles"
    DataClassification = "credential-bearing-integration-profiles"
  }
}

resource "aws_instance" "control_plane" {
  ami           = data.aws_ssm_parameter.amazon_linux_2023_x86_64.value
  instance_type = var.instance_type
  subnet_id     = aws_subnet.public.id

  associate_public_ip_address = true
  vpc_security_group_ids      = [aws_security_group.control_plane.id]
  iam_instance_profile        = aws_iam_instance_profile.control_plane.name

  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    aws_region       = var.aws_region
    log_group_name   = aws_cloudwatch_log_group.staging.name
    metric_namespace = local.metric_namespace
    integration_volume_id_compact = replace(
      aws_ebs_volume.integration_profiles.id,
      "-",
      "",
    )
  })
  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "disabled"
    http_put_response_hop_limit = 1
    http_tokens                 = "required"
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_gib
    encrypted             = true
    delete_on_termination = true
  }

  maintenance_options {
    auto_recovery = "default"
  }

  tags = {
    Name = "${local.name}-control-plane"
  }

  depends_on = [
    aws_iam_role_policy.runtime,
    aws_iam_role_policy.cloudwatch,
    aws_iam_role_policy_attachment.ssm,
    aws_route_table_association.public,
  ]
}

resource "aws_volume_attachment" "integration_profiles" {
  device_name = "/dev/sdf"
  instance_id = aws_instance.control_plane.id
  volume_id   = aws_ebs_volume.integration_profiles.id

  stop_instance_before_detaching = true
}

resource "aws_eip" "control_plane" {
  domain = "vpc"

  tags = {
    Name = "${local.name}-control-plane"
  }
}

resource "aws_eip_association" "control_plane" {
  allocation_id = aws_eip.control_plane.id
  instance_id   = aws_instance.control_plane.id
}
