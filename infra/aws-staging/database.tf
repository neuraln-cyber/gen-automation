resource "aws_db_subnet_group" "staging" {
  name = "${local.name}-postgresql"
  subnet_ids = [
    aws_subnet.private_a.id,
    aws_subnet.private_b.id,
  ]

  tags = {
    Name = "${local.name}-postgresql"
  }
}

resource "aws_db_parameter_group" "postgresql" {
  name   = "${local.name}-postgres17"
  family = "postgres17"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  tags = {
    Name = "${local.name}-postgres17"
  }
}

resource "aws_db_instance" "postgresql" {
  identifier = "${local.name}-postgresql"

  engine         = "postgres"
  engine_version = var.postgres_engine_version
  instance_class = var.db_instance_class

  db_name  = var.db_name
  username = var.db_master_username

  manage_master_user_password = true

  allocated_storage     = var.db_allocated_storage_gib
  max_allocated_storage = 40
  storage_type          = "gp3"
  storage_encrypted     = true

  availability_zone      = local.availability_zones[0]
  multi_az               = false
  publicly_accessible    = false
  db_subnet_group_name   = aws_db_subnet_group.staging.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.postgresql.name
  port                   = 5432

  backup_retention_period  = 7
  backup_window            = "03:00-03:30"
  maintenance_window       = "sun:04:00-sun:04:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  auto_minor_version_upgrade      = true
  allow_major_version_upgrade     = false
  apply_immediately               = false
  deletion_protection             = var.db_deletion_protection
  skip_final_snapshot             = var.db_skip_final_snapshot
  final_snapshot_identifier       = var.db_skip_final_snapshot ? null : "${local.name}-postgresql-final"
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = false
  monitoring_interval             = 0

  tags = {
    Name               = "${local.name}-postgresql"
    DataClassification = "private-application-state"
  }
}
