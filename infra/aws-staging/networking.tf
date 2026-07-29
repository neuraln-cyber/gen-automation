resource "aws_vpc" "staging" {
  cidr_block           = var.vpc_cidr
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${local.name}-vpc"
  }
}

resource "aws_internet_gateway" "staging" {
  vpc_id = aws_vpc.staging.id

  tags = {
    Name = "${local.name}-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.staging.id
  availability_zone       = local.availability_zones[0]
  cidr_block              = var.public_subnet_cidr
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name}-public-a"
    Tier = "public"
  }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.staging.id
  availability_zone       = local.availability_zones[0]
  cidr_block              = var.private_subnet_a_cidr
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name}-private-a"
    Tier = "private"
  }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.staging.id
  availability_zone       = local.availability_zones[1]
  cidr_block              = var.private_subnet_b_cidr
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name}-private-b"
    Tier = "private"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.staging.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.staging.id
  }

  tags = {
    Name = "${local.name}-public"
  }
}

resource "aws_route_table_association" "public" {
  route_table_id = aws_route_table.public.id
  subnet_id      = aws_subnet.public.id
}

resource "aws_security_group" "control_plane" {
  name        = "${local.name}-control-plane"
  description = "Public HTTP and HTTPS only; administration is through SSM."
  vpc_id      = aws_vpc.staging.id

  tags = {
    Name = "${local.name}-control-plane"
  }
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  for_each = var.http_ingress_cidrs

  security_group_id = aws_security_group.control_plane.id
  description       = "Public HTTP for Caddy certificate issuance and redirect"
  cidr_ipv4         = each.value
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  for_each = var.http_ingress_cidrs

  security_group_id = aws_security_group.control_plane.id
  description       = "Public HTTPS ingress"
  cidr_ipv4         = each.value
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "control_plane" {
  security_group_id = aws_security_group.control_plane.id
  description       = "Outbound package, AWS API, provider API, and publication traffic"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "database" {
  name        = "${local.name}-database"
  description = "Private PostgreSQL reachable only from the control-plane security group."
  vpc_id      = aws_vpc.staging.id

  tags = {
    Name = "${local.name}-database"
  }
}

resource "aws_vpc_security_group_ingress_rule" "postgresql" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the control-plane host only"
  referenced_security_group_id = aws_security_group.control_plane.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}
