import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra" / "aws-staging"


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
    assert (
        '"s3:PutObject"'
        not in source.split('sid = "ModelObjects"', maxsplit=1)[1].split(
            "resources",
            maxsplit=1,
        )[0]
    )
    assert "GEN_AUTOMATION_" not in cloud_init
    assert "password" not in cloud_init.casefold()
    assert "private_key" not in cloud_init.casefold()
    assert "secret_access_key" not in cloud_init.casefold()
    assert "gen-automation-deploy.target" in cloud_init
    assert 'mount_path="/var/lib/gen-automation/integration-profiles"' in cloud_init
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
