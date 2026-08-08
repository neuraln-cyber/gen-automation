import base64
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-staging.yml"
DEPLOY = ROOT / "infra" / "aws-staging" / "deploy"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _updater() -> str:
    return (DEPLOY / "update-control-plane.sh").read_text(encoding="utf-8")


def _migration_validator() -> str:
    return (DEPLOY / "validate-migration-environment.sh").read_text(encoding="utf-8")


def _semantic_activator() -> str:
    return (DEPLOY / "activate-semantic-gateway.sh").read_text(encoding="utf-8")


def _semantic_promoter() -> str:
    return (DEPLOY / "promote-semantic-anatomy.sh").read_text(encoding="utf-8")


def _rollout_command_program() -> str:
    rollout = _workflow().split("      - name: Roll out through AWS Systems Manager\n", maxsplit=1)[
        1
    ]
    embedded = textwrap.dedent(
        rollout.split("python3 -c '", maxsplit=1)[1].split('\' >"$parameters_file"', maxsplit=1)[0]
    )
    command = shlex.split(f"python3 -c '{embedded}'", posix=True)
    assert command[:2] == ["python3", "-c"]
    return command[2]


def test_staging_rollout_follows_only_successful_immutable_publication() -> None:
    workflow = _workflow()

    assert "name: Deploy staging control plane" in workflow
    assert 'workflows: ["Publish immutable images"]' in workflow
    assert "workflow_dispatch:" in workflow
    assert "github.event_name == 'workflow_run'" in workflow
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
    assert "gpu-worker@sha256:[0-9a-f]{64}" in workflow
    assert "WORKER_IMAGE_REPOSITORY: ghcr.io/neuraln-cyber/gen-automation/gpu-worker" in workflow
    assert "staging-worker-source-revision" in workflow
    assert "staging-worker-image-digest" in workflow
    assert 'worker_image_ref="${WORKER_IMAGE_REPOSITORY}@${PUBLISHED_WORKER_IMAGE_DIGEST}"' in (
        workflow
    )
    assert '[ "$worker_image_digest" = "$PUBLISHED_WORKER_IMAGE_DIGEST" ]' in workflow
    assert '[ "$worker_image_revision" = "$WORKER_SOURCE_REVISION" ]' in workflow
    assert "WORKER_IMAGE_REF=%s@%s" in workflow
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
    assert "needs: [publish, publish-worker]" in publication
    assert "SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}" in publication
    assert "printf '%s\\n' \"$SOURCE_SHA\"" in publication
    assert "WORKER_SOURCE_REVISION: ${{ needs.publish-worker.outputs.source_revision }}" in (
        publication
    )
    assert "WORKER_IMAGE_DIGEST: ${{ needs.publish-worker.outputs.image_digest }}" in publication
    assert "staging-worker-source-revision" in publication
    assert "staging-worker-image-digest" in publication
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

    command_block = workflow.split("migration_preflight = (", maxsplit=1)[1].split(
        "print(json.dumps", maxsplit=1
    )[0]
    assert "gen-automation-update-control-plane" in command_block
    assert "--image" in command_block
    assert "--revision" in command_block
    assert "GEN_AUTOMATION_SALAD_WORKER_IMAGE" in workflow
    assert 'runtime_env_backup = f"{runtime_env}.{revision}.rollback"' in workflow
    assert "tempfile.mkstemp" in workflow
    assert "os.replace(temporary, path)" in workflow
    assert "restore_runtime_env" in workflow
    assert "systemctl restart --no-block gen-automation-staging.service" in workflow
    assert "/usr/bin/timeout --signal=TERM --kill-after=30s 600s" in command_block
    assert "/usr/bin/docker run --rm" in command_block
    assert "gen-automation-validate-migration-environment" in command_block
    assert "--network bridge" in command_block
    assert "--network host" not in command_block
    assert "--user 10001:10001" in command_block
    assert "--read-only" in command_block
    assert "--env-file /etc/gen-automation/migration.env" in command_block
    assert (
        "--mount type=bind,src=/etc/gen-automation/rds-global-bundle.pem,"
        "dst=/run/gen-automation/rds-global-bundle.pem,readonly"
    ) in command_block
    assert "--cap-drop ALL" in command_block
    assert "--security-opt no-new-privileges:true" in command_block
    assert "python3.12 -m alembic upgrade head" in command_block
    assert command_block.index("python3.12 -m alembic upgrade head") < command_block.index(
        "gen-automation-update-control-plane"
    )
    assert command_block.index(
        "gen-automation-validate-migration-environment"
    ) < command_block.index("python3.12 -m alembic upgrade head")
    for prohibited in (
        "TOKEN",
        "PASSWORD",
        "SECRET",
        "PATREON",
        "MEGA",
        "DATABASE",
    ):
        assert prohibited not in command_block.upper()


def test_routine_rollout_refreshes_the_mega_bootstrap_bundle_safely() -> None:
    workflow = _workflow()
    helper = (DEPLOY / "bootstrap-mega-profile.sh").read_bytes()
    compose = (DEPLOY / "compose.bootstrap.yaml").read_bytes()

    assert "repos/${GITHUB_REPOSITORY}/contents/${relative_path}?ref=${SOURCE_REVISION}" in workflow
    assert "infra/aws-staging/deploy/bootstrap-mega-profile.sh" in workflow
    assert "infra/aws-staging/deploy/compose.bootstrap.yaml" in workflow
    assert 'bash -n "$host_bundle_dir/bootstrap-mega-profile.sh"' in workflow
    assert 'base64 --wrap=0 "$host_bundle_dir/bootstrap-mega-profile.sh"' in workflow
    assert 'base64 --wrap=0 "$host_bundle_dir/compose.bootstrap.yaml"' in workflow

    program = _rollout_command_program()
    compile(program, "staging-rollout-ssm-command", "exec")
    environment = {
        "SSM_IMAGE_REF": (
            "ghcr.io/neuraln-cyber/gen-automation/control-plane-mega@sha256:" + "a" * 64
        ),
        "SSM_WORKER_IMAGE_REF": (
            "ghcr.io/neuraln-cyber/gen-automation/gpu-worker@sha256:" + "b" * 64
        ),
        "SSM_SOURCE_REVISION": "c" * 40,
        "SSM_BOOTSTRAP_HELPER_SHA256": "d" * 64,
        "SSM_BOOTSTRAP_COMPOSE_SHA256": "e" * 64,
        "SSM_BOOTSTRAP_HELPER_BASE64": base64.b64encode(helper).decode("ascii"),
        "SSM_BOOTSTRAP_COMPOSE_BASE64": base64.b64encode(compose).decode("ascii"),
    }
    result = subprocess.run(  # noqa: S603 - executes the repository-owned rollout fixture.
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        env={**os.environ, **environment},
        text=True,
    )
    command = json.loads(result.stdout)["commands"][0]

    assert len(command) < 24_000
    assert "/usr/bin/base64 --decode" in command
    assert command.count("/usr/bin/sha256sum --check --status") == 2
    assert '/usr/bin/bash -n "$bundle_root/bootstrap-mega-profile.sh"' in command
    assert "--profile bootstrap config --quiet" in command
    assert (
        "sudo /usr/bin/install -o root -g root -m 0644 "
        '"$bundle_root/compose.bootstrap.yaml" '
        "/opt/gen-automation/deploy/compose.bootstrap.yaml"
    ) in command
    assert (
        "sudo /usr/bin/install -o root -g root -m 0755 "
        '"$bundle_root/bootstrap-mega-profile.sh" '
        "/usr/local/sbin/gen-automation-bootstrap-mega-profile"
    ) in command
    assert "raw.githubusercontent.com" not in command
    assert "/usr/bin/curl" not in command
    assert command.index("--profile bootstrap config --quiet") < command.index(
        "/opt/gen-automation/deploy/compose.bootstrap.yaml"
    )
    assert command.index("/usr/local/sbin/gen-automation-bootstrap-mega-profile") < command.index(
        "/etc/gen-automation/control-plane.env"
    )


def test_migration_environment_is_private_separate_and_tls_verified() -> None:
    validator = _migration_validator()
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    deployment_validator = (DEPLOY / "validate-deployment.sh").read_text(encoding="utf-8")
    example = (DEPLOY / "migration.env.example").read_text(encoding="utf-8")

    assert "root:root" in validator
    assert "400|600" in validator
    assert "exactly one active assignment" in validator
    assert "may define only GEN_AUTOMATION_DATABASE_URL" in validator
    assert "migration and runtime database URLs must be distinct" in validator
    assert "migration and runtime database usernames must be distinct" in validator
    assert "urlsplit" in validator
    assert "parse_qs" in validator
    assert 'python3 - "$migration_env" "$runtime_env"' in validator
    assert "sslmode=verify-full" in validator
    assert 'query.get("sslrootcert")' in validator
    assert "/run/gen-automation/rds-global-bundle.pem" in validator
    assert "gen-automation-validate-migration-environment" in installer
    assert "migration.env" in deployment_validator
    assert example.count("GEN_AUTOMATION_DATABASE_URL=") == 1
    assert "master role" in example


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


def test_semantic_anatomy_promotion_is_shadow_only_monotonic_and_reversible() -> None:
    promoter = _semantic_promoter()
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")

    assert "default_max_assessments=400" in promoter
    assert "max_initial_backlog=1000" in promoter
    assert "max assessments must be an integer from 1 through 1000" in promoter
    assert 'operation="status"' in promoter
    assert "--dry-run)" in promoter
    assert "--promote)" in promoter
    assert "--pause)" in promoter
    assert 'GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED": "true"' in promoter
    assert 'GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE": "shadow"' in promoter
    assert 'GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST": "[]"' in promoter
    assert 'updates = {"GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED": "false"}' in promoter
    assert "requested cap $requested_max would lower the current cap $current_max" in promoter
    assert "flock --exclusive --wait 60 8" in promoter
    assert "flock --exclusive --wait 60 9" in promoter
    assert "for _ in $(seq 1 50)" in promoter
    assert promoter.count("--max-time 2") >= 3
    assert "sleep 3" in promoter
    assert 'mktemp "$config_root/.control-plane.env.semantic.update.' in promoter
    assert 'mv -- "$temporary_env" "$controller_env"' in promoter
    assert "restore_previous_environment" in promoter
    assert 'mv -- "$backup_env" "$controller_env"' in promoter
    assert "restore_previous_environment || rollback_failed=1" in promoter
    assert "Rollback copy preserved at $backup_env." in promoter
    assert '[ "$rollback_failed" -eq 1 ]' in promoter
    assert 'systemctl restart --no-block "$service_name"' in promoter
    assert "http://127.0.0.1:8091/health/ready" in promoter
    assert "http://127.0.0.1:8000/api/v1/health/ready" in promoter
    assert "semantic_anatomy_asset_allowlist_count=" in promoter
    assert "semantic_anatomy_configured_per_scoring_run_cap=" in promoter
    assert '("pending", "processing", "retry_wait", "completed", "unavailable")' in promoter
    assert "semantic_current_profile_{state}_count=" in promoter
    assert "semantic_successful_canary_gate=" in promoter
    assert "promotion requires at least one completed current-profile canary assessment" in promoter
    assert "semantic_open_review_task_count=" in promoter
    assert "semantic_open_review_ranked_asset_count=" in promoter
    assert "semantic_open_review_missing_current_profile_count=" in promoter
    assert "semantic_projected_new_assessment_count=" in promoter
    assert "semantic_projected_attempt_ceiling=" in promoter
    assert "semantic_initial_backlog_guard=" in promoter
    assert "semantic_initial_backlog_guard_limit=" in promoter
    assert "min(int(missing), max(0, projection_cap - int(existing)))" in promoter
    assert "GROUP BY score.scoring_run_id" in promoter
    assert "projected initial backlog $projected_count exceeds the hard limit" in promoter
    assert 'os.environ["GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS"]' in promoter
    assert "docker compose" in promoter
    assert '[ -x /usr/bin/docker ] || fail "/usr/bin/docker is required"' in promoter
    assert "exec --no-TTY control-plane-mega" in promoter
    assert 'GEN_AUTOMATION_DATABASE_URL"]' in promoter
    assert 'print(f"semantic_profile_sha256=' in promoter
    assert 'fail "$operation requires --expected-control-plane-revision"' in promoter
    assert promoter.count('actual_revision="$(control_plane_revision)"') == 2
    assert "control_plane_source_revision=" in promoter
    assert "org.opencontainers.image.revision" in promoter
    assert "GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_API_KEY" not in promoter
    assert "existing rows were preserved" in promoter
    paused_branch = promoter.rsplit('if [ "$operation" = "pause" ]; then', maxsplit=1)[1].split(
        "else", maxsplit=1
    )[0]
    assert "rollback_armed=0" in paused_branch
    assert "wait_for_stack" not in paused_branch
    assert "control_plane_health=$(health_status" in paused_branch
    assert "semantic_gateway_health=$(health_status" in paused_branch
    assert "gen-automation-promote-semantic-anatomy" in installer
    assert '"$source_dir/promote-semantic-anatomy.sh"' in installer

    coverage_program = promoter.split("coverage_program <<'PY' || true\n", maxsplit=1)[1].split(
        "\nPY\n", maxsplit=1
    )[0]
    compile(coverage_program, "semantic-anatomy-coverage-status", "exec")


def test_semantic_anatomy_promotion_workflow_uses_pinned_oidc_ssm_dispatch() -> None:
    workflow = _workflow()
    manual_job = workflow.split("  semantic-anatomy:\n", maxsplit=1)[1]

    assert "workflow_dispatch:" in workflow
    assert "default: dry-run" in workflow
    assert 'default: "400"' in workflow
    assert "assessment cap (1-1000)" in workflow
    assert "timeout-minutes: 25" in workflow
    assert "status|dry-run|promote|pause" in workflow
    assert '"pause": "--pause"' in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "id-token: write" in workflow
    assert "contents: read" in workflow
    assert "packages: write" not in workflow
    assert (
        "aws-actions/configure-aws-credentials@acca2b1b2070338fb9fd1ca27ecee81d687e58e5"
    ) in workflow
    assert "role-to-assume: ${{ vars.AWS_STAGING_DEPLOY_ROLE_ARN }}" in workflow
    assert 'allowed-account-ids: "861912887470"' in workflow
    assert "role-duration-seconds: 1800" in workflow
    assert "--timeout-seconds 1200" in workflow
    assert "for _ in $(seq 1 250)" in workflow
    assert "aws ssm send-command" in workflow
    assert "AWS-RunShellScript" in workflow
    assert "raw.githubusercontent.com/neuraln-cyber/gen-automation/" in workflow
    assert "SSM_SCRIPT_SHA256" in workflow
    assert "sha256sum --check --status" in workflow
    assert "promote-semantic-anatomy.sh" in workflow
    assert "--expected-control-plane-revision" in workflow
    assert 'if operation in {"dry-run", "promote"}' in workflow
    assert "AWS_ACCESS_KEY_ID" not in workflow
    assert "AWS_SECRET_ACCESS_KEY" not in workflow
    assert "secrets.AWS" not in workflow

    embedded = textwrap.dedent(
        manual_job.split("python3 -c '", maxsplit=1)[1].split('\' >"$parameters_file"', maxsplit=1)[
            0
        ]
    )
    command = shlex.split(f"python3 -c '{embedded}'", posix=True)
    assert command[:2] == ["python3", "-c"]
    compile(command[2], "semantic-anatomy-ssm-command", "exec")
    assert not (ROOT / ".github" / "workflows" / "promote-semantic-anatomy.yml").exists()
