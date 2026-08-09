#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
service_name="gen-automation-staging.service"
image_key="GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE"
allowed_repository="ghcr.io/neuraln-cyber/gen-automation/control-plane-mega"
lock_file="/run/lock/gen-automation-control-plane-update.lock"

new_image=""
source_revision=""
old_image=""
temporary_env=""
backup_env=""
rollback_armed=0
rollback_mode="restart"
external_lock_held=0

fail() {
  printf '%s\n' "control-plane update failed: $*" >&2
  return 1
}

env_value() {
  local key="$1"
  local file="$2"
  local matches
  matches="$(grep -E "^${key}=" "$file" || true)"
  [ "$(printf '%s\n' "$matches" | sed '/^$/d' | wc -l)" -eq 1 ] ||
    fail "$file must define $key exactly once"
  printf '%s' "${matches#*=}"
}

wait_for_control_plane() {
  local expected_image="$1"
  local container_id
  local configured_image

  for _ in $(seq 1 60); do
    container_id="$(
      /usr/bin/docker compose \
        --env-file "$deploy_env" \
        -f "$compose_file" \
        ps --quiet control-plane-mega 2>/dev/null || true
    )"
    configured_image=""
    if [ -n "$container_id" ]; then
      configured_image="$(
        /usr/bin/docker inspect \
          --format '{{.Config.Image}}' \
          "$container_id" 2>/dev/null || true
      )"
    fi
    if systemctl is-active --quiet "$service_name" &&
      [ "$configured_image" = "$expected_image" ] &&
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

verify_pulled_image() {
  local image="$1"
  local expected_revision="$2"
  local actual_revision
  local architecture
  local operating_system

  timeout --signal=TERM --kill-after=30s 600s \
    /usr/bin/docker pull --quiet "$image" >/dev/null
  actual_revision="$(
    /usr/bin/docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$image"
  )"
  architecture="$(/usr/bin/docker image inspect --format '{{.Architecture}}' "$image")"
  operating_system="$(/usr/bin/docker image inspect --format '{{.Os}}' "$image")"

  [ "$actual_revision" = "$expected_revision" ] ||
    fail "image revision label does not match the requested source revision"
  [ "$architecture" = "amd64" ] || fail "image architecture must be amd64"
  [ "$operating_system" = "linux" ] || fail "image operating system must be linux"
}

restore_previous_deployment() {
  local rollback_status=0

  printf '%s\n' "Update did not become ready; restoring the previous immutable image." >&2
  if [ -z "$backup_env" ] || [ ! -f "$backup_env" ]; then
    printf '%s\n' "Rollback copy is unavailable." >&2
    return 1
  fi

  if mv -- "$backup_env" "$deploy_env"; then
    backup_env=""
  else
    rollback_status=1
  fi
  /usr/local/libexec/gen-automation-validate-deployment || rollback_status=1
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    -f "$compose_file" \
    config --quiet || rollback_status=1
  if [ "$rollback_mode" = "leave-stopped" ]; then
    systemctl stop "$service_name" || rollback_status=1
    if systemctl is-active --quiet "$service_name"; then
      rollback_status=1
    fi
  else
    systemctl restart --no-block "$service_name" || rollback_status=1
    wait_for_control_plane "$old_image" || rollback_status=1
  fi

  if [ "$rollback_status" -eq 0 ]; then
    if [ "$rollback_mode" = "leave-stopped" ]; then
      printf '%s\n' "Previous control-plane configuration restored; service remains stopped." >&2
    else
      printf '%s\n' "Previous control-plane image restored and ready." >&2
    fi
  else
    printf '%s\n' "Automatic rollback did not become healthy; operator attention is required." >&2
  fi
  return "$rollback_status"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_deployment
  fi
  [ -z "$temporary_env" ] || rm -f -- "$temporary_env"
  [ -z "$backup_env" ] || rm -f -- "$backup_env"
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
[ "$#" -ge 4 ] && [ "$#" -le 7 ] ||
  fail "usage: $0 --image <immutable-image> --revision <40-hex-sha> [--rollback-mode restart|leave-stopped] [--external-lock-held]"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --image)
      new_image="$2"
      shift 2
      ;;
    --revision)
      source_revision="$2"
      shift 2
      ;;
    --rollback-mode)
      rollback_mode="$2"
      shift 2
      ;;
    --external-lock-held)
      external_lock_held=1
      shift
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$new_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
  fail "image must be an immutable digest from $allowed_repository"
[[ "$source_revision" =~ ^[0-9a-f]{40}$ ]] ||
  fail "source revision must be exactly 40 lowercase hexadecimal characters"
case "$rollback_mode" in restart|leave-stopped) ;; *) fail "invalid rollback mode" ;; esac
command -v flock >/dev/null || fail "flock is required"
command -v timeout >/dev/null || fail "timeout is required"

if [ "$external_lock_held" -eq 0 ]; then
  exec 9>"$lock_file"
  flock --exclusive --wait 120 9 || fail "another control-plane update holds the deployment lock"
fi

/usr/local/libexec/gen-automation-validate-deployment
old_image="$(env_value "$image_key" "$deploy_env")"
[[ "$old_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
  fail "currently configured control-plane image is not an approved immutable reference"

verify_pulled_image "$new_image" "$source_revision"
if [ "$new_image" = "$old_image" ]; then
  systemctl start --no-block "$service_name"
  wait_for_control_plane "$new_image" ||
    fail "requested image is already configured but the deployment is not ready"
  printf '%s\n' "Requested control-plane digest is already deployed and ready."
  exit 0
fi

backup_env="$(mktemp "$config_root/.deploy.env.rollback.XXXXXX")"
temporary_env="$(mktemp "$config_root/.deploy.env.update.XXXXXX")"
install -o root -g root -m 0600 "$deploy_env" "$backup_env"
awk -v image="$new_image" -v key="$image_key" '
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
' "$deploy_env" >"$temporary_env" || fail "could not prepare the atomic environment update"
chown root:root "$temporary_env"
chmod 0600 "$temporary_env"

rollback_armed=1
mv -- "$temporary_env" "$deploy_env"
temporary_env=""
/usr/local/libexec/gen-automation-validate-deployment
/usr/bin/docker compose \
  --env-file "$deploy_env" \
  -f "$compose_file" \
  config --quiet
systemctl restart --no-block "$service_name"
wait_for_control_plane "$new_image" || fail "new control-plane image did not become ready"

rollback_armed=0
rm -f -- "$backup_env"
backup_env=""
printf '%s\n' "Control-plane update completed and readiness checks passed."
