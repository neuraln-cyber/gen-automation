#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
validator="/usr/local/libexec/gen-automation-validate-deployment"
service_name="gen-automation-staging.service"
lock_file="/run/lock/gen-automation-control-plane-update.lock"
video_image_key="GEN_AUTOMATION_SALAD_VIDEO_WORKER_IMAGE"
image_lane_key="GEN_AUTOMATION_SALAD_WORKER_IMAGE"
minimum_control_plane_revision="d585214403c2b8090dc468b5045db1cf7b06b3ac"
private_repository="ghcr.io/neuraln-cyber/gen-automation-a14b-registry/video-worker-a14b-private"

new_image=""
expected_revision=""
old_video_image=""
old_image_lane=""
image_lane_sha256=""
private_deployment_id=""
temporary_env=""
backup_env=""
rollback_restore_env=""
rollback_armed=0
retain_rollback_backup=0
external_lock_held=0
validate_only=0

fail() {
  printf '%s\n' "A14B VIDEO cutover failed: $*" >&2
  return 1
}

require_private_root_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing required host configuration"
  [ ! -L "$path" ] || fail "host configuration cannot be a symbolic link"
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$path")" = "0:0:600" ] ||
    fail "host configuration must remain root:root mode 0600"
  [ "$(/usr/bin/stat -c '%s' "$path")" -le 1048576 ] ||
    fail "host configuration is unexpectedly large"
}

env_value() {
  local key="$1"
  local file="$2"
  local matches
  matches="$(/usr/bin/grep -E "^${key}=" "$file" || true)"
  [ "$(printf '%s\n' "$matches" | /usr/bin/sed '/^$/d' | /usr/bin/wc -l)" -eq 1 ] ||
    fail "host configuration must define $key exactly once"
  printf '%s' "${matches#*=}"
}

control_plane_container() {
  local container_id
  container_id="$(
    /usr/bin/docker compose \
      --env-file "$deploy_env" \
      --file "$compose_file" \
      ps --status running --quiet control-plane-mega 2>/dev/null || true
  )"
  [ -n "$container_id" ] || fail "running control-plane container was not found"
  [ "$(printf '%s\n' "$container_id" | /usr/bin/sed '/^$/d' | /usr/bin/wc -l)" -eq 1 ] ||
    fail "expected exactly one running control-plane container"
  printf '%s' "$container_id"
}

control_plane_revision() {
  local container_id
  local revision
  container_id="$(control_plane_container)"
  revision="$(
    /usr/bin/docker inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$container_id"
  )"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "running control-plane image has no valid source revision"
  printf '%s' "$revision"
}

runtime_has_video_image() {
  local expected_image="$1"
  local container_id
  container_id="$(control_plane_container)" || return 1
  /usr/bin/docker exec "$container_id" python3.12 -c '
import hmac
import sys
from gen_automation.config import Settings

raise SystemExit(
    0
    if hmac.compare_digest(Settings().salad_video_worker_image or "", sys.argv[1])
    else 1
)
' "$expected_image" >/dev/null 2>&1
}

wait_for_control_plane() {
  local expected_video_image="$1"
  for _ in $(seq 1 60); do
    if systemctl is-active --quiet "$service_name" &&
      curl \
        --fail \
        --silent \
        --show-error \
        --max-time 5 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1 &&
      runtime_has_video_image "$expected_video_image"; then
      return 0
    fi
    sleep 5
  done
  return 1
}

assert_cutover_safe() {
  local current_image="$1"
  local container_id
  local output
  container_id="$(control_plane_container)" || return 1
  output="$(
    /usr/bin/docker exec "$container_id" \
      python3.12 -m gen_automation.a14b_private_provision_cli \
      assert-cutover-safe \
      --expected-current-image "$current_image" \
      --minimum-control-plane-revision "$minimum_control_plane_revision" \
      2>/dev/null
  )" || return 1
  [[ "$output" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s' "$output"
}

wait_for_cutover_applied() {
  local container_id
  local output
  for _ in $(seq 1 60); do
    container_id="$(control_plane_container 2>/dev/null || true)"
    output=""
    if [ -n "$container_id" ]; then
      output="$(/usr/bin/docker exec "$container_id" \
        python3.12 -m gen_automation.a14b_private_provision_cli \
        assert-cutover-applied \
        --image "$new_image" \
        --minimum-control-plane-revision "$minimum_control_plane_revision" \
        --expected-image-lane-sha256 "$image_lane_sha256" \
        2>/dev/null)" || output=""
    fi
    if [[ "$output" =~ ^deployment_id=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$ ]]; then
      private_deployment_id="${BASH_REMATCH[1]}"
      return 0
    fi
    sleep 5
  done
  return 1
}

atomic_restore_environment_file() {
  local source_backup="$1"
  local target_environment="$2"
  local target_directory
  target_directory="$(/usr/bin/dirname -- "$target_environment")" || return 1
  [ "$target_directory" = "$config_root" ] || return 1
  [ "$(/usr/bin/dirname -- "$source_backup")" = "$config_root" ] || return 1
  [ -f "$source_backup" ] || return 1

  rollback_restore_env="$(
    /usr/bin/mktemp "$config_root/.control-plane.env.a14b.restore.XXXXXX"
  )" || return 1
  /usr/bin/cp -- "$source_backup" "$rollback_restore_env" || return 1
  /usr/bin/chmod 0600 "$rollback_restore_env" || return 1
  /usr/bin/cmp --silent "$source_backup" "$rollback_restore_env" || return 1
  /usr/bin/mv -- "$rollback_restore_env" "$target_environment" || return 1
  rollback_restore_env=""
  /usr/bin/cmp --silent "$source_backup" "$target_environment"
}

restore_previous_environment() {
  local rollback_failed=0
  set +e
  printf '%s\n' \
    "A14B VIDEO cutover did not validate; restoring the exact prior environment." >&2
  if ! atomic_restore_environment_file "$backup_env" "$controller_env"; then
    rollback_failed=1
  elif ! verify_restored_environment; then
    rollback_failed=1
  fi
  if [ "$rollback_failed" -eq 0 ]; then
    printf '%s\n' "Exact prior controller environment was restored and is healthy." >&2
  else
    retain_rollback_backup=1
    printf '%s\n' "Automatic A14B rollback needs operator attention." >&2
    if [ -n "$backup_env" ] && [ -f "$backup_env" ]; then
      printf '%s\n' "Rollback backup retained at $backup_env" >&2
    fi
  fi
  return "$rollback_failed"
}

verify_restored_environment() {
  "$validator" || return 1
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    --file "$compose_file" \
    config --quiet || return 1
  systemctl restart --no-block "$service_name" || return 1
  wait_for_control_plane "$old_video_image" || return 1
  [ "$(control_plane_revision 2>/dev/null)" = "$expected_revision" ] || return 1
  [ "$(env_value "$image_lane_key" "$controller_env" 2>/dev/null)" = "$old_image_lane" ] ||
    return 1
}

cleanup_files() {
  [ -z "$temporary_env" ] || /usr/bin/rm -f -- "$temporary_env"
  [ -z "$rollback_restore_env" ] || /usr/bin/rm -f -- "$rollback_restore_env"
  if [ "$retain_rollback_backup" -eq 0 ]; then
    [ -z "$backup_env" ] || /usr/bin/rm -f -- "$backup_env"
  fi
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_environment || true
  fi
  cleanup_files
  exit "$status"
}
trap cleanup EXIT

[ "$#" -ge 1 ] ||
  fail "usage: $0 [--validate-only] --image <private-digest> --expected-control-plane-revision <40-hex> [--external-lock-held]"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --image)
      [ "$#" -ge 2 ] || fail "--image requires a value"
      new_image="$2"
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
    --validate-only)
      validate_only=1
      shift
      ;;
    *) fail "unknown argument" ;;
  esac
done

[[ "$new_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation-a14b-registry/video-worker-a14b-private@sha256:[0-9a-f]{64}$ ]] ||
  fail "image must be an immutable digest from $private_repository"
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] ||
  fail "expected control-plane revision must be exactly 40 lowercase hexadecimal characters"
if [ "$validate_only" -eq 1 ]; then
  [ "$external_lock_held" -eq 0 ] || fail "offline validation does not accept lock state"
  printf '%s\n' "A14B VIDEO cutover arguments are valid; no host or provider state was read."
  exit 0
fi

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
command -v flock >/dev/null || fail "flock is required"
[ -x /usr/bin/docker ] || fail "/usr/bin/docker is required"
[ -x "$validator" ] || fail "deployment validator is missing"
require_private_root_file "$controller_env"
require_private_root_file "$deploy_env"
[ -f "$compose_file" ] || fail "deployment compose file is missing"

if [ "$external_lock_held" -eq 0 ]; then
  exec 9>"$lock_file"
  flock --exclusive --wait 120 9 || fail "another control-plane change holds the lock"
fi

systemctl is-active --quiet "$service_name" ||
  fail "cutover requires an active baseline control plane"
wait_for_control_plane "$(env_value "$video_image_key" "$controller_env")" ||
  fail "cutover requires a healthy baseline control plane"
[ "$(control_plane_revision)" = "$expected_revision" ] ||
  fail "running control-plane revision does not match the exact operator binding"

old_video_image="$(env_value "$video_image_key" "$controller_env")"
old_image_lane="$(env_value "$image_lane_key" "$controller_env")"
[[ "$old_video_image" =~ ^ghcr[.]io/neuraln-cyber/(gen-automation/video-worker|gen-automation-a14b-registry/video-worker-a14b-private)@sha256:[0-9a-f]{64}$ ]] ||
  fail "currently configured VIDEO image is not an approved immutable reference"
[[ "$old_image_lane" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/gpu-worker@sha256:[0-9a-f]{64}$ ]] ||
  fail "IMAGE lane immutable binding is invalid"

image_lane_sha256="$(assert_cutover_safe "$old_video_image")" ||
  fail "migration, control-plane contract, or drained VIDEO preflight failed"

if [ "$new_image" = "$old_video_image" ]; then
  fail "requested private digest is already configured; use the one-shot provision command"
fi

backup_env="$(/usr/bin/mktemp "$config_root/.control-plane.env.a14b.rollback.XXXXXX")"
temporary_env="$(/usr/bin/mktemp "$config_root/.control-plane.env.a14b.update.XXXXXX")"
/usr/bin/install -o root -g root -m 0600 "$controller_env" "$backup_env"
/usr/bin/awk -v image="$new_image" -v key="$video_image_key" '
  BEGIN { matches = 0 }
  $0 ~ ("^" key "=") {
    print key "=" image
    matches += 1
    next
  }
  { print }
  END {
    if (matches != 1) {
      exit 42
    }
  }
' "$controller_env" >"$temporary_env" || fail "could not prepare the atomic VIDEO-only update"
/usr/bin/chown root:root "$temporary_env"
/usr/bin/chmod 0600 "$temporary_env"
[ "$(env_value "$video_image_key" "$temporary_env")" = "$new_image" ] ||
  fail "prepared VIDEO image value is invalid"
[ "$(env_value "$image_lane_key" "$temporary_env")" = "$old_image_lane" ] ||
  fail "prepared update changed the IMAGE lane"

rollback_armed=1
/usr/bin/mv -- "$temporary_env" "$controller_env"
temporary_env=""
"$validator"
/usr/bin/docker compose \
  --env-file "$deploy_env" \
  --file "$compose_file" \
  config --quiet
systemctl restart --no-block "$service_name"
wait_for_control_plane "$new_image" || fail "restarted control plane did not become ready"
[ "$(control_plane_revision)" = "$expected_revision" ] ||
  fail "control-plane revision changed during VIDEO cutover"
wait_for_cutover_applied ||
  fail "private VIDEO rollout did not reach the exact authorization boundary"
[ "$(env_value "$image_lane_key" "$controller_env")" = "$old_image_lane" ] ||
  fail "IMAGE lane changed during VIDEO cutover"

rollback_armed=0
/usr/bin/rm -f -- "$backup_env"
backup_env=""
printf '%s\n' \
  "Private A14B VIDEO image is configured and awaiting one-shot registry authorization." \
  "deployment_id=$private_deployment_id"
