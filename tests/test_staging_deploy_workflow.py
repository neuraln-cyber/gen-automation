import base64
import gzip
import hashlib
import json
import os
import re
import shlex
import shutil
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


def _lora_configurator() -> str:
    return (DEPLOY / "configure-lora-manager.sh").read_text(encoding="utf-8")


def _i2v_profile_configurator() -> str:
    return (DEPLOY / "configure-i2v-lora-profile.sh").read_text(encoding="utf-8")


def _i2v_worker_rollout() -> str:
    return (DEPLOY / "rollout-i2v-lora-worker.sh").read_text(encoding="utf-8")


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


def _lora_command_program() -> str:
    operation = (
        _workflow()
        .split("  lora-manager:\n", maxsplit=1)[1]
        .split(
            "      - name: Run the pinned LoRA-manager operation through Systems Manager\n",
            maxsplit=1,
        )[1]
    )
    embedded = textwrap.dedent(
        operation.split("python3 -c '", maxsplit=1)[1].split(
            '\' >"$parameters_file"',
            maxsplit=1,
        )[0]
    )
    command = shlex.split(f"python3 -c '{embedded}'", posix=True)
    assert command[:2] == ["python3", "-c"]
    return command[2]


def _i2v_profile_command_program() -> str:
    operation = (
        _workflow()
        .split("  i2v-lora-profile:\n", maxsplit=1)[1]
        .split(
            "      - name: Run the pinned I2V LoRA profile operation through Systems Manager\n",
            maxsplit=1,
        )[1]
    )
    embedded = textwrap.dedent(
        operation.split("python3 -c '", maxsplit=1)[1].split(
            '\' >"$parameters_file"',
            maxsplit=1,
        )[0]
    )
    command = shlex.split(f"python3 -c '{embedded}'", posix=True)
    assert command[:2] == ["python3", "-c"]
    return command[2]


def _i2v_worker_command_program() -> str:
    operation = (
        _workflow()
        .split("  i2v-lora-worker:\n", maxsplit=1)[1]
        .split(
            "      - name: Run the checksum-pinned worker operation through Systems Manager\n",
            maxsplit=1,
        )[1]
    )
    embedded = textwrap.dedent(
        operation.split("python3 -c '", maxsplit=1)[1].split(
            '\' >"$parameters_file"',
            maxsplit=1,
        )[0]
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
    assert "i2v-worker@sha256:[0-9a-f]{64}" in workflow
    assert "WORKER_IMAGE_REPOSITORY: ghcr.io/neuraln-cyber/gen-automation/gpu-worker" in workflow
    assert "staging-worker-source-revision" in workflow
    assert "staging-worker-image-digest" in workflow
    assert "staging-i2v-worker-source-revision" in workflow
    assert "staging-i2v-worker-image-digest" in workflow
    assert "tr -d '\\r\\n' <\"$metadata_dir/staging-i2v-worker-source-revision\"" in workflow
    assert '[[ "$i2v_worker_source_revision" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert "I2V_WORKER_SOURCE_REVISION=%s" in workflow
    assert 'worker_image_ref="${WORKER_IMAGE_REPOSITORY}@${PUBLISHED_WORKER_IMAGE_DIGEST}"' in (
        workflow
    )
    assert '[ "$worker_image_digest" = "$PUBLISHED_WORKER_IMAGE_DIGEST" ]' in workflow
    assert '[ "$worker_image_revision" = "$WORKER_SOURCE_REVISION" ]' in workflow
    assert '[ "$i2v_worker_image_revision" = "$I2V_WORKER_SOURCE_REVISION" ]' in workflow
    assert '[ "$i2v_worker_image_revision" = "$SOURCE_REVISION" ]' not in workflow
    assert "WORKER_IMAGE_REF=%s@%s" in workflow
    assert "I2V_WORKER_IMAGE_REF=%s@%s" in workflow
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


def test_lora_promotion_is_explicit_gated_reversible_and_keyless() -> None:
    workflow = _workflow()
    routine = workflow.split("  deploy:\n", maxsplit=1)[1].split(
        "  semantic-anatomy:\n", maxsplit=1
    )[0]

    assert "name: Deploy staging control plane" in workflow
    assert "workflow_dispatch:" in workflow
    assert "component:" in workflow
    assert "- semantic-anatomy\n          - lora-manager" in workflow
    assert "  lora-manager:" in workflow
    assert "inputs.component == 'lora-manager'" in workflow
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "group: deploy-staging-control-plane" in workflow
    assert "id-token: write" in workflow
    assert "contents: read" in workflow
    assert "contents: write" not in workflow
    assert "role-to-assume: ${{ vars.AWS_STAGING_DEPLOY_ROLE_ARN }}" in workflow
    assert 'allowed-account-ids: "861912887470"' in workflow
    assert "AWS_STAGING_INSTANCE_ID: ${{ vars.AWS_STAGING_INSTANCE_ID }}" in workflow
    assert (
        "AWS_STAGING_CIVITAI_API_SECRET_ARN: ${{ vars.AWS_STAGING_CIVITAI_API_SECRET_ARN }}"
    ) in workflow
    assert (
        "AWS_STAGING_LORA_MANAGER_PREREQUISITES_APPLIED: "
        "${{ vars.AWS_STAGING_LORA_MANAGER_PREREQUISITES_APPLIED }}"
    ) in workflow
    validation = workflow.split(
        "- name: Validate non-secret LoRA-manager promotion coordinates", maxsplit=1
    )[1].split("- name: Exchange GitHub OIDC identity", maxsplit=1)[0]
    enable_branch = validation.split('if [ "$OPERATION" = "enable" ]; then', maxsplit=1)[1]
    assert "gen-automation/staging/civitai-[A-Za-z0-9]{6}" in enable_branch
    assert '[ "$AWS_STAGING_LORA_MANAGER_PREREQUISITES_APPLIED" = "true" ]' in enable_branch
    assert "CORS/IAM/secret infrastructure apply is attested" in enable_branch
    assert "aws ssm send-command" in workflow
    assert "AWS-RunShellScript" in workflow
    assert "aws ssm get-command-invocation" in workflow
    assert "AWS_ACCESS_KEY_ID" not in workflow
    assert "AWS_SECRET_ACCESS_KEY" not in workflow
    assert "secrets.AWS" not in workflow
    assert "GEN_AUTOMATION_LORA_MANAGER_ENABLED" not in routine
    assert "gen-automation-configure-lora-manager" not in routine
    assert not (ROOT / ".github" / "workflows" / "configure-lora-manager-staging.yml").exists()


def test_lora_promotion_embeds_only_verified_scripts_and_disable_needs_no_arn() -> None:
    workflow = _workflow()
    lora_job = workflow.split("  lora-manager:\n", maxsplit=1)[1]
    configurator = (DEPLOY / "configure-lora-manager.sh").read_bytes()
    validator = (DEPLOY / "validate-deployment.sh").read_bytes()
    configurator_sha256 = hashlib.sha256(configurator).hexdigest()
    validator_sha256 = hashlib.sha256(validator).hexdigest()
    configurator_payload = base64.b64encode(
        gzip.compress(configurator, compresslevel=9, mtime=0)
    ).decode("ascii")
    validator_payload = base64.b64encode(gzip.compress(validator, compresslevel=9, mtime=0)).decode(
        "ascii"
    )
    program = _lora_command_program()
    compile(program, "staging-lora-manager-ssm-command", "exec")
    civitai_arn = (
        "arn:aws:secretsmanager:eu-central-1:861912887470:"
        "secret:gen-automation/staging/civitai-Ab12Cd"
    )

    commands: dict[str, str] = {}
    for operation in ("status", "disable", "enable"):
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            env={
                **os.environ,
                "SSM_OPERATION": operation,
                "SSM_CIVITAI_API_SECRET_ARN": civitai_arn,
                "SSM_SOURCE_REVISION": "c" * 40,
                "SSM_CONFIGURATOR_SHA256": configurator_sha256,
                "SSM_VALIDATOR_SHA256": validator_sha256,
                "SSM_CONFIGURATOR_PAYLOAD": configurator_payload,
                "SSM_VALIDATOR_PAYLOAD": validator_payload,
            },
            text=True,
        )
        commands[operation] = json.loads(result.stdout)["commands"][0]

    for command in commands.values():
        assert len(command.encode("utf-8")) <= 24_000
        assert configurator_payload in command
        assert validator_payload in command
        assert configurator_sha256 in command
        assert validator_sha256 in command
        assert command.count("/usr/bin/gzip --decompress") == 2
        assert command.count("/usr/bin/sha256sum --check --status") == 2
        assert '/usr/bin/bash -n "$payload_root/configure-lora-manager.sh"' in command
        assert '/usr/bin/bash -n "$payload_root/validate-deployment.sh"' in command
        bash = shutil.which("bash")
        if bash is not None:
            syntax = subprocess.run(  # noqa: S603
                [bash, "-n"],
                check=False,
                capture_output=True,
                input=command,
                text=True,
            )
            assert syntax.returncode == 0, syntax.stderr

    assert "--status" in commands["status"]
    assert "--validator" not in commands["status"]
    assert civitai_arn not in commands["status"]
    assert "--disable" in commands["disable"]
    assert "--validator" in commands["disable"]
    assert civitai_arn not in commands["disable"]
    assert "--enable" in commands["enable"]
    assert "--civitai-secret-arn" in commands["enable"]
    assert "--expected-control-plane-revision" in commands["enable"]
    assert "c" * 40 in commands["enable"]
    assert civitai_arn in commands["enable"]
    assert "c" * 40 not in commands["status"]
    assert "c" * 40 not in commands["disable"]
    assert 'if len(command.encode("utf-8")) > 24_000' in program
    assert "raw.githubusercontent.com" not in lora_job


def test_publication_exports_its_exact_ci_source_for_nested_workflow_run() -> None:
    publication = (ROOT / ".github" / "workflows" / "publish-images.yml").read_text(
        encoding="utf-8"
    )

    assert "record-staging-source:" in publication
    assert "needs: [publish, publish-worker, publish-i2v-worker]" in publication
    assert "SOURCE_SHA: ${{ github.event.workflow_run.head_sha }}" in publication
    assert "printf '%s\\n' \"$SOURCE_SHA\"" in publication
    assert "WORKER_SOURCE_REVISION: ${{ needs.publish-worker.outputs.source_revision }}" in (
        publication
    )
    assert "WORKER_IMAGE_DIGEST: ${{ needs.publish-worker.outputs.image_digest }}" in publication
    assert "I2V_SOURCE_REVISION: ${{ needs.publish-i2v-worker.outputs.source_revision }}" in (
        publication
    )
    assert "I2V_IMAGE_DIGEST: ${{ needs.publish-i2v-worker.outputs.image_digest }}" in publication
    assert "staging-worker-source-revision" in publication
    assert "staging-worker-image-digest" in publication
    assert "staging-i2v-worker-source-revision" in publication
    assert "staging-i2v-worker-image-digest" in publication
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
    same_image_branch = updater.split('if [ "$new_image" = "$old_image" ]; then', maxsplit=1)[
        1
    ].split("fi", maxsplit=1)[0]
    assert 'systemctl start --no-block "$service_name"' in same_image_branch
    assert same_image_branch.index("systemctl start") < same_image_branch.index(
        "wait_for_control_plane"
    )
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
    assert "restore_runtime_env_stopped" in workflow
    assert "stop_control_plane" in workflow
    assert "systemctl stop gen-automation-staging.service" in workflow
    assert "systemctl is-active --quiet" in workflow
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
        "systemctl stop gen-automation-staging.service"
    ) < command_block.index("python3.12 -m alembic upgrade head")
    assert "--rollback-mode leave-stopped" in command_block
    assert "--external-lock-held" in command_block
    assert "unlocked_command = (" in command_block
    assert "sudo /usr/bin/flock --exclusive --wait 120" in command_block
    assert "/run/lock/gen-automation-control-plane-update.lock" in command_block
    assert command_block.index("sudo /usr/bin/flock") < command_block.index(
        "shlex.quote(unlocked_command)"
    )
    post_migration = command_block.split("if {migration_command}; then", maxsplit=1)[1].split(
        "else ( {restore_runtime_env_stopped}; false )", maxsplit=1
    )[0]
    assert "restore_runtime_env_stopped" in post_migration
    assert "restore_runtime_env};" not in post_migration
    assert "systemctl restart" not in post_migration
    stopped_restore_block = workflow.split("restore_runtime_env_stopped = (", maxsplit=1)[1].split(
        "migration_preflight = (", maxsplit=1
    )[0]
    assert "systemctl stop gen-automation-staging.service" in stopped_restore_block
    assert "systemctl is-active --quiet" in stopped_restore_block
    assert "systemctl restart" not in stopped_restore_block
    assert "else ( {restore_runtime_env_stopped}; false ); fi" in command_block
    assert command_block.count("else ( {restore_runtime_env}; false ); fi") == 1
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


def test_routine_rollout_always_preserves_i2v_pin_and_profile_state() -> None:
    workflow = _workflow()
    rollout = workflow.split(
        "      - name: Roll out through AWS Systems Manager\n",
        maxsplit=1,
    )[1].split("\n  semantic-anatomy:", maxsplit=1)[0]
    program = _rollout_command_program()
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
        "SSM_CONTROL_PLANE_UPDATER_SHA256": "f" * 64,
        "SSM_BOOTSTRAP_HELPER_GZIP_BASE64": "AA==",
        "SSM_BOOTSTRAP_COMPOSE_GZIP_BASE64": "AA==",
        "SSM_CONTROL_PLANE_UPDATER_GZIP_BASE64": "AA==",
    }

    assert 'SSM_I2V_WORKER_IMAGE_REF="$I2V_WORKER_IMAGE_REF"' not in rollout
    assert "COORDINATED_I2V" not in workflow

    routine = subprocess.run(  # noqa: S603 - executes the repository-owned fixture.
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        env={**os.environ, **environment},
        text=True,
    )
    routine_command = json.loads(routine.stdout)["commands"][0]
    assert "GEN_AUTOMATION_SALAD_WORKER_IMAGE" in routine_command
    assert "GEN_AUTOMATION_I2V_WORKER_IMAGE" not in routine_command
    assert "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED" not in routine_command
    assert "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED" not in routine_command
    assert "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED" not in routine_command
    assert "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON" not in routine_command
    assert "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256" not in routine_command
    assert "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION" not in routine_command


def test_post_migration_updater_failure_restores_files_but_never_restarts_old_code() -> None:
    updater = _updater()

    assert 'rollback_mode="restart"' in updater
    assert "external_lock_held=0" in updater
    assert "--external-lock-held)" in updater
    assert 'if [ "$external_lock_held" -eq 0 ]; then' in updater
    assert "restart|leave-stopped" in updater
    leave_stopped = updater.split('if [ "$rollback_mode" = "leave-stopped" ]; then', maxsplit=1)[
        1
    ].split("else", maxsplit=1)[0]
    assert 'systemctl stop "$service_name"' in leave_stopped
    assert 'systemctl is-active --quiet "$service_name"' in leave_stopped
    assert "systemctl restart" not in leave_stopped
    assert "wait_for_control_plane" not in leave_stopped


def test_lora_configurator_is_atomic_idempotent_and_requires_manifest_trust_anchor(
    tmp_path: Path,
) -> None:
    configurator = _lora_configurator()
    installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    program = configurator.split("<<'PY'\n", maxsplit=1)[1].split("\nPY\n", maxsplit=1)[0]
    compile(program, "configure-lora-manager-environment", "exec")

    assert "root:root" in configurator
    assert "mode 0400 or 0600" in configurator
    assert "flock --exclusive --wait 120" in configurator
    assert "--external-lock-held" in configurator
    assert "--status|--enable|--disable" in configurator
    assert "--expected-control-plane-revision" in configurator
    assert "control_plane_revision" in configurator
    assert "org.opencontainers.image.revision" in configurator
    assert "running control-plane revision $actual_revision does not match $expected_revision" in (
        configurator
    )
    assert "restore_previous_configuration" in configurator
    assert "wait_for_control_plane" in configurator
    assert ".control-plane.env.lora.rollback." in configurator
    assert ".gen-automation-validate-deployment.lora.rollback." in configurator
    assert ".gen-automation-configure-lora-manager.update." in configurator
    assert 'mv -f -- "$helper_temporary" "$installed_helper"' in configurator
    assert "systemctl is-active --quiet" in configurator
    assert "gen-automation/staging/civitai-[A-Za-z0-9]{6}" in configurator
    assert "GEN_AUTOMATION_CIVITAI_API_KEY" in configurator
    assert "a direct Civitai API key is forbidden in staging" in configurator
    assert "gen-automation-configure-lora-manager" in installer
    assert '"$source_dir/configure-lora-manager.sh"' in installer

    manifest_sha256 = "a" * 64
    manifest_json = json.dumps(
        {"manifest_sha256": manifest_sha256, "artifacts": []},
        separators=(",", ":"),
    )
    environment_path = tmp_path / "control-plane.env"
    environment_path.write_text(
        "\n".join(
            (
                "GEN_AUTOMATION_ENVIRONMENT=staging",
                "GEN_AUTOMATION_BACKGROUND_RUNTIME_ENABLED=true",
                "GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_BUCKET=model-bucket",
                "GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_REGION=eu-central-1",
                f"GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_JSON={manifest_json}",
                "GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256=" + manifest_sha256,
                "GEN_AUTOMATION_QUALITY_SCORING_ENABLED=true",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    civitai_arn = (
        "arn:aws:secretsmanager:eu-central-1:861912887470:"
        "secret:gen-automation/staging/civitai-Ab12Cd"
    )
    command = [
        sys.executable,
        "-c",
        program,
        str(environment_path),
        "enable",
        civitai_arn,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    configured = environment_path.read_text(encoding="utf-8")
    assert configured.count("GEN_AUTOMATION_LORA_MANAGER_ENABLED=true\n") == 1
    assert (
        configured.count(
            f"GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE=aws-secrets-manager://{civitai_arn}\n"
        )
        == 1
    )
    assert "GEN_AUTOMATION_QUALITY_SCORING_ENABLED=true\n" in configured
    assert "GEN_AUTOMATION_CIVITAI_API_KEY=" not in configured

    before_replay = environment_path.read_bytes()
    subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603
    assert environment_path.read_bytes() == before_replay

    missing_anchor = tmp_path / "missing-anchor.env"
    missing_anchor.write_text(
        configured.replace(
            f"GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256={manifest_sha256}\n",
            "",
        ),
        encoding="utf-8",
    )
    before_failure = missing_anchor.read_bytes()
    failed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program, str(missing_anchor), "enable", civitai_arn],
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert "MODEL_MANIFEST_SHA256 exactly once" in failed.stderr
    assert missing_anchor.read_bytes() == before_failure

    mismatched_anchor = tmp_path / "mismatched-anchor.env"
    mismatched_anchor.write_text(
        configured.replace(
            f'"manifest_sha256":"{manifest_sha256}"',
            f'"manifest_sha256":"{"b" * 64}"',
        ),
        encoding="utf-8",
    )
    mismatch_before = mismatched_anchor.read_bytes()
    mismatch = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            program,
            str(mismatched_anchor),
            "enable",
            civitai_arn,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert mismatch.returncode != 0
    assert "does not match its independent trust anchor" in mismatch.stderr
    assert mismatched_anchor.read_bytes() == mismatch_before

    disabled_without_credential = tmp_path / "disabled.env"
    disabled_without_credential.write_text(
        "GEN_AUTOMATION_LORA_MANAGER_ENABLED=true\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            program,
            str(disabled_without_credential),
            "disable",
            "",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert disabled_without_credential.read_text(encoding="utf-8") == (
        "GEN_AUTOMATION_LORA_MANAGER_ENABLED=false\nGEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE=\n"
    )


def test_i2v_lora_worker_rollout_is_bounded_queue_preserving_and_two_phase() -> None:
    workflow = _workflow()
    rollout = _i2v_worker_rollout()
    worker_job = workflow.split("  i2v-lora-worker:\n", maxsplit=1)[1].split(
        "\n  i2v-lora-profile:\n", maxsplit=1
    )[0]

    assert "- i2v-lora-worker" in workflow
    assert "inputs.component == 'i2v-lora-worker'" in worker_job
    assert 'case "$OPERATION" in status|dry-run|promote|rollback)' in worker_job
    assert 'case "$operation" in status|dry-run|promote|rollback)' in rollout
    assert "timeout-minutes: 350" in worker_job
    assert "role-duration-seconds: 20400" in worker_job
    assert '"executionTimeout": ["19000"]' in worker_job
    assert "--timeout-seconds 180" in worker_job
    assert "for _ in $(seq 1 3900)" in worker_job
    assert 'deadline="$(( $(cut -d. -f1 /proc/uptime) + 600 ))"' in rollout
    assert '[ "$remaining" -gt 0 ] || return 1' in rollout
    assert 'sleep "$((remaining < 5 ? remaining : 5))"' in rollout
    assert 'timeout --signal=TERM --kill-after=60s "$timeout_seconds"' in rollout
    for bound in ("900s dry-run", "1900s rollback", "10000s promote"):
        assert bound in rollout
    # Worst-case automatic recovery fits inside the RunShellScript execution
    # budget. The separate SSM delivery allowance plus execution then fits
    # inside polling, which fits inside OIDC, which fits inside the job timeout.
    worst_run_one_off_dry_run = 900 + 60
    worst_restart = 600
    worst_run_one_off_promote = 10_000 + 60
    worst_run_one_off_preflight = 900 + 60
    # The outer watchdog is 1900s, while the shared provider rollback deadline
    # is 1800s plus the one-off process's 60s termination grace.
    worst_run_one_off_rollback = 1_800 + 60
    worst_full_sequence = (
        worst_run_one_off_dry_run
        + worst_restart
        + worst_run_one_off_promote
        + worst_run_one_off_preflight
        + worst_restart
        # Failure cleanup returns to maintenance before touching the provider.
        + worst_restart
        + worst_run_one_off_rollback
        + worst_restart
    )
    execution_timeout = 19_000
    delivery_timeout = 180
    polling_timeout = 3_900 * 5
    oidc_timeout = 20_400
    workflow_timeout = 350 * 60
    github_hosted_job_maximum = 360 * 60
    assert worst_full_sequence < execution_timeout
    assert execution_timeout + delivery_timeout < polling_timeout < oidc_timeout < workflow_timeout
    assert workflow_timeout <= github_hosted_job_maximum
    assert 'systemctl restart --no-block "$service_name"' in rollout
    assert "while true" not in worker_job

    assert "Bind the immutable worker digest to its intrinsic source revision" in worker_job
    assert "if: inputs.operation != 'rollback'" in worker_job
    assert worker_job.count("docker buildx imagetools inspect") == 2
    assert "--format '{{json .Manifest}}'" in worker_job
    assert "--format '{{json .Image}}'" in worker_job
    assert 'Labels"]["org.opencontainers.image.revision"]' in worker_job
    assert '[ "${EXPECTED_WORKER_IMAGE#*@}" = "$actual_digest" ]' in worker_job
    assert '[ "$actual_revision" = "$EXPECTED_WORKER_SOURCE_REVISION" ]' in worker_job

    assert "bash -n infra/aws-staging/deploy/rollout-i2v-lora-worker.sh" in worker_job
    assert "I2V_ROLLOUT_SCRIPT_SHA256" in worker_job
    assert worker_job.count("/usr/bin/sha256sum --check --status") == 1
    assert worker_job.count('/usr/bin/bash -n \\"$payload_root/rollout-i2v-lora-worker.sh\\"') == 1
    assert "SSM command exceeds the reviewed size bound" in worker_job

    assert 'manifest_bucket="gen-automation-staging-861912887470-eu-central-1-models"' in rollout
    assert (
        'manifest_key="worker/i2v/manifests/sha256/'
        'f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e.json"' in rollout
    )
    assert 'manifest_version="u4bSnCPzDJ4zctrA2Nr66ji0Zh2qPpXX"' in rollout
    assert (
        'manifest_source_sha256="'
        'f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e"' in rollout
    )
    assert "i2v-worker@sha256:[0-9a-f]{64}" in rollout

    for flag in (
        "GEN_AUTOMATION_I2V_ENABLED",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED",
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED",
    ):
        assert f'"{flag}": "false"' in rollout
    assert 'assert_container_flags "$maintenance_id" false false false false' in rollout
    assert 'assert_container_flags "$target_container" true true true false' in rollout
    assert "http://127.0.0.1:8000/api/v1/health/ready" in rollout
    assert "systemctl stop" not in rollout
    assert "docker stop" not in rollout
    assert "compose stop" not in rollout
    assert "compose down" not in rollout

    promotion = rollout.split(
        '[ ! -e "$active_state" ] || fail "a prior reviewed-worker rollback bundle already exists"',
        maxsplit=1,
    )[1]
    assert promotion.index('run_one_off "$original_env" "$initial_container" 900s dry-run') < (
        promotion.index('restart_into "$maintenance_env"')
    )
    assert promotion.index('restart_into "$maintenance_env"') < promotion.index(
        'run_one_off "$original_env" "$maintenance_id" 10000s promote'
    )
    assert promotion.index(
        'run_one_off "$original_env" "$maintenance_id" 10000s promote'
    ) < promotion.index('run_one_off "$target_env" "$maintenance_id" 900s profile-preflight')
    assert promotion.index(
        'run_one_off "$target_env" "$maintenance_id" 900s profile-preflight'
    ) < promotion.index('mv -- "$active_temporary" "$active_state"')
    assert promotion.index('mv -- "$active_temporary" "$active_state"') < promotion.index(
        'restart_into "$target_env"'
    )
    assert promotion.index('restart_into "$target_env"') < promotion.index(
        'assert_container_flags "$target_container" true true true false'
    )

    rollback = rollout.split("rollback_after_failure() {\n", maxsplit=1)[1].split(
        "\n}\n\ncleanup()", maxsplit=1
    )[0]
    assert rollback.index('restart_into "$maintenance_env"') < rollback.index(
        'run_one_off "$original_env" "$maintenance_id" 1900s rollback'
    )
    assert rollback.index(
        'run_one_off "$original_env" "$maintenance_id" 1900s rollback'
    ) < rollback.index('restart_into "$original_env"')
    assert "backups remain under $work_dir" in rollback
    assert "provider-mutation-attempted.json" in rollout
    assert "merge_saved_i2v_profile" in rollout
    assert 'status_json="$(run_one_off "$controller_env" "$initial_container" 900s status)"' in (
        rollout
    )
    explicit_rollback = rollout.split('if [ "$operation" = "rollback" ]; then', maxsplit=2)[2]
    assert explicit_rollback.index('resume_env="$original_env"') < explicit_rollback.index(
        'restart_into "$original_env"'
    )

    for queue_mutation in (
        "create_job",
        "cancel_job",
        "retry_job",
        "reorder_job",
        "/api/v1/i2v/jobs",
    ):
        assert queue_mutation not in rollout


def test_i2v_lora_worker_workflow_transfers_only_checksum_pinned_helper() -> None:
    rollout = (DEPLOY / "rollout-i2v-lora-worker.sh").read_bytes()
    checksum = hashlib.sha256(rollout).hexdigest()
    payload = base64.b64encode(gzip.compress(rollout, compresslevel=9, mtime=0)).decode("ascii")
    program = _i2v_worker_command_program()
    compile(program, "staging-i2v-lora-worker-ssm-command", "exec")
    common = {
        "SSM_SOURCE_REVISION": "a" * 40,
        "SSM_WORKER_IMAGE": ("ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "b" * 64),
        "SSM_WORKER_SOURCE_REVISION": "c" * 40,
        "SSM_SCRIPT_SHA256": checksum,
        "SSM_SCRIPT_PAYLOAD": payload,
    }
    commands: dict[str, str] = {}
    for operation in ("status", "dry-run", "promote", "rollback"):
        environment = {**common, "SSM_OPERATION": operation}
        if operation == "rollback":
            environment.update({"SSM_WORKER_IMAGE": "", "SSM_WORKER_SOURCE_REVISION": ""})
        result = subprocess.run(  # noqa: S603 - repository-owned command generator.
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            env={**os.environ, **environment},
            text=True,
        )
        parameters = json.loads(result.stdout)
        command = parameters["commands"][0]
        assert parameters == {
            "commands": [command],
            "executionTimeout": ["19000"],
        }
        commands[operation] = command

    for operation, command in commands.items():
        assert len(command.encode("utf-8")) <= 24_000
        assert payload in command
        assert checksum in command
        assert command.count("/usr/bin/gzip --decompress") == 1
        assert command.count("/usr/bin/sha256sum --check --status") == 1
        assert command.count('/usr/bin/bash -n "$payload_root/rollout-i2v-lora-worker.sh"') == 1
        assert f"--{operation}" in command
        assert "--expected-control-plane-revision" in command
    for operation in ("status", "dry-run", "promote"):
        assert "--expected-worker-image" in commands[operation]
        assert "--expected-worker-source-revision" in commands[operation]
        assert common["SSM_WORKER_IMAGE"] in commands[operation]
    assert "--expected-worker-image" not in commands["rollback"]
    assert "--expected-worker-source-revision" not in commands["rollback"]
    for prohibited in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SALAD_API_KEY"):
        assert prohibited not in "\n".join(commands.values())


def test_i2v_lora_worker_profile_renderer_freezes_and_restores_exact_flags(
    tmp_path: Path,
) -> None:
    rollout = _i2v_worker_rollout()
    render_program = rollout.split("<<'PY'\n", maxsplit=1)[1].split("\nPY\n", maxsplit=1)[0]
    compile(render_program, "rollout-i2v-lora-worker-profile-renderer", "exec")
    source = tmp_path / "source.env"
    maintenance = tmp_path / "maintenance.env"
    target = tmp_path / "target.env"
    patch = tmp_path / "target.patch"
    source.write_text(
        "\n".join(
            (
                "GEN_AUTOMATION_ENVIRONMENT=staging",
                "GEN_AUTOMATION_I2V_ENABLED=true",
                "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED=true",
                "GEN_AUTOMATION_I2V_WORKER_IMAGE=prior-image",
                "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION=" + "1" * 40,
                "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256=" + "2" * 64,
                'GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON={"objects":[]}',
                "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256=" + "3" * 64,
                "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true",
                "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED=true",
                "UNRELATED_SETTING=preserved",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - executes an extracted repository-owned renderer.
        [sys.executable, "-c", render_program, str(source), str(maintenance), "maintenance", ""],
        check=True,
        capture_output=True,
        text=True,
    )
    maintenance_text = maintenance.read_text(encoding="utf-8")
    for flag in (
        "GEN_AUTOMATION_I2V_ENABLED",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED",
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED",
    ):
        assert f"{flag}=false" in maintenance_text
    assert "UNRELATED_SETTING=preserved" in maintenance_text
    assert "GEN_AUTOMATION_I2V_WORKER_IMAGE=prior-image" in maintenance_text

    target_values = {
        "GEN_AUTOMATION_I2V_ENABLED": "true",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED": "true",
        "GEN_AUTOMATION_I2V_WORKER_IMAGE": (
            "ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "4" * 64
        ),
        "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION": "5" * 40,
        "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256": "6" * 64,
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON": '{"objects":[]}',
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256": "7" * 64,
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "true",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "false",
    }
    patch.write_text(
        "".join(f"{key}={value}\n" for key, value in target_values.items()),
        encoding="utf-8",
    )
    subprocess.run(  # noqa: S603 - executes an extracted repository-owned renderer.
        [sys.executable, "-c", render_program, str(source), str(target), "target", str(patch)],
        check=True,
        capture_output=True,
        text=True,
    )
    target_text = target.read_text(encoding="utf-8")
    for key, value in target_values.items():
        assert f"{key}={value}" in target_text
    assert "UNRELATED_SETTING=preserved" in target_text


def test_i2v_lora_profile_operation_is_single_flag_atomic_and_provider_read_only() -> None:
    workflow = _workflow()
    configurator = _i2v_profile_configurator()
    environment_program = configurator.split("<<'PY'\n", maxsplit=1)[1].split("\nPY\n", maxsplit=1)[
        0
    ]
    compile(environment_program, "configure-i2v-lora-profile-environment", "exec")

    assert "- i2v-lora-profile" in workflow
    profile_job = workflow.split("  i2v-lora-profile:\n", maxsplit=1)[1]
    assert "timeout-minutes: 120" in profile_job
    assert "role-duration-seconds: 6600" in profile_job
    assert '"executionTimeout": ["5400"]' in profile_job
    assert "--timeout-seconds 120" in profile_job
    assert "for _ in $(seq 1 1140)" in profile_job
    # A latest-possible enable failure includes the lock, bounded service and
    # Docker reads, initial readiness, both local environment programs, both
    # provider preflights (including TERM grace), the changed-profile restart,
    # and the complete automatic rollback deadline.
    worst_failure_recovery = (
        120
        + 20
        + 10
        + 10
        + 10
        + 60
        + 30
        + (600 + 15)
        + 10
        + 30
        + 10
        + 240
        + 10
        + 10
        + (600 + 15)
        + 330
    )
    execution_timeout = 5_400
    delivery_timeout = 120
    polling_timeout = 1_140 * 5
    oidc_timeout = 6_600
    workflow_timeout = 120 * 60
    assert worst_failure_recovery == 2_130 < 2_160
    assert 2_160 + 360 == 2_520 < execution_timeout
    assert execution_timeout + delivery_timeout < polling_timeout < oidc_timeout < workflow_timeout
    assert workflow_timeout <= 360 * 60
    assert "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e" in (workflow)
    assert "ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f" in (workflow)
    assert "be5802ffc52ee6bfa6c64a135dfdef37e4e0274e4098c9eb87e4edaafc4719a6" in workflow
    assert "68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301" in workflow
    assert "i2v_artifact_identity_sha256:" not in workflow
    for identity in (
        "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e",
        "ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f",
        "be5802ffc52ee6bfa6c64a135dfdef37e4e0274e4098c9eb87e4edaafc4719a6",
        "68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301",
    ):
        assert identity in configurator
    assert "profile-preflight" in configurator
    assert "--expected-public-profile" in configurator
    assert "verify_provider_and_queue false" in configurator
    assert configurator.index("verify_provider_and_queue false") < configurator.index(
        'environment_backup="$(mktemp'
    )
    assert "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED" in environment_program
    for protected_key in (
        "GEN_AUTOMATION_I2V_WORKER_IMAGE",
        "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION",
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON",
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256",
        "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256",
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
    ):
        assert f'set_profile("{protected_key}' not in environment_program
    assert "os.replace(temporary, path)" in environment_program
    assert "os.fsync" in environment_program
    assert "restore_previous_configuration" in configurator
    assert "Preserved the root-only rollback backup at $environment_backup" in configurator
    assert "rollback_restored=0" in configurator
    assert "wait_for_control_plane" in configurator
    assert "wait_for_control_plane_replacement" in configurator
    assert "restart_control_plane_requiring_replacement" in configurator
    assert 'systemctl restart --no-block "$service_name"' in configurator
    assert 'systemctl reset-failed "$service_name"' in configurator
    assert "control_plane_ready_deadline_seconds=60" in configurator
    assert "control_plane_restart_deadline_seconds=240" in configurator
    assert "rollback_deadline_seconds=330" in configurator
    assert "provider_preflight_timeout_seconds=600" in configurator
    assert "provider_preflight_kill_grace_seconds=15" in configurator
    assert "operation_timeout_seconds=2160" in configurator
    assert "operation_cleanup_grace_seconds=360" in configurator
    assert "monotonic_seconds" in configurator
    assert "</proc/uptime" in configurator
    assert "deadline_remaining" in configurator
    assert "bounded_timeout_before" in configurator
    assert "for _ in $(seq 1 90)" not in configurator
    assert '--connect-timeout "$probe_timeout" --max-time "$probe_timeout"' in configurator
    assert 'wait_for_control_plane_replacement "$previous_container_id" "$deadline"' in (
        configurator
    )
    assert '"$rollback_reference_container_id" "$rollback_deadline"' in configurator
    assert "a prior running control-plane container is required" not in configurator
    assert "Could not capture the container that rollback must replace." not in configurator
    assert "GEN_AUTOMATION_I2V_PROFILE_TIMEOUT_SUPERVISED" in configurator
    assert "trap 'exit 143' TERM" in configurator
    assert '[ "$replacement_container_id" != "$previous_container_id" ]' in configurator
    assert 'original_control_plane_container_id="$(control_plane_container_id)"' in configurator
    assert 'restart_from_control_plane_container_id="$(control_plane_container_id)"' in configurator
    assert 'verify_profile_readback "$expected_profile"' in configurator
    assert '"$original_profile" "$restored_container_id" "$rollback_deadline"' in configurator
    assert configurator.index('verify_profile_readback "$expected_profile"') < configurator.rindex(
        "rollback_armed=0"
    )
    assert "running public profile flag differs from the expected value" in configurator
    assert "Restored control-plane revision differs from the original revision." in configurator
    assert "create_job" not in configurator
    assert "cancel_job" not in configurator
    assert "update_container_group" not in configurator
    assert "start_container_group" not in configurator
    assert "stop_container_group" not in configurator


def test_i2v_lora_profile_workflow_transfers_only_checksum_pinned_helper() -> None:
    configurator = (DEPLOY / "configure-i2v-lora-profile.sh").read_bytes()
    checksum = hashlib.sha256(configurator).hexdigest()
    payload = base64.b64encode(gzip.compress(configurator, compresslevel=9, mtime=0)).decode(
        "ascii"
    )
    program = _i2v_profile_command_program()
    compile(program, "staging-i2v-lora-profile-ssm-command", "exec")
    common = {
        "SSM_SOURCE_REVISION": "a" * 40,
        "SSM_WORKER_IMAGE": ("ghcr.io/neuraln-cyber/gen-automation/i2v-worker@sha256:" + "b" * 64),
        "SSM_WORKER_SOURCE_REVISION": "c" * 40,
        "SSM_MANIFEST_SHA256": "f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e",
        "SSM_MODEL_MANIFEST_SHA256": (
            "ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f"
        ),
        "SSM_WORKER_MODEL_OBJECTS_SHA256": (
            "be5802ffc52ee6bfa6c64a135dfdef37e4e0274e4098c9eb87e4edaafc4719a6"
        ),
        "SSM_ARTIFACT_IDENTITY_SHA256": (
            "68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301"
        ),
        "SSM_SCRIPT_SHA256": checksum,
        "SSM_SCRIPT_PAYLOAD": payload,
    }
    commands: dict[str, str] = {}
    for operation in ("status", "enable", "disable"):
        environment = dict(common)
        environment["SSM_OPERATION"] = operation
        if operation == "disable":
            environment.update(
                {
                    "SSM_WORKER_IMAGE": "",
                    "SSM_WORKER_SOURCE_REVISION": "",
                }
            )
        result = subprocess.run(  # noqa: S603 - repository-owned command generator.
            [sys.executable, "-c", program],
            check=True,
            capture_output=True,
            env={**os.environ, **environment},
            text=True,
        )
        parameters = json.loads(result.stdout)
        command = parameters["commands"][0]
        assert parameters == {
            "commands": [command],
            "executionTimeout": ["5400"],
        }
        commands[operation] = command

    for command in commands.values():
        assert len(command.encode("utf-8")) <= 24_000
        assert payload in command
        assert checksum in command
        assert command.count("/usr/bin/gzip --decompress") == 1
        assert command.count("/usr/bin/sha256sum --check --status") == 1
        assert '/usr/bin/bash -n "$payload_root/configure-i2v-lora-profile.sh"' in command
    assert "--expected-worker-image" in commands["status"]
    assert "--expected-artifact-identity-sha256" in commands["enable"]
    assert "--expected-model-manifest-sha256" in commands["enable"]
    assert "--expected-worker-model-objects-sha256" in commands["enable"]
    assert "--expected-worker-image" not in commands["disable"]
    assert "--disable" in commands["disable"]
    for prohibited in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "SALAD_API_KEY"):
        assert prohibited not in "\n".join(commands.values())


def test_routine_rollout_refreshes_the_host_deployment_bundle_safely() -> None:
    workflow = _workflow()
    helper = (DEPLOY / "bootstrap-mega-profile.sh").read_bytes()
    compose = (DEPLOY / "compose.bootstrap.yaml").read_bytes()
    updater = (DEPLOY / "update-control-plane.sh").read_bytes()
    helper_sha256 = hashlib.sha256(helper).hexdigest()
    compose_sha256 = hashlib.sha256(compose).hexdigest()
    updater_sha256 = hashlib.sha256(updater).hexdigest()
    helper_payload = base64.b64encode(gzip.compress(helper, compresslevel=9, mtime=0)).decode(
        "ascii"
    )
    compose_payload = base64.b64encode(gzip.compress(compose, compresslevel=9, mtime=0)).decode(
        "ascii"
    )
    updater_payload = base64.b64encode(gzip.compress(updater, compresslevel=9, mtime=0)).decode(
        "ascii"
    )

    assert "repos/${GITHUB_REPOSITORY}/contents/${relative_path}?ref=${SOURCE_REVISION}" in workflow
    assert "infra/aws-staging/deploy/bootstrap-mega-profile.sh" in workflow
    assert "infra/aws-staging/deploy/compose.bootstrap.yaml" in workflow
    assert "infra/aws-staging/deploy/update-control-plane.sh" in workflow
    assert 'bash -n "$host_bundle_dir/bootstrap-mega-profile.sh"' in workflow
    assert 'bash -n "$host_bundle_dir/update-control-plane.sh"' in workflow
    assert (
        'gzip --best --no-name --stdout "$host_bundle_dir/bootstrap-mega-profile.sh"'
        " | base64 --wrap=0"
    ) in workflow
    assert (
        'gzip --best --no-name --stdout "$host_bundle_dir/compose.bootstrap.yaml" | base64 --wrap=0'
    ) in workflow
    assert (
        'gzip --best --no-name --stdout "$host_bundle_dir/update-control-plane.sh"'
        " | base64 --wrap=0"
    ) in workflow

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
        "SSM_BOOTSTRAP_HELPER_SHA256": helper_sha256,
        "SSM_BOOTSTRAP_COMPOSE_SHA256": compose_sha256,
        "SSM_CONTROL_PLANE_UPDATER_SHA256": updater_sha256,
        "SSM_BOOTSTRAP_HELPER_GZIP_BASE64": helper_payload,
        "SSM_BOOTSTRAP_COMPOSE_GZIP_BASE64": compose_payload,
        "SSM_CONTROL_PLANE_UPDATER_GZIP_BASE64": updater_payload,
    }
    result = subprocess.run(  # noqa: S603 - executes the repository-owned rollout fixture.
        [sys.executable, "-c", program],
        check=True,
        capture_output=True,
        env={**os.environ, **environment},
        text=True,
    )
    command = json.loads(result.stdout)["commands"][0]
    bash = shutil.which("bash")
    if bash is None:
        git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
        bash = str(git_bash) if git_bash.exists() else None
    if bash is not None:
        syntax = subprocess.run(  # noqa: S603 - syntax-checks a repository-owned command only.
            [bash, "-n"],
            check=False,
            capture_output=True,
            input=command,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stderr

    assert len(command.encode("utf-8")) <= 24_000
    assert "if command_size > 24_000:" in program
    assert helper_payload in command
    assert compose_payload in command
    assert updater_payload in command
    assert helper_sha256 in command
    assert compose_sha256 in command
    assert updater_sha256 in command
    assert "/usr/bin/base64 --decode" in command
    assert command.count("/usr/bin/gzip --decompress") == 3
    assert command.count("/usr/bin/sha256sum --check --status") == 4
    assert '/usr/bin/bash -n "$bundle_root/bootstrap-mega-profile.sh"' in command
    assert '/usr/bin/bash -n "$bundle_root/update-control-plane.sh"' in command
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
    updater_install = (
        "sudo /usr/bin/install -o root -g root -m 0755 "
        '"$bundle_root/update-control-plane.sh" '
        "/usr/local/sbin/gen-automation-update-control-plane"
    )
    assert updater_install in command
    post_updater_install = command.split(updater_install, maxsplit=1)[1]
    assert updater_sha256 in post_updater_install
    assert "/usr/local/sbin/gen-automation-update-control-plane" in post_updater_install
    assert command.count(updater_sha256) == 2
    assert "raw.githubusercontent.com" not in command
    assert "/usr/bin/curl" not in command
    assert command.index("--profile bootstrap config --quiet") < command.index(
        "/opt/gen-automation/deploy/compose.bootstrap.yaml"
    )
    assert command.index("/usr/local/sbin/gen-automation-bootstrap-mega-profile") < command.index(
        "/etc/gen-automation/control-plane.env"
    )
    updater_install_index = command.index(updater_install)
    assert updater_install_index < command.index(
        "sudo /usr/bin/systemctl stop gen-automation-staging.service"
    )
    assert updater_install_index < command.index("python3.12 -m alembic upgrade head")
    assert updater_install_index < command.index(
        "sudo /usr/local/sbin/gen-automation-update-control-plane --image"
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
