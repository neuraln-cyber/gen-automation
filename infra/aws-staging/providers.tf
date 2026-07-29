provider "aws" {
  region = var.aws_region

  default_tags {
    tags = merge(
      {
        Application = "gen-automation"
        Environment = "staging"
        ManagedBy   = "opentofu"
      },
      var.tags,
    )
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_partition" "current" {}

data "aws_region" "current" {}

data "aws_ssm_parameter" "amazon_linux_2023_x86_64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}
