#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
service_name="gen-automation-staging.service"
lock_file="/run/lock/gen-automation-control-plane-update.lock"
reviewed_private_manifest_source_sha256="f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e"
reviewed_model_manifest_sha256="ebdeca736ee3e9ea4e4b7118c9e4b54dfcfd1bbde5a761f424aa85b1670b806f"
reviewed_worker_model_objects_sha256="4ff59362992c7284e2e24fcb7d3ce2c61b6d662f074123777bc621971f33a8fc"
reviewed_artifact_identity_sha256="68f6c28831ac2a8e1801ba420c9816a29e09c8cc4738aae85611955553a3d301"
control_plane_ready_deadline_seconds=60
control_plane_restart_deadline_seconds=240
rollback_deadline_seconds=330
health_probe_timeout_seconds=3
poll_interval_seconds=2
docker_command_timeout_seconds=10
service_command_timeout_seconds=20
local_file_command_timeout_seconds=10
environment_program_timeout_seconds=30
provider_preflight_timeout_seconds=600
provider_preflight_kill_grace_seconds=15
operation_timeout_seconds=2160
operation_cleanup_grace_seconds=360

# The first process is only a hard wall-clock supervisor. The supervised shell
# retains the EXIT trap and therefore has a full rollback deadline after TERM;
# KILL makes the absolute operation bound 2160 + 360 = 2520 seconds.
# The conservative latest enable failure is 2130 seconds: lock 120, bounded
# pre-change reads/readiness 110, environment checks 60, provider preflights
# 2*(600+15), backup/current-container reads 20, changed restart/readback 260,
# and rollback 330. The 2160-second TERM leaves 30 seconds of local slack.
if [ "${GEN_AUTOMATION_I2V_PROFILE_TIMEOUT_SUPERVISED:-false}" != "true" ]; then
  [ -x /usr/bin/timeout ] || {
    printf '%s\n' "I2V LoRA profile operation failed: /usr/bin/timeout is required" >&2
    exit 1
  }
  export GEN_AUTOMATION_I2V_PROFILE_TIMEOUT_SUPERVISED=true
  exec /usr/bin/timeout --signal=TERM \
    --kill-after="${operation_cleanup_grace_seconds}s" \
    "${operation_timeout_seconds}s" /usr/bin/bash "$0" "$@"
fi

operation=""
expected_revision=""
expected_worker_image=""
expected_worker_source_revision=""
expected_private_manifest_source_sha256=""
expected_model_manifest_sha256=""
expected_worker_model_objects_sha256=""
expected_artifact_identity_sha256=""
environment_backup=""
rollback_armed=0
service_was_active=0
original_profile=""
original_control_plane_container_id=""
original_control_plane_revision=""

fail() {
  printf '%s\n' "I2V LoRA profile operation failed: $*" >&2
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

monotonic_seconds() {
  local uptime ignored
  if ! read -r uptime ignored </proc/uptime; then
    fail "could not read the monotonic system clock"
    return 1
  fi
  uptime="${uptime%%.*}"
  if [[ ! "$uptime" =~ ^[0-9]+$ ]]; then
    fail "monotonic system clock has an invalid value"
    return 1
  fi
  printf '%s' "$uptime"
}

deadline_after() {
  local duration="$1" now
  if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
    fail "deadline duration must be a positive integer"
    return 1
  fi
  now="$(monotonic_seconds)" || return 1
  printf '%s' "$((now + duration))"
}

deadline_remaining() {
  local deadline="$1" now
  now="$(monotonic_seconds)" || return 1
  if [ "$now" -ge "$deadline" ]; then
    return 1
  fi
  printf '%s' "$((deadline - now))"
}

bounded_timeout_before() {
  local deadline="$1" maximum="$2" remaining
  remaining="$(deadline_remaining "$deadline")" || return 1
  if [ "$remaining" -gt "$maximum" ]; then
    remaining="$maximum"
  fi
  printf '%s' "$remaining"
}

sleep_before_deadline() {
  local deadline="$1" duration
  duration="$(bounded_timeout_before "$deadline" "$poll_interval_seconds")" || return 1
  sleep "$duration"
}

control_plane_ready_before() {
  local deadline="$1" command_timeout probe_timeout
  command_timeout="$(
    bounded_timeout_before "$deadline" "$service_command_timeout_seconds"
  )" || return 1
  /usr/bin/timeout --signal=KILL "${command_timeout}s" \
    systemctl is-active --quiet "$service_name" || return 1
  probe_timeout="$(
    bounded_timeout_before "$deadline" "$health_probe_timeout_seconds"
  )" || return 1
  /usr/bin/curl --fail --silent --show-error \
    --connect-timeout "$probe_timeout" --max-time "$probe_timeout" \
    http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1 || return 1
  deadline_remaining "$deadline" >/dev/null
}

control_plane_service_state() {
  local status=0
  /usr/bin/timeout --signal=KILL "${service_command_timeout_seconds}s" \
    systemctl is-active --quiet "$service_name" || status=$?
  case "$status" in
    0) printf '%s' "active" ;;
    3) printf '%s' "inactive" ;;
    *)
      fail "could not determine control-plane service state"
      return 1
      ;;
  esac
}

wait_for_control_plane() {
  local deadline
  deadline="$(deadline_after "$control_plane_ready_deadline_seconds")" || return 1
  while deadline_remaining "$deadline" >/dev/null; do
    if control_plane_ready_before "$deadline"; then
      return 0
    fi
    sleep_before_deadline "$deadline" || break
  done
  return 1
}

try_control_plane_container_id() {
  local command_timeout="${1:-$docker_command_timeout_seconds}" container_id container_count
  [[ "$command_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
  container_id="$(
    /usr/bin/timeout --signal=KILL "${command_timeout}s" /usr/bin/docker compose \
      --env-file "$deploy_env" \
      --file "$compose_file" \
      ps --status running --quiet control-plane-mega 2>/dev/null
  )" || return 1
  container_count="$(printf '%s\n' "$container_id" | sed '/^$/d' | wc -l)"
  [ -n "$container_id" ] && [ "$container_count" -eq 1 ] || return 1
  printf '%s' "$container_id"
}

control_plane_container_id() {
  local container_id
  if ! container_id="$(try_control_plane_container_id)"; then
    fail "expected exactly one running control-plane container"
    return 1
  fi
  printf '%s' "$container_id"
}

control_plane_revision() {
  local container_id="${1:-}" command_timeout="${2:-$docker_command_timeout_seconds}" revision
  if [ -z "$container_id" ]; then
    container_id="$(control_plane_container_id)"
  fi
  [[ "$command_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
  if ! revision="$(
    /usr/bin/timeout --signal=KILL "${command_timeout}s" /usr/bin/docker inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$container_id"
  )"; then
    fail "could not inspect the running control-plane revision"
    return 1
  fi
  if [[ ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    fail "running control-plane image has no valid source revision"
    return 1
  fi
  printf '%s' "$revision"
}

wait_for_control_plane_replacement() {
  local previous_container_id="$1" deadline="$2" command_timeout replacement_container_id
  while deadline_remaining "$deadline" >/dev/null; do
    command_timeout="$(
      bounded_timeout_before "$deadline" "$docker_command_timeout_seconds"
    )" || break
    replacement_container_id="$(try_control_plane_container_id "$command_timeout" || true)"
    if [ -n "$replacement_container_id" ] &&
      [ "$replacement_container_id" != "$previous_container_id" ] &&
      control_plane_ready_before "$deadline"; then
      printf '%s' "$replacement_container_id"
      return 0
    fi
    sleep_before_deadline "$deadline" || break
  done
  return 1
}

restart_control_plane_requiring_replacement() {
  local previous_container_id="$1" deadline="${2:-}" command_timeout replacement_container_id
  if [ -z "$deadline" ]; then
    deadline="$(deadline_after "$control_plane_restart_deadline_seconds")" || return 1
  fi
  command_timeout="$(
    bounded_timeout_before "$deadline" "$service_command_timeout_seconds"
  )" || return 1
  /usr/bin/timeout --signal=KILL "${command_timeout}s" \
    systemctl reset-failed "$service_name" >/dev/null 2>&1 || true
  command_timeout="$(
    bounded_timeout_before "$deadline" "$service_command_timeout_seconds"
  )" || return 1
  if ! /usr/bin/timeout --signal=KILL "${command_timeout}s" \
    systemctl restart --no-block "$service_name"; then
    fail "control-plane service restart failed"
    return 1
  fi
  if ! replacement_container_id="$(
    wait_for_control_plane_replacement "$previous_container_id" "$deadline"
  )"; then
    fail "control-plane restart did not produce a distinct ready container"
    return 1
  fi
  printf '%s' "$replacement_container_id"
}

profile_value_from_file() {
  local path="$1" matches match_count profile_value
  matches="$(grep '^GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED=' "$path" || true)"
  match_count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l)"
  if [ "$match_count" -ne 1 ]; then
    fail "$path must define GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED exactly once"
    return 1
  fi
  profile_value="${matches#*=}"
  case "$profile_value" in
    true|false) ;;
    *)
      fail "$path has an invalid public profile flag"
      return 1
      ;;
  esac
  printf '%s' "$profile_value"
}

profile_value_from_container() {
  local container_id="$1" command_timeout="${2:-$docker_command_timeout_seconds}"
  local environment matches match_count profile_value
  [[ "$command_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
  if ! environment="$(
    /usr/bin/timeout --signal=KILL "${command_timeout}s" /usr/bin/docker inspect \
      --format '{{range .Config.Env}}{{println .}}{{end}}' \
      "$container_id"
  )"; then
    fail "could not inspect the running control-plane environment"
    return 1
  fi
  matches="$(
    printf '%s\n' "$environment" |
      grep '^GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED=' || true
  )"
  match_count="$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l)"
  if [ "$match_count" -ne 1 ]; then
    fail "running control plane must define the public profile flag exactly once"
    return 1
  fi
  profile_value="${matches#*=}"
  case "$profile_value" in
    true|false) ;;
    *)
      fail "running control plane has an invalid public profile flag"
      return 1
      ;;
  esac
  printf '%s' "$profile_value"
}

verify_profile_readback() {
  local expected_profile="$1" container_id="$2" deadline="${3:-}"
  local command_timeout="$docker_command_timeout_seconds" host_profile running_profile
  if [ "$expected_profile" != "true" ] && [ "$expected_profile" != "false" ]; then
    fail "expected profile readback must be true or false"
    return 1
  fi
  if ! host_profile="$(profile_value_from_file "$controller_env")"; then
    return 1
  fi
  if [ "$host_profile" != "$expected_profile" ]; then
    fail "host public profile flag differs from the expected value"
    return 1
  fi
  if [ -n "$deadline" ]; then
    command_timeout="$(
      bounded_timeout_before "$deadline" "$docker_command_timeout_seconds"
    )" || return 1
  fi
  if ! running_profile="$(
    profile_value_from_container "$container_id" "$command_timeout"
  )"; then
    return 1
  fi
  if [ "$running_profile" != "$expected_profile" ]; then
    fail "running public profile flag differs from the expected value"
    return 1
  fi
}

restore_previous_configuration() {
  local rollback_failed=0 rollback_deadline command_timeout
  local rollback_reference_container_id="" restored_container_id="" restored_revision=""
  printf '%s\n' "Profile change failed; restoring the previous host environment." >&2
  rollback_deadline="$(deadline_after "$rollback_deadline_seconds")" || return 1
  if [ -n "$environment_backup" ] && [ -f "$environment_backup" ]; then
    command_timeout="$(
      bounded_timeout_before "$rollback_deadline" "$local_file_command_timeout_seconds"
    )" || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ] &&
      ! /usr/bin/timeout --signal=KILL "${command_timeout}s" \
        /usr/bin/install -o root -g root -m 0600 \
          "$environment_backup" "$controller_env"; then
      rollback_failed=1
    fi
  else
    rollback_failed=1
  fi
  if [ "$service_was_active" -eq 1 ] && [ "$rollback_failed" -eq 0 ]; then
    command_timeout="$(
      bounded_timeout_before "$rollback_deadline" "$docker_command_timeout_seconds"
    )" || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ]; then
      rollback_reference_container_id="$(
        try_control_plane_container_id "$command_timeout" || true
      )"
    fi
    # A failed changed-profile restart may have removed the old container.
    # Empty means the restored service may satisfy recovery with any exact,
    # newly ready container; nonempty still requires a distinct replacement.
    if [ "$rollback_failed" -eq 0 ] && restored_container_id="$(
      restart_control_plane_requiring_replacement \
        "$rollback_reference_container_id" "$rollback_deadline"
    )"; then
      if ! verify_profile_readback \
        "$original_profile" "$restored_container_id" "$rollback_deadline"; then
        rollback_failed=1
      fi
      command_timeout="$(
        bounded_timeout_before "$rollback_deadline" "$docker_command_timeout_seconds"
      )" || rollback_failed=1
      if [ "$rollback_failed" -eq 0 ]; then
        restored_revision="$(
          control_plane_revision "$restored_container_id" "$command_timeout" || true
        )"
      fi
      if [ "$restored_revision" != "$original_control_plane_revision" ]; then
        printf '%s\n' "Restored control-plane revision differs from the original revision." >&2
        rollback_failed=1
      fi
    elif [ "$rollback_failed" -eq 0 ]; then
      rollback_failed=1
    fi
  elif [ "$service_was_active" -eq 0 ]; then
    command_timeout="$(
      bounded_timeout_before "$rollback_deadline" "$service_command_timeout_seconds"
    )" || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ] &&
      ! /usr/bin/timeout --signal=KILL "${command_timeout}s" \
        systemctl stop "$service_name"; then
      rollback_failed=1
    fi
    if [ "$(profile_value_from_file "$controller_env" || true)" != "$original_profile" ]; then
      rollback_failed=1
    fi
  fi
  [ "$rollback_failed" -eq 0 ] ||
    printf '%s\n' "Automatic profile rollback needs operator attention." >&2
  return "$rollback_failed"
}

cleanup() {
  local status=$?
  local rollback_restored=1
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_configuration || rollback_restored=0
  fi
  if [ "$rollback_restored" -eq 1 ]; then
    [ -z "$environment_backup" ] || rm -f -- "$environment_backup"
  elif [ -n "$environment_backup" ]; then
    printf '%s\n' "Preserved the root-only rollback backup at $environment_backup" >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 143' TERM

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
[ "$#" -ge 1 ] || fail "choose --status, --enable, or --disable"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status|--enable|--disable)
      [ -z "$operation" ] || fail "choose exactly one operation"
      operation="${1#--}"
      shift
      ;;
    --expected-control-plane-revision)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_revision="$2"
      shift 2
      ;;
    --expected-worker-image)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_worker_image="$2"
      shift 2
      ;;
    --expected-worker-source-revision)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_worker_source_revision="$2"
      shift 2
      ;;
    --expected-private-manifest-sha256)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_private_manifest_source_sha256="$2"
      shift 2
      ;;
    --expected-model-manifest-sha256)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_model_manifest_sha256="$2"
      shift 2
      ;;
    --expected-worker-model-objects-sha256)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_worker_model_objects_sha256="$2"
      shift 2
      ;;
    --expected-artifact-identity-sha256)
      [ "$#" -ge 2 ] || fail "$1 requires a value"
      expected_artifact_identity_sha256="$2"
      shift 2
      ;;
    *) fail "unknown argument" ;;
  esac
done

case "$operation" in
  status|enable)
    [[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] ||
      fail "$operation requires the exact control-plane revision"
    [[ "$expected_worker_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/i2v-worker@sha256:[0-9a-f]{64}$ ]] ||
      fail "$operation requires the exact immutable I2V worker image"
    [[ "$expected_worker_source_revision" =~ ^[0-9a-f]{40}$ ]] ||
      fail "$operation requires the exact worker source revision"
    [[ "$expected_private_manifest_source_sha256" =~ ^[0-9a-f]{64}$ ]] ||
      fail "$operation requires the exact private source manifest SHA-256"
    [[ "$expected_model_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] ||
      fail "$operation requires the exact canonical model manifest SHA-256"
    [[ "$expected_worker_model_objects_sha256" =~ ^[0-9a-f]{64}$ ]] ||
      fail "$operation requires the exact worker model-objects SHA-256"
    [[ "$expected_artifact_identity_sha256" =~ ^[0-9a-f]{64}$ ]] ||
      fail "$operation requires the exact artifact identity SHA-256"
    [ "$expected_private_manifest_source_sha256" = "$reviewed_private_manifest_source_sha256" ] ||
      fail "$operation requires the reviewed private source manifest"
    [ "$expected_model_manifest_sha256" = "$reviewed_model_manifest_sha256" ] ||
      fail "$operation requires the reviewed canonical model manifest"
    [ "$expected_worker_model_objects_sha256" = "$reviewed_worker_model_objects_sha256" ] ||
      fail "$operation requires the reviewed worker model objects"
    [ "$expected_artifact_identity_sha256" = "$reviewed_artifact_identity_sha256" ] ||
      fail "$operation requires the reviewed artifact identity"
    ;;
  disable)
    [ -z "$expected_revision$expected_worker_image$expected_worker_source_revision$expected_private_manifest_source_sha256$expected_model_manifest_sha256$expected_worker_model_objects_sha256$expected_artifact_identity_sha256" ] ||
      fail "disable accepts no rollout coordinates"
    ;;
  *) fail "choose exactly one of --status, --enable, or --disable" ;;
esac

require_private_root_file "$controller_env"
[ -f "$deploy_env" ] || fail "missing $deploy_env"
[ -f "$compose_file" ] || fail "missing $compose_file"
[ -x /usr/bin/docker ] || fail "/usr/bin/docker is required"
[ -x /usr/bin/python3 ] || fail "/usr/bin/python3 is required"
[ -x /usr/bin/curl ] || fail "/usr/bin/curl is required"
[ -x /usr/bin/install ] || fail "/usr/bin/install is required"
[ -x /usr/bin/timeout ] || fail "/usr/bin/timeout is required"
[ -r /proc/uptime ] || fail "/proc/uptime is required for monotonic deadlines"

environment_program=$(cat <<'PY'
import hashlib
import json
import os
import pathlib
import sys
import tempfile


def fail(message: str) -> None:
    raise SystemExit(f"I2V LoRA profile operation failed: {message}")


path = pathlib.Path(sys.argv[1])
operation = sys.argv[2]
expected_image = sys.argv[3]
expected_source_revision = sys.argv[4]
expected_private_manifest_source_sha256 = sys.argv[5]
expected_model_manifest_sha256 = sys.argv[6]
lines = path.read_text(encoding="utf-8").splitlines()


def value(key: str, *, required: bool = True) -> str:
    matches = [line.split("=", maxsplit=1)[1] for line in lines if line.startswith(f"{key}=")]
    if len(matches) > 1 or (required and len(matches) != 1):
        fail(f"{path} must define {key} exactly once")
    return matches[0] if matches else ""


def require_exact(key: str, expected: str) -> None:
    if value(key) != expected:
        fail(f"{key} does not match the expected rollout identity")


def set_profile(enabled: bool) -> None:
    key = "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED"
    matches = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
    if len(matches) != 1:
        fail(f"{path} must define {key} exactly once")
    lines[matches[0]] = f"{key}={'true' if enabled else 'false'}"


if operation in {"status", "enable"}:
    require_exact("GEN_AUTOMATION_ENVIRONMENT", "staging")
    require_exact("GEN_AUTOMATION_I2V_ENABLED", "true")
    require_exact("GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED", "true")
    require_exact("GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED", "true")
    require_exact("GEN_AUTOMATION_I2V_WORKER_IMAGE", expected_image)
    require_exact(
        "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION",
        expected_source_revision,
    )
    manifest_json = value("GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON")
    manifest_sha256 = value("GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256")
    private_manifest_source_sha256 = value(
        "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256"
    )
    if private_manifest_source_sha256 != expected_private_manifest_source_sha256:
        fail("private source manifest SHA-256 does not match the rollout identity")
    if manifest_sha256 != expected_model_manifest_sha256:
        fail("canonical model manifest SHA-256 does not match the rollout identity")
    if hashlib.sha256(manifest_json.encode("utf-8")).hexdigest() != manifest_sha256:
        fail("private manifest JSON does not match its SHA-256")
    try:
        parsed = json.loads(manifest_json)
    except (TypeError, ValueError):
        fail("private manifest JSON is invalid")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("objects"), list):
        fail("private manifest JSON has the wrong schema")

if operation == "status":
    print(f"i2v_lora_profile_enabled={value('GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED')}")
    print("host_rollout_identity=verified")
    raise SystemExit(0)

set_profile(operation == "enable")
rendered = "\n".join(lines) + "\n"
if rendered == path.read_text(encoding="utf-8"):
    raise SystemExit(0)
descriptor, temporary_name = tempfile.mkstemp(prefix=".control-plane.env.i2v-profile.", dir=path.parent)
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(rendered)
        output.flush()
        os.fsync(output.fileno())
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
finally:
    temporary.unlink(missing_ok=True)
PY
)

verify_provider_and_queue() {
  local expected_profile="$1"
  /usr/bin/timeout --signal=TERM \
    --kill-after="${provider_preflight_kill_grace_seconds}s" \
    "${provider_preflight_timeout_seconds}s" /usr/bin/docker compose \
    --env-file "$deploy_env" \
    --file "$compose_file" \
    exec -T control-plane-mega \
    python3.12 -m gen_automation.i2v_lora_rollout_cli profile-preflight \
      --expected-worker-image "$expected_worker_image" \
      --expected-worker-source-revision "$expected_worker_source_revision" \
      --expected-public-profile "$expected_profile"
}

if [ "$operation" = "status" ]; then
  [ "$(control_plane_service_state)" = "active" ] || fail "control plane is not active"
  wait_for_control_plane || fail "control plane is not ready"
  status_container_id="$(control_plane_container_id)"
  [ "$(control_plane_revision "$status_container_id")" = "$expected_revision" ] ||
    fail "running control-plane revision differs from the expected revision"
  /usr/bin/timeout --signal=KILL "${environment_program_timeout_seconds}s" \
    /usr/bin/python3 -c "$environment_program" \
    "$controller_env" status "$expected_worker_image" \
    "$expected_worker_source_revision" "$expected_private_manifest_source_sha256" \
    "$expected_model_manifest_sha256"
  configured_profile="$(profile_value_from_file "$controller_env")"
  verify_profile_readback "$configured_profile" "$status_container_id"
  verify_provider_and_queue "$configured_profile"
  printf '%s\n' "I2V LoRA profile status passed exact provider and queue verification."
  exit 0
fi

command -v flock >/dev/null || fail "flock is required"
exec 9>"$lock_file"
flock --exclusive --wait 120 9 || fail "another control-plane update holds the lock"
service_state="$(control_plane_service_state)"
if [ "$service_state" = "active" ]; then
  service_was_active=1
  original_control_plane_container_id="$(control_plane_container_id)"
  original_control_plane_revision="$(
    control_plane_revision "$original_control_plane_container_id"
  )"
fi
original_profile="$(profile_value_from_file "$controller_env")"
if [ "$service_was_active" -eq 1 ]; then
  verify_profile_readback "$original_profile" "$original_control_plane_container_id"
fi

if [ "$operation" = "enable" ]; then
  [ "$service_was_active" -eq 1 ] || fail "enable requires an active control plane"
  wait_for_control_plane || fail "enable requires a healthy control plane"
  [ "$original_control_plane_revision" = "$expected_revision" ] ||
    fail "running control-plane revision differs from the expected revision"
  /usr/bin/timeout --signal=KILL "${environment_program_timeout_seconds}s" \
    /usr/bin/python3 -c "$environment_program" \
    "$controller_env" status "$expected_worker_image" \
    "$expected_worker_source_revision" "$expected_private_manifest_source_sha256" \
    "$expected_model_manifest_sha256" >/dev/null
  [ "$original_profile" = "false" ] ||
    fail "enable requires the public profile to start disabled"
  verify_provider_and_queue false
fi

environment_backup="$(mktemp "$config_root/.control-plane.env.i2v-profile.rollback.XXXXXX")"
/usr/bin/timeout --signal=KILL "${local_file_command_timeout_seconds}s" \
  /usr/bin/install -o root -g root -m 0600 "$controller_env" "$environment_backup"
rollback_armed=1

/usr/bin/timeout --signal=KILL "${environment_program_timeout_seconds}s" \
  /usr/bin/python3 -c "$environment_program" \
  "$controller_env" "$operation" "$expected_worker_image" \
  "$expected_worker_source_revision" "$expected_private_manifest_source_sha256" \
  "$expected_model_manifest_sha256"

expected_profile="false"
if [ "$operation" = "enable" ]; then
  expected_profile="true"
fi

if [ "$service_was_active" -eq 1 ]; then
  restart_from_control_plane_container_id="$(control_plane_container_id)"
  [ "$restart_from_control_plane_container_id" = \
    "$original_control_plane_container_id" ] ||
    fail "running control-plane container changed while the profile operation held the lock"
  updated_control_plane_container_id="$(
    restart_control_plane_requiring_replacement "$restart_from_control_plane_container_id"
  )"
  verify_profile_readback "$expected_profile" "$updated_control_plane_container_id"
  [ "$(control_plane_revision "$updated_control_plane_container_id")" = \
    "$original_control_plane_revision" ] ||
    fail "control-plane revision changed during profile $operation"
else
  [ "$operation" = "disable" ] || fail "enable requires an active control plane"
  [ "$(profile_value_from_file "$controller_env")" = "false" ] ||
    fail "disabled host public profile flag failed exact readback"
  if [ "$(control_plane_service_state)" != "inactive" ]; then
    fail "control plane unexpectedly became active during disable"
  fi
fi

if [ "$operation" = "enable" ]; then
  verify_provider_and_queue true
fi

rollback_armed=0
rm -f -- "$environment_backup"
environment_backup=""
printf '%s\n' "I2V LoRA profile $operation completed."
