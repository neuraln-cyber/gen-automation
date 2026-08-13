import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws-staging"
I2V_REVIEWED_MANIFEST_KEY = (
    "worker/i2v/manifests/sha256/"
    "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e.json"
)
I2V_REVIEWED_MANIFEST_VERSION = "u4bSnCPzDJ4zctrA2Nr66ji0Zh2qPpXX"


def _terraform_source() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(INFRA.glob("*.tf")))


def test_aws_staging_topology_and_cost_defaults_are_explicit() -> None:
    source = _terraform_source()

    assert 'default     = "eu-central-1"' in source
    assert 'version = "~> 6.55.0"' in source
    assert 'default     = "t3a.medium"' in source
    assert 'default     = "db.t4g.micro"' in source
    assert 'default     = "17.10"' in source
    assert re.search(r"manage_master_user_password\s*=\s*true", source)
    assert re.search(r"backup_retention_period\s*=\s*7", source)
    assert re.search(r"publicly_accessible\s*=\s*false", source)
    assert 'resource "aws_subnet" "private_a"' in source
    assert 'resource "aws_subnet" "private_b"' in source
    assert 'resource "aws_eip" "control_plane"' in source
    assert 'resource "aws_ebs_volume" "integration_profiles"' in source
    assert source.count('resource "aws_s3_bucket"') == 2
    assert source.count('resource "aws_s3_bucket_versioning"') == 2
    assert source.count('resource "aws_s3_bucket_public_access_block"') == 2
    assert source.count('resource "aws_s3_bucket_server_side_encryption_configuration"') == 2


def test_asset_lifecycle_expires_only_abandoned_staging_uploads() -> None:
    storage = (INFRA / "storage.tf").read_text(encoding="utf-8")
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")

    assert 'variable "abandoned_staging_retention_days"' in variables
    assert 'id     = "expire-abandoned-staging-uploads"' in storage
    assert 'prefix = "staging/"' in storage
    assert "days = var.abandoned_staging_retention_days" in storage
    assert "noncurrent_days = var.abandoned_staging_retention_days" in storage
    for durable_prefix in (
        'prefix = "masters/"',
        'prefix = "derivatives/"',
        'prefix = "finished-set-archives/"',
        'prefix = "publication-packages/"',
    ):
        assert durable_prefix not in storage


def test_budget_notifies_before_and_at_the_limit() -> None:
    monitoring = (INFRA / "monitoring.tf").read_text(encoding="utf-8")

    for threshold in (50, 80, 100):
        assert re.search(
            rf"threshold\s*=\s*{threshold}.*?notification_type\s*=\s*\"ACTUAL\"",
            monitoring,
            re.DOTALL,
        )
    assert re.search(
        r'threshold\s*=\s*100.*?notification_type\s*=\s*"FORECASTED"',
        monitoring,
        re.DOTALL,
    )


def test_aws_staging_has_no_ssh_or_secret_value_resources() -> None:
    source = _terraform_source()
    cloud_init = (INFRA / "cloud-init.yaml.tftpl").read_text(encoding="utf-8")

    assert re.search(r"from_port\s*=\s*80", source)
    assert re.search(r"from_port\s*=\s*443", source)
    assert re.search(r"from_port\s*=\s*22", source) is None
    assert "key_name" not in source
    assert "AmazonSSMManagedInstanceCore" in source
    assert re.search(r'http_tokens\s*=\s*"required"', source)
    assert re.search(r"http_put_response_hop_limit\s*=\s*1", source)
    assert re.search(r"http_put_response_hop_limit\s*=\s*2", source) is None
    assert "aws_secretsmanager_secret_version" not in source
    assert "secret_string" not in source
    assert "GEN_AUTOMATION_" not in cloud_init
    assert "password" not in cloud_init.casefold()
    assert "private_key" not in cloud_init.casefold()
    assert "secret_access_key" not in cloud_init.casefold()
    assert "gen-automation-deploy.target" in cloud_init
    assert 'mount_path="/var/lib/gen-automation/integration-profiles"' in cloud_init
    assert 'mkfs.xfs -L genauto-int "$device"' in cloud_init
    assert 'install -d -o 10001 -g 10001 -m 0700 "$mount_path/mega"' in cloud_init
    assert '"$mount_path/patreon-browser/profiles"' in cloud_init
    assert '"$mount_path/patreon-browser/state"' in cloud_init
    assert "install -d -o 10001 -g 10001 -m 0700" in cloud_init


def test_aws_staging_examples_and_state_safety_are_non_secret() -> None:
    backend = (INFRA / "backend.s3.tfbackend.example").read_text(encoding="utf-8")
    tfvars = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "aws-staging-runbook.md").read_text(encoding="utf-8")

    assert "use_lockfile = true" in backend
    assert "encrypt      = true" in backend
    assert re.search(r"(?m)^\s*access_key\s*=", backend) is None
    assert re.search(r"(?m)^\s*secret_key\s*=", backend) is None
    assert "refresh_token" not in tfvars
    assert "client_secret" not in tfvars
    assert "x_oauth_secret_arn = null" in tfvars
    assert 'x_oauth_auth_mode  = "oauth2"' in tfvars
    assert "budget_enabled" in tfvars
    assert "count = var.budget_enabled ? 1 : 0" in _terraform_source()
    assert "infra/aws-staging/backend.s3.tfbackend" in gitignore
    assert "infra/aws-staging/terraform.tfvars" in gitignore
    assert "*.tfstate" in gitignore
    assert "*.tfplan" in gitignore
    assert "tofu plan -out=staging.tfplan" in runbook
    assert "Do not apply until" in runbook
    assert "control-plane container with\nhost networking" in runbook
    assert "application listener only to `127.0.0.1`" in runbook
    assert "Caddy alone owns host ports\n80/443" in runbook
    assert (
        "Patreon browser and any separate MEGA uploader sidecars on a private\nDocker bridge"
        in runbook
    )
    assert "never use host networking for them" in runbook
    assert "prevents a bridged sidecar from obtaining the EC2 instance role" in runbook


def test_x_oauth1_uses_the_exact_secret_arn_with_read_only_runtime_iam() -> None:
    iam = (INFRA / "iam.tf").read_text(encoding="utf-8")
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    tfvars = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    controller = (INFRA / "deploy" / "control-plane.env.example").read_text(encoding="utf-8")
    statement = iam.split(
        "for_each = var.x_oauth_secret_arn == null ? [] : [var.x_oauth_secret_arn]",
        maxsplit=1,
    )[1].split("\n  }", maxsplit=1)[0]
    secret_variable = variables.split('variable "x_oauth_secret_arn"', maxsplit=1)[1].split(
        'variable "x_oauth_auth_mode"', maxsplit=1
    )[0]
    oauth2_regex = (
        "^arn:aws:secretsmanager:eu-central-1:861912887470:secret:"
        "gen-automation-staging/x/oauth-[A-Za-z0-9]{6}$"
    )
    oauth1_regex = (
        "^arn:aws:secretsmanager:eu-central-1:861912887470:secret:"
        "gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}$"
    )

    assert 'variable "x_oauth_auth_mode"' in variables
    assert re.search(
        r'variable "x_oauth_auth_mode"\s*\{.*?default\s*=\s*"oauth2"',
        variables,
        re.DOTALL,
    )
    assert 'contains(["oauth1", "oauth2"], var.x_oauth_auth_mode)' in variables
    assert f'"{oauth2_regex}"' in secret_variable
    assert f'"{oauth1_regex}"' in secret_variable
    assert re.search(
        rf'var\.x_oauth_auth_mode == "oauth2".*?"{re.escape(oauth2_regex)}"',
        secret_variable,
        re.DOTALL,
    )
    assert re.search(
        rf'var\.x_oauth_auth_mode == "oauth1".*?"{re.escape(oauth1_regex)}"',
        secret_variable,
        re.DOTALL,
    )
    assert "arn:[^:]+" not in secret_variable
    assert "secret:.+" not in secret_variable
    assert 'x_oauth_auth_mode  = "oauth2"' in tfvars
    assert "GEN_AUTOMATION_X_AUTH_MODE=oauth2" in controller
    assert 'sid = "XOAuthSecretAccess"' in statement
    assert '"secretsmanager:DescribeSecret"' in statement
    assert '"secretsmanager:GetSecretValue"' in statement
    assert 'var.x_oauth_auth_mode == "oauth2" ? [' in statement
    assert '"secretsmanager:PutSecretValue"' in statement
    assert '"secretsmanager:UpdateSecretVersionStage"' in statement
    assert "resources = [statement.value]" in statement
    assert "*" not in statement

    valid_oauth2 = (
        "arn:aws:secretsmanager:eu-central-1:861912887470:secret:"
        "gen-automation-staging/x/oauth-AbCd12"
    )
    valid_oauth1 = (
        "arn:aws:secretsmanager:eu-central-1:861912887470:secret:"
        "gen-automation-staging/x/oauth1-AbCd12"
    )
    assert re.fullmatch(oauth2_regex, valid_oauth2)
    assert re.fullmatch(oauth1_regex, valid_oauth1)
    for invalid in (
        valid_oauth1,
        valid_oauth2.replace("861912887470", "123456789012"),
        valid_oauth2.replace("eu-central-1", "us-east-1"),
        valid_oauth2.replace("arn:aws:", "arn:aws-us-gov:"),
        valid_oauth2.replace("oauth-AbCd12", "oauth-*"),
        valid_oauth2.replace("oauth-AbCd12", "oauth-AbCd123"),
    ):
        assert re.fullmatch(oauth2_regex, invalid) is None
    for invalid in (
        valid_oauth2,
        valid_oauth1.replace("861912887470", "123456789012"),
        valid_oauth1.replace("eu-central-1", "us-east-1"),
        valid_oauth1.replace("oauth1-AbCd12", "oauth1-*"),
        valid_oauth1.replace("oauth1-AbCd12", "oauth1-AbCd123"),
    ):
        assert re.fullmatch(oauth1_regex, invalid) is None


def test_salad_artifact_reader_is_disabled_and_exact_version_only() -> None:
    source = _terraform_source()
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    tfvars = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    reader_policy = source.split(
        'data "aws_iam_policy_document" "salad_worker_artifact_reader" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "salad_worker_artifact_reader" {',
        maxsplit=1,
    )[0]
    control_policy = source.split(
        'data "aws_iam_policy_document" "runtime" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "runtime" {',
        maxsplit=1,
    )[0]

    assert 'variable "salad_worker_artifact_object_versions"' in variables
    assert re.search(
        r'variable "salad_worker_artifact_object_versions"\s*\{.*?default\s*=\s*\{\}',
        variables,
        re.DOTALL,
    )
    assert "salad_worker_artifact_object_versions = {" in tfvars
    assert "length(var.salad_worker_artifact_object_versions) > 0" in source
    assert "max_session_duration = 43200" in source
    assert "identifiers = [aws_iam_role.control_plane.arn]" in source
    assert 'actions   = ["s3:GetObjectVersion"]' in reader_policy
    assert 'variable = "s3:VersionId"' in reader_policy
    assert "statement.value" in reader_policy
    assert "i2v/manifests/sha256/" in variables
    assert "ReadPinnedI2vManifest" in control_policy
    assert 'actions   = ["s3:GetObjectVersion"]' in control_policy
    assert 'variable = "s3:VersionId"' in control_policy
    assert re.search(r'"s3:(?:GetObject|ListBucket|PutObject|DeleteObject)"', reader_policy) is None
    assert 'sid       = "AssumeSaladArtifactReader"' in control_policy
    assert 'actions   = ["sts:AssumeRole"]' in control_policy
    assert "aws_iam_role.salad_worker_artifact_reader[0].arn" in control_policy
    assert '"${aws_s3_bucket.models.arn}/*"' not in control_policy


def test_reviewed_i2v_source_manifest_is_exactly_inventoried_for_host_read() -> None:
    iam = (INFRA / "iam.tf").read_text(encoding="utf-8")
    variables = (INFRA / "variables.tf").read_text(encoding="utf-8")
    tfvars = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    runtime_policy = iam.split(
        'data "aws_iam_policy_document" "runtime" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "runtime" {',
        maxsplit=1,
    )[0]
    manifest_statement = runtime_policy.split(
        'sid       = "ReadPinnedI2vManifest',
        maxsplit=1,
    )[1].split(
        'sid = "RdsManagedMasterSecretRead"',
        maxsplit=1,
    )[0]

    assert f'"{I2V_REVIEWED_MANIFEST_KEY}" = "{I2V_REVIEWED_MANIFEST_VERSION}"' in tfvars
    assert r"i2v/manifests/sha256/[0-9a-f]{64}\\.json" in variables
    assert 'if startswith(object_key, "worker/i2v/manifests/sha256/")' in runtime_policy
    assert 'actions   = ["s3:GetObjectVersion"]' in manifest_statement
    assert 'resources = ["${aws_s3_bucket.models.arn}/${statement.key}"]' in (manifest_statement)
    assert 'variable = "s3:VersionId"' in manifest_statement
    assert "values   = [statement.value]" in manifest_statement
    assert '"${aws_s3_bucket.models.arn}/worker/i2v/manifests/sha256/*"' not in (manifest_statement)
    for broader_action in (
        '"s3:GetObject"',
        '"s3:PutObject"',
        '"s3:DeleteObject"',
        '"s3:ListBucket"',
    ):
        assert broader_action not in manifest_statement


def test_runtime_can_list_only_managed_lora_model_prefixes() -> None:
    iam = (INFRA / "iam.tf").read_text(encoding="utf-8")
    runtime_policy = iam.split(
        'data "aws_iam_policy_document" "runtime" {',
        maxsplit=1,
    )[1].split(
        'resource "aws_iam_role_policy" "runtime" {',
        maxsplit=1,
    )[0]
    listing_statement = runtime_policy.split(
        'sid       = "ManagedLoraObjectListing"',
        maxsplit=1,
    )[1].split(
        'sid = "ManagedLoraObjects"',
        maxsplit=1,
    )[0]

    assert 'actions   = ["s3:ListBucket"]' in listing_statement
    assert "resources = [aws_s3_bucket.models.arn]" in listing_statement
    assert 'test     = "StringLike"' in listing_statement
    assert 'variable = "s3:prefix"' in listing_statement
    prefix_values = re.search(r"values\s*=\s*\[(.*?)\]", listing_statement, re.DOTALL)
    assert prefix_values is not None
    assert re.findall(r'"([^"]+)"', prefix_values.group(1)) == [
        "onboarding/loras/*",
        "worker/managed-loras/sha256/*",
        "worker/i2v/manifests/sha256/*",
        "worker/i2v/sha256/*",
    ]
    assert runtime_policy.count('"s3:ListBucket"') == 2
