import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws-staging"


def _text(name: str) -> str:
    return (INFRA / name).read_text(encoding="utf-8")


def test_github_deploy_identity_is_optional_keyless_and_main_only() -> None:
    variables = _text("variables.tf")
    locals_tf = _text("locals.tf")
    iam = _text("iam.tf")

    enabled = variables.split('variable "github_actions_deploy_enabled" {', maxsplit=1)[1].split(
        'variable "github_actions_repository" {', maxsplit=1
    )[0]
    repository = variables.split('variable "github_actions_repository" {', maxsplit=1)[1].split(
        'variable "github_actions_repository_owner_id" {', maxsplit=1
    )[0]
    owner_id = variables.split('variable "github_actions_repository_owner_id" {', maxsplit=1)[
        1
    ].split('variable "github_actions_repository_id" {', maxsplit=1)[0]
    repository_id = variables.split('variable "github_actions_repository_id" {', maxsplit=1)[
        1
    ].split('variable "github_actions_oidc_provider_arn" {', maxsplit=1)[0]
    assume = iam.split(
        'data "aws_iam_policy_document" "github_actions_deploy_assume" {',
        maxsplit=1,
    )[1].split('resource "aws_iam_role" "github_actions_deploy" {', maxsplit=1)[0]

    assert re.search(r"default\s*=\s*false", enabled)
    assert 'default     = "neuraln-cyber/gen-automation"' in repository
    assert "without wildcards" in repository
    assert re.search(r"default\s*=\s*310034173", owner_id)
    assert "must be a positive integer" in owner_id
    assert re.search(r"default\s*=\s*1314605368", repository_id)
    assert "must be a positive integer" in repository_id
    assert (
        """github_actions_deploy_subject = format(
    "repo:%s@%d/%s@%d:ref:refs/heads/main",
    local.github_actions_repository_parts[0],
    var.github_actions_repository_owner_id,
    local.github_actions_repository_parts[1],
    var.github_actions_repository_id,
  )"""
        in locals_tf
    )
    assert 'actions = ["sts:AssumeRoleWithWebIdentity"]' in assume
    assert 'variable = "token.actions.githubusercontent.com:aud"' in assume
    assert 'values   = ["sts.amazonaws.com"]' in assume
    assert 'variable = "token.actions.githubusercontent.com:sub"' in assume
    assert "local.github_actions_deploy_subject" in assume
    assert 'variable = "token.actions.githubusercontent.com:repository"' in assume
    assert "values   = [var.github_actions_repository]" in assume
    assert 'variable = "token.actions.githubusercontent.com:repository_owner_id"' in assume
    assert "values   = [tostring(var.github_actions_repository_owner_id)]" in assume
    assert 'variable = "token.actions.githubusercontent.com:repository_id"' in assume
    assert "values   = [tostring(var.github_actions_repository_id)]" in assume
    assert 'variable = "token.actions.githubusercontent.com:workflow"' in assume
    assert "values   = [local.github_actions_deploy_workflow]" in assume
    assert 'variable = "token.actions.githubusercontent.com:ref"' in assume
    assert 'values   = ["refs/heads/main"]' in assume
    assert 'github_actions_deploy_workflow = "Deploy staging control plane"' in locals_tf
    assert "StringLike" not in assume
    assert "pull_request" not in assume
    assert "aws_iam_access_key" not in iam


def test_github_oidc_provider_can_be_managed_or_safely_reused() -> None:
    variables = _text("variables.tf")
    locals_tf = _text("locals.tf")
    iam = _text("iam.tf")

    assert 'resource "aws_iam_openid_connect_provider" "github_actions"' in iam
    assert 'data "aws_iam_openid_connect_provider" "github_actions_existing"' in iam
    assert 'url            = "https://token.actions.githubusercontent.com"' in iam
    assert 'client_id_list = ["sts.amazonaws.com"]' in iam
    assert "github_actions_manages_oidc_provider" in locals_tf
    assert "github_actions_uses_existing_oidc_provider" in locals_tf
    assert 'variable "github_actions_oidc_provider_arn"' in variables
    assert r"oidc-provider/token\\.actions\\.githubusercontent\\.com" in variables
    assert 'check "github_actions_oidc_provider_account"' in locals_tf
    assert 'check "github_actions_existing_oidc_provider_configuration"' in locals_tf
    assert "data.aws_iam_openid_connect_provider.github_actions_existing" in locals_tf
    assert '"sts.amazonaws.com"' in locals_tf


def test_github_deploy_role_has_only_exact_instance_run_command_access() -> None:
    iam = _text("iam.tf")
    policy = iam.split('data "aws_iam_policy_document" "github_actions_deploy" {', maxsplit=1)[
        1
    ].split('resource "aws_iam_role_policy" "github_actions_deploy" {', maxsplit=1)[0]
    send = policy.split('"SendExactStagingDeployCommand"', maxsplit=1)[1].split(
        '"ReadSubmittedCommand"', maxsplit=1
    )[0]
    follow_up = policy.split('"ReadSubmittedCommand"', maxsplit=1)[1]

    assert 'actions = ["ssm:SendCommand"]' in send
    assert (
        "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}::"
        "document/AWS-RunShellScript"
    ) in send
    assert "aws_instance.control_plane.arn" in send
    assert 'resources = ["*"]' not in send
    assert '"ssm:GetCommandInvocation"' in follow_up
    assert "ssm:CancelCommand" not in follow_up
    assert 'resources = ["*"]' in follow_up
    assert "ssm:ListCommands" not in policy
    assert "ssm:StartSession" not in policy
    assert "secretsmanager:" not in policy
    assert "s3:" not in policy
    assert "iam:PassRole" not in policy
    assert "sts:AssumeRole" not in policy
    assert "max_session_duration = 43200" in iam


def test_github_deploy_outputs_examples_and_runbook_are_explicit() -> None:
    outputs = _text("outputs.tf")
    tfvars = _text("terraform.tfvars.example")
    readme = _text("README.md")
    runbook = (ROOT / "docs" / "aws-staging-runbook.md").read_text(encoding="utf-8")

    assert 'output "github_actions_deploy_role_arn"' in outputs
    assert 'output "github_actions_oidc_provider_arn"' in outputs
    assert re.search(r"github_actions_deploy_enabled\s*=\s*true", tfvars)
    assert re.search(r'github_actions_repository\s*=\s*"neuraln-cyber/gen-automation"', tfvars)
    assert re.search(r"github_actions_repository_owner_id\s*=\s*310034173", tfvars)
    assert re.search(r"github_actions_repository_id\s*=\s*1314605368", tfvars)
    assert re.search(r"github_actions_oidc_provider_arn\s*=\s*null", tfvars)
    assert "never create AWS access keys for GitHub" in readme
    assert ("repo:neuraln-cyber@310034173/gen-automation@1314605368:ref:refs/heads/main") in runbook
    assert "the independent exact name claim `neuraln-cyber/gen-automation`" in runbook
    assert "repo:neuraln-cyber/gen-automation:ref:refs/heads/main" not in runbook
    assert "without an AWS browser login or stored AWS keys" in runbook
    assert "Infrastructure plans and\napplies remain" in runbook
