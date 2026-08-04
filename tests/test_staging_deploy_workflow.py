import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-staging.yml"
DEPLOY = ROOT / "infra" / "aws-staging" / "deploy"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _updater() -> str:
    return (DEPLOY / "update-control-plane.sh").read_text(encoding="utf-8")


def _semantic_activator() -> str:
    return (DEPLOY / "activate-semantic-gateway.sh").read_text(encoding="utf-8")


def test_staging_rollout_follows_only_successful_immutable_publication() -> None:
    workflow = _workflow()

    assert 'workflows: ["Publish immutable images"]' in workflow
    assert "workflow_dispatch:" not in workflow
    assert "github.event.workflow_run.conclusion == 'success'" in workflow
    assert "github.event.workflow_run.head_branch == 'main'" in workflow
    assert "github.event.workflow_run.head_sha" not in workflow
    assert "PUBLISH_RUN_ID: ${{ github.event.workflow_run.id }}" in workflow
    assert "gh run download" in workflow
    assert "staging-deploy-source" in workflow
    assert "actions: read" in workflow
    assert "nothing will deploy" in workflow
    assert 'printf \'%s\\n\' "available=false" >>"$GITHUB_OUTPUT"' in workflow
    assert "if: steps.publication_metadata.outputs.available == 'true'" in workflow
    assert workflow.count("git/ref/heads/main") == 2
    assert "this stale publication will not deploy" in workflow
    assert "this rollout is now a no-op" in workflow
    assert 'printf \'%s\\n\' "deploy=false" >>"$GITHUB_OUTPUT"' in workflow
    assert workflow.count("if: steps.main_gate.outputs.deploy == 'true'") == 4
    assert "control-plane-mega@sha256:[0-9a-f]{64}" in workflow
    assert "org.opencontainers.image.revision" in workflow


def test_staging_rollout_uses_short_lived_oidc_and_ssm_without_user_keys() -> None:
    workflow = _workflow()

    assert "id-token: write" in workflow
    assert "contents: read" in workflow
    assert "packages: read" in workflow
    assert "packages: write" not in workflow
    assert (
        "aws-actions/configure-aws-credentials@acca2b1b2070338fb9fd1ca27ecee81d687e58e5"
    ) in workflow
    assert "role-to-assume: ${{ vars.AWS_STAGING_DEPLOY_ROLE_ARN }}" in workflow
    assert 'allowed-account-ids: "861912887470"' in workflow
    assert "role-duration-seconds: 3600" in workflow
    assert "AWS_STAGING_INSTANCE_ID: ${{ vars.AWS_STAGING_INSTANCE_ID }}" in workflow
    assert "aws ssm send-command" in workflow
    assert "AWS-RunShellScript" in workflow
    assert "aws ssm get-command-invocation" in workflow
    assert "AWS_ACCESS_KEY_ID" not in workflow
    assert "AWS_SECRET_ACCESS_KEY" not in workflow
    assert "secrets.AWS" not in workflow
    assert "secrets.SALAD" not in workflow


def test_publication_exports_its_exact_ci_source_for_nested_workflow_run() -> None:
    publication = (ROOT / ".github" / "workflows" / "publish-images.yml").read_text(
        encoding="utf-8"
    )

    assert "record-staging-source:" in publication
    assert "needs: publish" in publication
    assert "SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}" in publication
    assert "printf '%s\\n' \"$SOURCE_SHA\"" in publication
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in publication
    assert "name: staging-deploy-source" in publication
    assert "retention-days: 1" in publication


def test_host_updater_is_locked_atomic_validated_and_rolls_back() -> None:
    updater = _updater()
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "flock --exclusive --wait" in updater
    assert "ghcr.io/neuraln-cyber/gen-automation/control-plane-mega" in updater
    assert re.search(r"source_revision.*\{40\}", updater)
    assert "org.opencontainers.image.revision" in updater
    assert 'architecture" = "amd64' in updater
    assert 'operating_system" = "linux' in updater
    assert 'mktemp "$config_root/.deploy.env.update.' in updater
    assert 'mv -- "$temporary_env" "$deploy_env"' in updater
    assert "gen-automation-validate-deployment" in updater
    assert "config --quiet" in updater
    assert "wait_for_control_plane" in updater
    assert "restore_previous_deployment" in updater
    assert 'mv -- "$backup_env" "$deploy_env"' in updater
    assert "timeout --signal=TERM --kill-after=30s 600s" in updater
    assert 'systemctl restart --no-block "$service_name"' in updater
    assert "run database" not in updater.casefold()
    assert "gen-automation-update-control-plane" in installer
    assert '"$source_dir/update-control-plane.sh"' in installer


def test_ssm_command_contains_only_public_immutable_coordinates() -> None:
    workflow = _workflow()

    command_block = workflow.split("command = (", maxsplit=1)[1].split(
        "print(json.dumps", maxsplit=1
    )[0]
    assert "gen-automation-update-control-plane" in command_block
    assert "--image" in command_block
    assert "--revision" in command_block
    for prohibited in (
        "TOKEN",
        "PASSWORD",
        "SECRET",
        "SALAD",
        "PATREON",
        "MEGA",
        "DATABASE",
    ):
        assert prohibited not in command_block.upper()


def test_semantic_gateway_activation_is_pinned_bounded_atomic_and_reversible() -> None:
    activator = _semantic_activator()
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "raw.githubusercontent.com/neuraln-cyber/gen-automation/$source_revision" in activator
    assert 'allowed_repository="ghcr.io/neuraln-cyber/gen-automation/semantic-gateway"' in activator
    assert "semantic-gateway@sha256:[0-9a-f]{64}" in activator
    assert "org.opencontainers.image.revision" in activator
    assert 'architecture" = "amd64' in activator
    assert 'operating_system" = "linux' in activator
    assert "aws ssm get-parameter" in activator
    assert "--with-decryption" in activator
    assert '>"$key_output" 2>/dev/null' in activator
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false" in activator
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE=shadow" in activator
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE=0" in activator
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST=[]" in activator
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_SEVERE_CONFIDENCE_MICROS=900000" in activator
    for seconds in (600, 630, 660, 720):
        assert str(seconds) in activator
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS=5" in activator
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_BASE_SECONDS=30" in activator
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_MAX_SECONDS=120" in activator
    assert "create_backup" in activator
    assert "restore_previous_deployment" in activator
    assert 'rm -f -- "${paths[$index]}"' in activator
    assert activator.count('systemctl restart --no-block "$service_name"') == 2
    assert "config --quiet" in activator
    assert "http://127.0.0.1:8091/health/ready" in activator
    assert "http://127.0.0.1:8000/api/v1/health/ready" in activator
    assert "managed_gateway_owns_loopback_port" in activator
    assert activator.count("assert_loopback_gateway_port_available_or_managed") == 3
    assert "ps --status running --quiet semantic-gateway" in activator
    assert 'docker port "$container_id" 8080/tcp' in activator
    assert '[ "$published_port" = "127.0.0.1:8091" ]' in activator
    assert "used outside the managed semantic gateway" in activator
    assert "loopback gateway port 8091 is already in use" not in activator
    assert "gen-automation-activate-semantic-gateway" in installer
