#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
validator="/usr/local/libexec/gen-automation-validate-deployment"
installed_helper="/usr/local/libexec/gen-automation-configure-lora-manager"
service_name="gen-automation-staging.service"
lock_file="/run/lock/gen-automation-control-plane-update.lock"

operation=""
secret_arn=""
expected_revision=""
validator_source=""
external_lock_held=0
environment_backup=""
validator_backup=""
helper_temporary=""
validator_replaced=0
rollback_armed=0
service_was_active=0

fail() {
  printf '%s\n' "LoRA manager configuration failed: $*" >&2
  return 1
}

require_private_root_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing $path"
  [ "$(stat -c '%U:%G' "$path")" = "root:root" ] ||
    fail "$path must be owned by root:root"
  case "$(stat -c '%a' "$path")" in
    400|600) ;;
    *) fail "$path must have mode 0400 or 0600" ;;
  esac
}

wait_for_control_plane() {
  for _ in $(seq 1 60); do
    if systemctl is-active --quiet "$service_name" &&
      curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

control_plane_revision() {
  local container_id
  local revision
  container_id="$(
    /usr/bin/docker compose \
      --env-file "$deploy_env" \
      --file "$compose_file" \
      ps --status running --quiet control-plane-mega 2>/dev/null || true
  )"
  [ -n "$container_id" ] || fail "running control-plane container was not found"
  [ "$(printf '%s\n' "$container_id" | sed '/^$/d' | wc -l)" -eq 1 ] ||
    fail "expected exactly one running control-plane container"
  revision="$(
    /usr/bin/docker inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$container_id"
  )"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "running control-plane image has no valid source revision"
  printf '%s' "$revision"
}

restore_previous_configuration() {
  local rollback_failed=0
  set +e
  printf '%s\n' "LoRA manager change failed; restoring the previous host configuration." >&2
  if [ -n "$environment_backup" ] && [ -f "$environment_backup" ]; then
    install -o root -g root -m 0600 "$environment_backup" "$controller_env" ||
      rollback_failed=1
  else
    rollback_failed=1
  fi
  if [ "$validator_replaced" -eq 1 ]; then
    if [ -n "$validator_backup" ] && [ -f "$validator_backup" ]; then
      install -o root -g root -m 0755 "$validator_backup" "$validator" ||
        rollback_failed=1
    else
      rollback_failed=1
    fi
  fi
  if [ "$service_was_active" -eq 1 ]; then
    systemctl restart --no-block "$service_name" || rollback_failed=1
    wait_for_control_plane || rollback_failed=1
  else
    systemctl stop "$service_name" || rollback_failed=1
  fi
  [ "$rollback_failed" -eq 0 ] ||
    printf '%s\n' "Automatic LoRA manager rollback needs operator attention." >&2
  return "$rollback_failed"
}

cleanup() {
  local status=$?
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_configuration || true
  fi
  [ -z "$environment_backup" ] || rm -f -- "$environment_backup"
  [ -z "$validator_backup" ] || rm -f -- "$validator_backup"
  [ -z "$helper_temporary" ] || rm -f -- "$helper_temporary"
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
[ "$#" -ge 1 ] ||
  fail "usage: $0 --status | --enable --civitai-secret-arn <exact-arn> --expected-control-plane-revision <40-hex> | --disable"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status|--enable|--disable)
      [ -z "$operation" ] || fail "choose exactly one operation"
      operation="${1#--}"
      shift
      ;;
    --civitai-secret-arn)
      [ "$#" -ge 2 ] || fail "--civitai-secret-arn requires a value"
      secret_arn="$2"
      shift 2
      ;;
    --validator)
      [ "$#" -ge 2 ] || fail "--validator requires a path"
      validator_source="$2"
      shift 2
      ;;
    --expected-control-plane-revision)
      [ "$#" -ge 2 ] || fail "--expected-control-plane-revision requires a value"
      expected_revision="$2"
      shift 2
      ;;
    --external-lock-held)
      external_lock_held=1
      shift
      ;;
    *) fail "unknown argument" ;;
  esac
done

case "$operation" in
  enable)
    [[ "$secret_arn" =~ ^arn:aws:secretsmanager:eu-central-1:861912887470:secret:gen-automation/staging/civitai-[A-Za-z0-9]{6}$ ]] ||
      fail "Civitai reference must be the exact configured staging secret ARN"
    [[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] ||
      fail "enable requires the exact 40-hex deployed control-plane revision"
    ;;
  disable|status)
    [ -z "$secret_arn" ] || fail "$operation does not accept a Civitai secret ARN"
    [ -z "$expected_revision" ] ||
      fail "$operation does not accept an expected control-plane revision"
    ;;
  *) fail "choose exactly one of --status, --enable, or --disable" ;;
esac
require_private_root_file "$controller_env"
[ -x /usr/bin/python3 ] || fail "/usr/bin/python3 is required"

environment_program=$(cat <<'PY'
import json
import os
import pathlib
import re
import sys
import tempfile


def fail(message: str) -> None:
    raise SystemExit(f"LoRA manager configuration failed: {message}")


path = pathlib.Path(sys.argv[1])
operation = sys.argv[2]
secret_arn = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()


def assignment_indexes(key: str) -> list[int]:
    return [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]


def optional_value(key: str):
    indexes = assignment_indexes(key)
    if len(indexes) > 1:
        fail(f"{path} defines {key} more than once")
    return lines[indexes[0]].split("=", maxsplit=1)[1] if indexes else None


def required_value(key: str) -> str:
    value = optional_value(key)
    if value is None:
        fail(f"{path} must define {key} exactly once")
    if not value:
        fail(f"{key} is required before LoRA manager enablement")
    return value


def upsert(key: str, value: str) -> None:
    indexes = assignment_indexes(key)
    if len(indexes) > 1:
        fail(f"{path} defines {key} more than once")
    replacement = f"{key}={value}"
    if indexes:
        lines[indexes[0]] = replacement
    else:
        lines.append(replacement)


if operation == "status":
    enabled = optional_value("GEN_AUTOMATION_LORA_MANAGER_ENABLED") == "true"
    reference = optional_value("GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE") or ""
    anchor = optional_value("GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256") or ""
    print(f"lora_manager_enabled={str(enabled).lower()}")
    print(f"civitai_secret_reference_configured={str(bool(reference)).lower()}")
    print(
        "worker_manifest_trust_anchor_configured="
        f"{str(re.fullmatch(r'[0-9a-f]{64}', anchor) is not None).lower()}"
    )
    raise SystemExit(0)

if operation == "enable":
    if required_value("GEN_AUTOMATION_ENVIRONMENT") != "staging":
        fail("the permanent LoRA manager rollout is staging-only")
    if required_value("GEN_AUTOMATION_BACKGROUND_RUNTIME_ENABLED") != "true":
        fail("the background runtime must be enabled")
    required_value("GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_BUCKET")
    if required_value("GEN_AUTOMATION_SALAD_WORKER_ARTIFACT_REGION") != "eu-central-1":
        fail("the worker artifact region must remain eu-central-1")

    manifest_json = required_value("GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_JSON")
    manifest_sha256 = required_value("GEN_AUTOMATION_SALAD_WORKER_MODEL_MANIFEST_SHA256")
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        fail("the worker model manifest trust anchor must be 64 lowercase hex characters")
    try:
        manifest = json.loads(manifest_json)
    except (TypeError, ValueError):
        fail("the worker model manifest must be valid JSON")
    if not isinstance(manifest, dict) or manifest.get("manifest_sha256") != manifest_sha256:
        fail("the worker model manifest does not match its independent trust anchor")

    api_key = optional_value("GEN_AUTOMATION_CIVITAI_API_KEY")
    if api_key:
        fail("a direct Civitai API key is forbidden in staging")
    upsert("GEN_AUTOMATION_LORA_MANAGER_ENABLED", "true")
    upsert(
        "GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE",
        f"aws-secrets-manager://{secret_arn}",
    )
elif operation == "disable":
    upsert("GEN_AUTOMATION_LORA_MANAGER_ENABLED", "false")
    if optional_value("GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE") is None:
        upsert("GEN_AUTOMATION_CIVITAI_API_SECRET_REFERENCE", "")
else:
    fail("unsupported operation")

rendered = "\n".join(lines) + "\n"
if rendered == path.read_text(encoding="utf-8"):
    raise SystemExit(0)

descriptor, temporary_name = tempfile.mkstemp(
    prefix=".control-plane.env.lora.update.",
    dir=path.parent,
)
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(rendered)
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    if os.name != "nt":
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
finally:
    temporary.unlink(missing_ok=True)
PY
)

if [ "$operation" = "status" ]; then
  /usr/bin/python3 -c "$environment_program" "$controller_env" status ""
  exit 0
fi

command -v flock >/dev/null || fail "flock is required"
if [ "$external_lock_held" -eq 0 ]; then
  exec 9>"$lock_file"
  flock --exclusive --wait 120 9 || fail "another control-plane update holds the deployment lock"
fi

if [ "$operation" = "enable" ]; then
  [ -f "$deploy_env" ] || fail "missing $deploy_env"
  [ -f "$compose_file" ] || fail "missing $compose_file"
  [ -x /usr/bin/docker ] || fail "/usr/bin/docker is required"
  systemctl is-active --quiet "$service_name" ||
    fail "enablement requires the existing control plane to be active"
  wait_for_control_plane || fail "enablement requires a healthy baseline control plane"
  actual_revision="$(control_plane_revision)"
  [ "$actual_revision" = "$expected_revision" ] ||
    fail "running control-plane revision $actual_revision does not match $expected_revision"
fi
if systemctl is-active --quiet "$service_name"; then
  service_was_active=1
fi

if [ -z "$validator_source" ]; then
  validator_source="$validator"
fi
[ -f "$validator_source" ] || fail "missing deployment validator"
/usr/bin/bash -n "$validator_source" || fail "deployment validator has invalid Bash syntax"

environment_backup="$(mktemp "$config_root/.control-plane.env.lora.rollback.XXXXXX")"
install -o root -g root -m 0600 "$controller_env" "$environment_backup"
if [ "$(readlink -f "$validator_source")" != "$(readlink -f "$validator")" ]; then
  [ -f "$validator" ] || fail "installed deployment validator is missing"
  validator_backup="$(mktemp /usr/local/libexec/.gen-automation-validate-deployment.lora.rollback.XXXXXX)"
  install -o root -g root -m 0755 "$validator" "$validator_backup"
  validator_replaced=1
fi
rollback_armed=1

systemctl stop "$service_name"
! systemctl is-active --quiet "$service_name" || fail "control plane did not stop"
/usr/bin/python3 -c "$environment_program" "$controller_env" "$operation" "$secret_arn"
if [ "$validator_replaced" -eq 1 ]; then
  install -o root -g root -m 0755 "$validator_source" "$validator"
fi
"$validator"

if [ "$service_was_active" -eq 1 ] || [ "$operation" = "enable" ]; then
  systemctl restart --no-block "$service_name"
  wait_for_control_plane || fail "updated control plane did not become ready"
fi

if [ "$(readlink -f "$0")" != "$(readlink -f "$installed_helper")" ]; then
  helper_temporary="$(mktemp /usr/local/libexec/.gen-automation-configure-lora-manager.update.XXXXXX)"
  install -o root -g root -m 0755 "$0" "$helper_temporary"
  mv -f -- "$helper_temporary" "$installed_helper"
  helper_temporary=""
fi
rollback_armed=0
rm -f -- "$environment_backup"
environment_backup=""
rm -f -- "$validator_backup"
validator_backup=""
printf '%s\n' "LoRA manager $operation completed and passed deployment validation."
