#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
deploy_env="$config_root/deploy.env"
controller_env="$config_root/control-plane.env"
semantic_env="$config_root/semantic-gateway.env"
compose_file="$deploy_root/compose.yaml"
validator="/usr/local/libexec/gen-automation-validate-deployment"
service_name="gen-automation-staging.service"
backup_root="/var/lib/gen-automation/semantic-gateway-activation-backups"
lock_file="/run/lock/gen-automation-semantic-gateway-activation.lock"
allowed_repository="ghcr.io/neuraln-cyber/gen-automation/semantic-gateway"
model="Qwen/Qwen3-VL-8B-Instruct"
model_revision="60595ebc30ec8e3b1d3b9e65d4943ca011c0006a"

image=""
source_revision=""
endpoint_id=""
ssm_parameter=""
source_base_url=""
work_dir=""
backup_dir=""
backup_pending=""
rollback_armed=0
service_was_active=0

fail() {
  printf '%s\n' "semantic gateway activation failed: $*" >&2
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

upsert_env_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local next
  next="$(mktemp "$work_dir/env.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { matches = 0 }
    $0 ~ ("^" key "=") {
      if (matches > 0) {
        exit 42
      }
      print key "=" value
      matches += 1
      next
    }
    { print }
    END {
      if (matches == 0) {
        print key "=" value
      }
    }
  ' "$file" >"$next" || fail "$file contains duplicate $key entries"
  mv -- "$next" "$file"
}

atomic_install() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local temporary
  temporary="$(mktemp "$(dirname -- "$destination")/.$(basename -- "$destination").activate.XXXXXX")"
  install -o root -g root -m "$mode" "$source" "$temporary"
  mv -- "$temporary" "$destination"
}

verify_pulled_image() {
  local actual_revision
  local architecture
  local operating_system
  local repo_digests

  timeout --signal=TERM --kill-after=30s 600s \
    /usr/bin/docker pull --quiet "$image" >/dev/null
  repo_digests="$(
    /usr/bin/docker image inspect \
      --format '{{range .RepoDigests}}{{println .}}{{end}}' \
      "$image"
  )"
  printf '%s\n' "$repo_digests" | grep --fixed-strings --line-regexp --quiet "$image" ||
    fail "pulled image repository digest does not match the requested digest"
  actual_revision="$(
    /usr/bin/docker image inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$image"
  )"
  architecture="$(/usr/bin/docker image inspect --format '{{.Architecture}}' "$image")"
  operating_system="$(/usr/bin/docker image inspect --format '{{.Os}}' "$image")"
  [ "$actual_revision" = "$source_revision" ] ||
    fail "image revision label does not match the requested source revision"
  [ "$architecture" = "amd64" ] || fail "image architecture must be amd64"
  [ "$operating_system" = "linux" ] || fail "image operating system must be linux"
}

download_deployment_files() {
  local name
  for name in compose.yaml validate-deployment.sh; do
    curl \
      --fail \
      --location \
      --proto '=https' \
      --proto-redir '=https' \
      --retry 3 \
      --retry-all-errors \
      --silent \
      --show-error \
      --output "$work_dir/$name" \
      "$source_base_url/$name"
    [ -s "$work_dir/$name" ] || fail "downloaded $name is empty"
  done
  grep --fixed-strings --quiet 'semantic-gateway:' "$work_dir/compose.yaml" ||
    fail "commit-pinned compose file does not contain semantic-gateway"
  grep --fixed-strings --quiet '127.0.0.1:8091:8080/tcp' "$work_dir/compose.yaml" ||
    fail "commit-pinned compose file does not keep the gateway on loopback"
  grep --fixed-strings --quiet 'GEN_AUTOMATION_SEMANTIC_GATEWAY_IMAGE' \
    "$work_dir/validate-deployment.sh" ||
    fail "commit-pinned validator does not validate the semantic gateway image"
}

create_backup() {
  local final_name
  local index
  local path
  local paths=(
    "$compose_file"
    "$validator"
    "$deploy_env"
    "$controller_env"
    "$semantic_env"
  )

  install -d -o root -g root -m 0700 "$backup_root"
  backup_pending="$(mktemp -d "$backup_root/.pending.XXXXXX")"
  chmod 0700 "$backup_pending"
  for index in "${!paths[@]}"; do
    path="${paths[$index]}"
    if [ -f "$path" ]; then
      install -o root -g root -m 0600 "$path" "$backup_pending/$index.file"
      printf '%s\n' present >"$backup_pending/$index.state"
    else
      printf '%s\n' absent >"$backup_pending/$index.state"
    fi
  done
  chmod 0600 "$backup_pending"/*
  final_name="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)-$$"
  mv -- "$backup_pending" "$final_name"
  backup_pending=""
  backup_dir="$final_name"
}

wait_for_controller() {
  for _ in $(seq 1 120); do
    if systemctl is-active --quiet "$service_name" &&
      curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

wait_for_activation() {
  local container_id
  local configured_image
  for _ in $(seq 1 120); do
    container_id="$(
      /usr/bin/docker compose \
        --env-file "$deploy_env" \
        -f "$compose_file" \
        ps --quiet semantic-gateway 2>/dev/null || true
    )"
    configured_image=""
    if [ -n "$container_id" ]; then
      configured_image="$(
        /usr/bin/docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true
      )"
    fi
    if systemctl is-active --quiet "$service_name" &&
      [ "$configured_image" = "$image" ] &&
      curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8091/health/ready >/dev/null 2>&1 &&
      curl --fail --silent --show-error --max-time 5 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  return 1
}

restore_previous_deployment() {
  local rollback_status=0
  local index
  local state
  local paths=(
    "$compose_file"
    "$validator"
    "$deploy_env"
    "$controller_env"
    "$semantic_env"
  )
  local modes=(0644 0755 0600 0600 0600)

  printf '%s\n' "Activation did not become ready; restoring the previous deployment." >&2
  [ -n "$backup_dir" ] && [ -d "$backup_dir" ] || return 1
  for index in "${!paths[@]}"; do
    state="$(<"$backup_dir/$index.state")"
    if [ "$state" = present ]; then
      atomic_install "$backup_dir/$index.file" "${paths[$index]}" "${modes[$index]}" ||
        rollback_status=1
    elif [ "$state" = absent ]; then
      rm -f -- "${paths[$index]}" || rollback_status=1
    else
      rollback_status=1
    fi
  done

  if [ "$service_was_active" -eq 1 ]; then
    "$validator" || rollback_status=1
    /usr/bin/docker compose \
      --env-file "$deploy_env" \
      -f "$compose_file" \
      config --quiet || rollback_status=1
    systemctl restart --no-block "$service_name" || rollback_status=1
    wait_for_controller || rollback_status=1
  else
    systemctl stop "$service_name" || rollback_status=1
  fi
  [ "$rollback_status" -eq 0 ] ||
    printf '%s\n' "Automatic rollback requires operator attention." >&2
  return "$rollback_status"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_deployment
  fi
  [ -z "$work_dir" ] || rm -rf -- "$work_dir"
  [ -z "$backup_pending" ] || rm -rf -- "$backup_pending"
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
[ "$#" -eq 10 ] || fail \
  "usage: $0 --image <digest> --revision <sha> --endpoint-id <id> --ssm-parameter <name> --source-base-url <url>"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --image) image="$2"; shift 2 ;;
    --revision) source_revision="$2"; shift 2 ;;
    --endpoint-id) endpoint_id="$2"; shift 2 ;;
    --ssm-parameter) ssm_parameter="$2"; shift 2 ;;
    --source-base-url) source_base_url="${2%/}"; shift 2 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/semantic-gateway@sha256:[0-9a-f]{64}$ ]] ||
  fail "image must be an immutable digest from $allowed_repository"
[[ "$source_revision" =~ ^[0-9a-f]{40}$ ]] ||
  fail "source revision must be exactly 40 lowercase hexadecimal characters"
[[ "$endpoint_id" =~ ^[A-Za-z0-9_-]{5,64}$ ]] || fail "RunPod endpoint ID is invalid"
[[ "$ssm_parameter" =~ ^/[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$ ]] ||
  fail "SSM parameter name must be an exact absolute path"
[ "$source_base_url" = \
  "https://raw.githubusercontent.com/neuraln-cyber/gen-automation/$source_revision/infra/aws-staging/deploy" ] ||
  fail "source base URL must pin the requested commit in the approved repository"

for command in aws curl flock ss timeout; do
  command -v "$command" >/dev/null || fail "$command is required"
done
[ -f "$deploy_env" ] || fail "missing $deploy_env"
[ -f "$controller_env" ] || fail "missing $controller_env"
if ss -H -ltn 'sport = :8091' | grep --quiet .; then
  fail "loopback gateway port 8091 is already in use"
fi

exec 9>"$lock_file"
flock --exclusive --wait 120 9 || fail "another deployment update holds the activation lock"
work_dir="$(mktemp -d)"
chmod 0700 "$work_dir"
download_deployment_files
verify_pulled_image

key_output="$work_dir/ssm-key"
AWS_REGION=eu-central-1 AWS_DEFAULT_REGION=eu-central-1 \
  aws ssm get-parameter \
    --name "$ssm_parameter" \
    --with-decryption \
    --query Parameter.Value \
    --output text \
    --no-cli-pager >"$key_output" 2>/dev/null ||
  fail "could not read the RunPod API key from the requested SSM parameter"
chmod 0600 "$key_output"
mapfile -t key_lines <"$key_output"
[ "${#key_lines[@]}" -eq 1 ] || fail "RunPod API key must contain exactly one line"
runpod_key="${key_lines[0]}"
[[ "$runpod_key" =~ ^[A-Za-z0-9._-]{20,512}$ ]] ||
  fail "RunPod API key has an invalid format"

install -o root -g root -m 0600 "$deploy_env" "$work_dir/deploy.env"
upsert_env_key "$work_dir/deploy.env" GEN_AUTOMATION_SEMANTIC_GATEWAY_IMAGE "$image"

install -o root -g root -m 0600 "$controller_env" "$work_dir/control-plane.env"
controller_values=(
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED=false'
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE=shadow'
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL=http://127.0.0.1:8091/v1/anatomy/assess'
  "GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL=$model"
  "GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL_REVISION=$model_revision"
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE=0'
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST=[]'
  'GEN_AUTOMATION_SEMANTIC_ANATOMY_SEVERE_CONFIDENCE_MICROS=900000'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_REQUEST_TIMEOUT_SECONDS=630'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_TIMEOUT_SECONDS=660'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_LEASE_SECONDS=720'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS=5'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_BASE_SECONDS=30'
  'GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_MAX_SECONDS=120'
)
for assignment in "${controller_values[@]}"; do
  upsert_env_key \
    "$work_dir/control-plane.env" \
    "${assignment%%=*}" \
    "${assignment#*=}"
done

semantic_values=(
  "GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_CHAT_COMPLETIONS_URL=https://api.runpod.ai/v2/$endpoint_id/openai/v1/chat/completions"
  "GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL=$model"
  "GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL_REVISION=$model_revision"
  "GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_API_KEY=$runpod_key"
  'GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_TIMEOUT_SECONDS=600'
  'GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_MAX_TOKENS=512'
  'GEN_AUTOMATION_SEMANTIC_GATEWAY_MAX_IMAGE_BYTES=12582912'
  'GEN_AUTOMATION_SEMANTIC_GATEWAY_MAX_UPSTREAM_RESPONSE_BYTES=131072'
)
: >"$work_dir/semantic-gateway.env"
chmod 0600 "$work_dir/semantic-gateway.env"
for assignment in "${semantic_values[@]}"; do
  printf '%s\n' "$assignment" >>"$work_dir/semantic-gateway.env"
done
unset assignment runpod_key key_lines semantic_values
rm -f -- "$key_output"

for assignment in "${controller_values[@]}"; do
  [ "$(env_value "${assignment%%=*}" "$work_dir/control-plane.env")" = "${assignment#*=}" ] ||
    fail "prepared controller environment does not preserve the bounded shadow setting"
done
[ "$(env_value GEN_AUTOMATION_SEMANTIC_GATEWAY_IMAGE "$work_dir/deploy.env")" = "$image" ] ||
  fail "prepared deploy environment does not contain the requested image"

if ss -H -ltn 'sport = :8091' | grep --quiet .; then
  fail "loopback gateway port 8091 became unavailable during activation preparation"
fi
systemctl is-active --quiet "$service_name" && service_was_active=1
create_backup
rollback_armed=1
atomic_install "$work_dir/compose.yaml" "$compose_file" 0644
atomic_install "$work_dir/validate-deployment.sh" "$validator" 0755
atomic_install "$work_dir/deploy.env" "$deploy_env" 0600
atomic_install "$work_dir/control-plane.env" "$controller_env" 0600
atomic_install "$work_dir/semantic-gateway.env" "$semantic_env" 0600

"$validator"
/usr/bin/docker compose \
  --env-file "$deploy_env" \
  -f "$compose_file" \
  config --quiet
systemctl restart --no-block "$service_name"
wait_for_activation || fail "semantic gateway and controller did not become ready"

rollback_armed=0
printf '%s\n' \
  "Semantic gateway activated and healthy; anatomy remains disabled in bounded shadow mode."
