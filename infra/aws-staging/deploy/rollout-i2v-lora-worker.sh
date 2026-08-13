#!/usr/bin/env bash
set -Eeuo pipefail

# Queue-preserving two-phase rollout for the reviewed I2V LoRA worker. The
# serving control plane remains healthy in a maintenance profile throughout the
# potentially long provider image pull. This script never submits, retries,
# cancels, or reorders an I2V job.

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
service_name="gen-automation-staging.service"
lock_file="/run/lock/gen-automation-control-plane-update.lock"
state_root="/var/lib/gen-automation/i2v-lora-rollout"
active_state="$state_root/active"

manifest_bucket="gen-automation-staging-861912887470-eu-central-1-models"
manifest_key="worker/i2v/manifests/sha256/f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e.json"
manifest_version="u4bSnCPzDJ4zctrA2Nr66ji0Zh2qPpXX"
manifest_source_sha256="f0cd579606c8bc7fbf77ee8353b5c542395576d08f21e9acea37a1e2de19876e"

operation=""
expected_revision=""
expected_worker_image=""
expected_worker_source_revision=""
work_dir=""
current_env=""
original_env=""
maintenance_env=""
provider_state=""
provider_marker=""
resume_env=""
provider_rollback_needed=0
rollback_armed=0
rollback_failed=0
active_persisted=0

fail() {
  printf '%s\n' "I2V LoRA worker rollout failed: $*" >&2
  return 1
}

compose() {
  /usr/bin/docker compose --env-file "$deploy_env" --file "$compose_file" "$@"
}

require_private_root_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing $path"
  [ "$(stat -c '%U:%G' "$path")" = "root:root" ] || fail "$path must be root-owned"
  case "$(stat -c '%a' "$path")" in 400|600) ;; *) fail "$path must be private" ;; esac
}

control_plane_container_id() {
  local value
  value="$(timeout 15s /usr/bin/docker compose --env-file "$deploy_env" \
    --file "$compose_file" ps --status running --quiet control-plane-mega 2>/dev/null || true)"
  [ -n "$value" ] || fail "running control-plane container was not found"
  [ "$(printf '%s\n' "$value" | sed '/^$/d' | wc -l)" -eq 1 ] ||
    fail "expected exactly one running control-plane container"
  printf '%s' "$value"
}

optional_control_plane_container_id() {
  local value
  value="$(timeout 15s /usr/bin/docker compose --env-file "$deploy_env" \
    --file "$compose_file" ps --status running --quiet control-plane-mega 2>/dev/null || true)"
  if [ -n "$value" ] && [ "$(printf '%s\n' "$value" | sed '/^$/d' | wc -l)" -eq 1 ]; then
    printf '%s' "$value"
  fi
}

container_revision() {
  /usr/bin/docker inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$1"
}

container_image() {
  /usr/bin/docker inspect --format '{{.Config.Image}}' "$1"
}

wait_for_replacement() {
  local old_id="$1"
  local expected="$2"
  local current_id=""
  local revision=""
  local deadline
  local now
  local remaining
  deadline="$(( $(cut -d. -f1 /proc/uptime) + 600 ))"
  while true; do
    now="$(cut -d. -f1 /proc/uptime)"
    remaining="$((deadline - now))"
    [ "$remaining" -gt 0 ] || return 1
    current_id="$(timeout 15s /usr/bin/docker compose --env-file "$deploy_env" \
      --file "$compose_file" ps --status running --quiet control-plane-mega \
      2>/dev/null || true)"
    if [ -n "$current_id" ] && { [ -z "$old_id" ] || [ "$current_id" != "$old_id" ]; }; then
      revision="$(timeout 15s /usr/bin/docker inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "$current_id" 2>/dev/null || true)"
      if [ "$revision" = "$expected" ] && systemctl is-active --quiet "$service_name" &&
        curl --fail --silent --show-error --max-time "$((remaining < 5 ? remaining : 5))" \
          http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
        printf '%s' "$current_id"
        return 0
      fi
    fi
    sleep "$((remaining < 5 ? remaining : 5))"
  done
}

restart_into() {
  local source_env="$1"
  local expected="$2"
  local old_id
  local new_id
  old_id="$(optional_control_plane_container_id)"
  install -o root -g root -m 0600 "$source_env" "$controller_env"
  systemctl reset-failed "$service_name" >/dev/null 2>&1 || true
  timeout --signal=TERM --kill-after=10s 60s \
    systemctl restart --no-block "$service_name"
  new_id="$(wait_for_replacement "$old_id" "$expected")" ||
    fail "replacement control plane did not become ready"
  printf '%s' "$new_id"
}

render_profile() {
  local source="$1"
  local destination="$2"
  local mode="$3"
  local patch_path="${4:-}"
  /usr/bin/python3 - "$source" "$destination" "$mode" "$patch_path" <<'PY'
import os
import pathlib
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
mode = sys.argv[3]
lines = source.read_text(encoding="utf-8").splitlines()

bootstrap_defaults = {
    # These coordinates were introduced after the original four-role I2V
    # deployment.  A first reviewed-LoRA rollout may therefore encounter a
    # healthy legacy host that has no lines for them yet.  Materialize only
    # their fail-closed baseline values in the private operation copy; never
    # alter the live host until the guarded restart step.
    "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION": "",
    "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256": "",
    "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "false",
    "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "false",
}

if mode == "normalize":
    updates = {}
elif mode == "maintenance":
    updates = {
        "GEN_AUTOMATION_I2V_ENABLED": "false",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED": "false",
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "false",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "false",
    }
elif mode == "target":
    patch_path = pathlib.Path(sys.argv[4])
    patch_lines = patch_path.read_text(encoding="utf-8").splitlines()
    updates = {}
    for line in patch_lines:
        if "=" not in line:
            raise SystemExit("invalid reviewed host patch")
        key, value = line.split("=", 1)
        if key in updates or not key or "\n" in value or "\r" in value:
            raise SystemExit("invalid reviewed host patch")
        updates[key] = value
    required = {
        "GEN_AUTOMATION_I2V_ENABLED",
        "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED",
        "GEN_AUTOMATION_I2V_WORKER_IMAGE",
        "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION",
        "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256",
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON",
        "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256",
        "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
        "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED",
    }
    if set(updates) != required:
        raise SystemExit("reviewed host patch is incomplete")
else:
    raise SystemExit("unknown profile rendering mode")

managed = set(updates) | set(bootstrap_defaults)
seen = {key: 0 for key in managed}
rendered = []
for line in lines:
    key = line.split("=", 1)[0]
    if key in managed:
        seen[key] += 1
    if key in updates:
        rendered.append(f"{key}={updates[key]}")
    else:
        rendered.append(line)
if any(count > 1 for count in seen.values()):
    raise SystemExit("control-plane environment does not contain each rollout key exactly once")
for key, default in bootstrap_defaults.items():
    if seen[key] == 0:
        rendered.append(f"{key}={updates.get(key, default)}")
if any(seen[key] != 1 for key in set(updates) - set(bootstrap_defaults)):
    raise SystemExit("control-plane environment does not contain each rollout key exactly once")

destination.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(prefix=".i2v-lora-profile.", dir=destination.parent)
temporary = pathlib.Path(temporary_name)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("\n".join(rendered) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)
PY
}

merge_saved_i2v_profile() {
  local current="$1"
  local saved="$2"
  local destination="$3"
  /usr/bin/python3 - "$current" "$saved" "$destination" <<'PY'
import os
import pathlib
import tempfile
import sys

current, saved, destination = map(pathlib.Path, sys.argv[1:])
keys = {
    "GEN_AUTOMATION_I2V_ENABLED",
    "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED",
    "GEN_AUTOMATION_I2V_WORKER_IMAGE",
    "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION",
    "GEN_AUTOMATION_I2V_PRIVATE_MANIFEST_SOURCE_SHA256",
    "GEN_AUTOMATION_I2V_MODEL_MANIFEST_JSON",
    "GEN_AUTOMATION_I2V_MODEL_MANIFEST_SHA256",
    "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED",
    "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED",
}
def parse(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        key = line.split("=", 1)[0]
        if key in keys:
            if "=" not in line or key in values:
                raise SystemExit("invalid I2V environment profile")
            values[key] = line.split("=", 1)[1]
    if set(values) != keys:
        raise SystemExit("incomplete I2V environment profile")
    return lines, values
current_lines, _ = parse(current)
_, saved_values = parse(saved)
rendered = [
    f"{key}={saved_values[key]}" if key in keys else line
    for line in current_lines
    for key in [line.split("=", 1)[0]]
]
destination.parent.mkdir(parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(prefix=".i2v-lora-restore.", dir=destination.parent)
temporary = pathlib.Path(name)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write("\n".join(rendered) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
finally:
    temporary.unlink(missing_ok=True)
PY
}

assert_zero_status() {
  /usr/bin/python3 -c '
import json, sys
value=json.load(sys.stdin)
keys=("durable_active_jobs","durable_active_attempts","provider_active_jobs")
raise SystemExit(0 if all(value.get(key) == 0 for key in keys) else 1)
'
}

assert_container_flags() {
  local container_id="$1"
  local enabled="$2"
  local hires="$3"
  local worker="$4"
  local public="$5"
  timeout --signal=TERM --kill-after=10s 60s /usr/bin/docker exec \
    --env EXPECTED_I2V_ENABLED="$enabled" \
    --env EXPECTED_I2V_HIRES="$hires" \
    --env EXPECTED_I2V_LORA_WORKER="$worker" \
    --env EXPECTED_I2V_LORA_PUBLIC="$public" \
    "$container_id" python3.12 -c '
import os
from gen_automation.config import Settings
s = Settings()
expected = (
    os.environ["EXPECTED_I2V_ENABLED"] == "true",
    os.environ["EXPECTED_I2V_HIRES"] == "true",
    os.environ["EXPECTED_I2V_LORA_WORKER"] == "true",
    os.environ["EXPECTED_I2V_LORA_PUBLIC"] == "true",
)
actual = (
    s.i2v_enabled,
    s.i2v_hires_profile_enabled,
    s.i2v_lora_worker_enabled,
    s.i2v_lora_profile_enabled,
)
raise SystemExit(0 if actual == expected else 1)
'
}

run_one_off() {
  local profile_env="$1"
  local container_id="$2"
  local timeout_seconds="$3"
  shift 3
  local image
  image="$(timeout 15s /usr/bin/docker inspect --format '{{.Config.Image}}' "$container_id")"
  timeout --signal=TERM --kill-after=60s "$timeout_seconds" \
    /usr/bin/docker run --rm --init --read-only --network host \
    --user 10001:10001 \
    --env-file "$profile_env" \
    --env GEN_AUTOMATION_I2V_ROLLOUT_MAINTENANCE_CONFIRMED=true \
    --volumes-from "$container_id":ro \
    --mount "type=bind,source=$work_dir,target=/run/i2v-lora-rollout" \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=1770 \
    --entrypoint python3.12 \
    "$image" -m gen_automation.i2v_lora_rollout_cli "$@"
}

profile_preflight() {
  local container_id="$1"
  local expected_public="$2"
  timeout --signal=TERM --kill-after=10s 900s \
    /usr/bin/docker exec "$container_id" python3.12 -m \
    gen_automation.i2v_lora_rollout_cli profile-preflight \
    --expected-worker-image "$expected_worker_image" \
    --expected-worker-source-revision "$expected_worker_source_revision" \
    --expected-public-profile "$expected_public"
}

rollback_after_failure() {
  set +e
  local maintenance_id=""
  local recovery_env="$resume_env"
  [ -f "$provider_marker" ] && provider_rollback_needed=1 || provider_rollback_needed=0
  printf '%s\n' "Rollout failed; keeping the site in queue-frozen maintenance while restoring." >&2
  maintenance_id="$(restart_into "$maintenance_env" "$expected_revision")" || rollback_failed=1
  if [ "$provider_rollback_needed" -eq 1 ] && [ ! -f "$provider_state" ]; then
    # A failed recycle writes the mutation marker before STOP. Its service-level
    # recovery removes that marker only after the exact group is safely started.
    # Without a promotion rollback state, an extant marker is therefore
    # ambiguous and must leave the serving controller queue-frozen.
    rollback_failed=1
  fi
  if [ "$provider_rollback_needed" -eq 1 ] && [ -f "$provider_state" ] &&
    [ "$rollback_failed" -eq 0 ]; then
    run_one_off "$original_env" "$maintenance_id" 1900s rollback \
        --rollback-state-input /run/i2v-lora-rollout/provider-rollback.json \
        --provider-mutation-marker-output \
          /run/i2v-lora-rollout/provider-mutation-attempted.json ||
      rollback_failed=1
  fi
  if [ "$provider_rollback_needed" -eq 0 ]; then
    restart_into "$resume_env" "$expected_revision" >/dev/null || rollback_failed=1
  elif [ "$rollback_failed" -eq 0 ]; then
    provider_rollback_needed=0
    recovery_env="$original_env"
    restart_into "$original_env" "$expected_revision" >/dev/null || rollback_failed=1
  else
    # Never abandon control-plane recovery merely because provider rollback
    # could not be attempted. Make one independent bounded effort to leave the
    # public site serving with all I2V queue mutations frozen.
    restart_into "$maintenance_env" "$expected_revision" >/dev/null 2>&1 || true
  fi
  if ! curl --fail --silent --max-time 5 \
      http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
    if [ "$provider_rollback_needed" -eq 0 ]; then
      restart_into "$recovery_env" "$expected_revision" >/dev/null 2>&1 || true
    else
      restart_into "$maintenance_env" "$expected_revision" >/dev/null 2>&1 || true
    fi
  fi
  if [ "$rollback_failed" -ne 0 ]; then
    printf '%s\n' "Automatic rollback needs operator attention; backups remain under $work_dir." >&2
  elif [ "$active_persisted" -eq 1 ] && [ -d "$active_state" ]; then
    rm -rf --one-file-system -- "$active_state" || rollback_failed=1
  fi
  return "$rollback_failed"
}

cleanup() {
  local status=$?
  local cleanup_target=""
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    rollback_after_failure || true
  fi
  if [ "$status" -eq 0 ] && [ -n "$work_dir" ] && [ "$operation" != "promote" ]; then
    cleanup_target="$(readlink -f -- "$work_dir" 2>/dev/null || true)"
    case "$cleanup_target" in
      "$state_root"/.operation.*) rm -rf --one-file-system -- "$cleanup_target" ;;
      *) printf '%s\n' "Refused to remove an unexpected rollout path." >&2 ;;
    esac
  fi
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status|--dry-run|--promote|--recycle-promote|--rollback)
      [ -z "$operation" ] || fail "choose exactly one operation"
      operation="${1#--}"
      shift
      ;;
    --expected-control-plane-revision)
      expected_revision="$2"; shift 2 ;;
    --expected-worker-image)
      expected_worker_image="$2"; shift 2 ;;
    --expected-worker-source-revision)
      expected_worker_source_revision="$2"; shift 2 ;;
    *) fail "unknown argument" ;;
  esac
done

case "$operation" in
  status|dry-run|promote|recycle-promote|rollback) ;;
  *) fail "choose one operation" ;;
esac
[[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] || fail "exact control-plane revision is required"
if [ "$operation" != "rollback" ]; then
  [[ "$expected_worker_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/i2v-worker@sha256:[0-9a-f]{64}$ ]] ||
    fail "exact immutable I2V worker image is required"
  [[ "$expected_worker_source_revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "exact I2V worker source revision is required"
fi

for command in curl docker flock python3 timeout; do command -v "$command" >/dev/null || fail "$command is required"; done
require_private_root_file "$controller_env"
require_private_root_file "$deploy_env"
[ -f "$compose_file" ] || fail "missing deployed compose file"
install -d -o root -g root -m 0700 "$state_root"
exec 9>"$lock_file"
flock --exclusive --wait 120 9 || fail "another control-plane update holds the lock"

initial_container="$(control_plane_container_id)"
[ "$(container_revision "$initial_container")" = "$expected_revision" ] ||
  fail "running control-plane revision differs from the reviewed revision"
curl --fail --silent --show-error --max-time 5 \
  http://127.0.0.1:8000/api/v1/health/ready >/dev/null

if [ "$operation" = "rollback" ]; then
  [ -z "$expected_worker_image$expected_worker_source_revision" ] ||
    fail "rollback accepts no new worker coordinates"
  [ -d "$active_state" ] || fail "no preserved reviewed-worker rollback bundle exists"
  require_private_root_file "$active_state/original.env"
  require_private_root_file "$active_state/provider-rollback.json"
fi

if [ "$operation" = "status" ]; then
  timeout --signal=TERM --kill-after=10s 900s \
    /usr/bin/docker exec "$initial_container" python3.12 -m \
    gen_automation.i2v_lora_rollout_cli status
  exit 0
fi

work_dir="$(mktemp -d "$state_root/.operation.XXXXXX")"
chown 10001:10001 "$work_dir"
chmod 0700 "$work_dir"
original_env="$work_dir/original.env"
current_env="$work_dir/current.env"
maintenance_env="$work_dir/maintenance.env"
provider_state="$work_dir/provider-rollback.json"
provider_marker="$work_dir/provider-mutation-attempted.json"
render_profile "$controller_env" "$original_env" normalize
chown 10001:10001 "$original_env"
render_profile "$original_env" "$maintenance_env" maintenance
chown 10001:10001 "$maintenance_env"

if [ "$operation" = "dry-run" ]; then
  run_one_off "$original_env" "$initial_container" 900s dry-run \
    --expected-worker-image "$expected_worker_image" \
    --expected-worker-source-revision "$expected_worker_source_revision" \
    --expected-private-manifest-bucket "$manifest_bucket" \
    --expected-private-manifest-key "$manifest_key" \
    --expected-private-manifest-version "$manifest_version" \
    --expected-private-manifest-source-sha256 "$manifest_source_sha256" \
    --prepared-host-env-output /run/i2v-lora-rollout/target.patch
  exit 0
fi

if [ "$operation" = "rollback" ]; then
  # Refuse before maintenance when active work exists, then restore only the
  # bounded I2V profile into today's environment so unrelated later settings
  # and secret-reference rotations remain intact.
  status_json="$(run_one_off "$controller_env" "$initial_container" 900s status)"
  printf '%s' "$status_json" | assert_zero_status ||
    fail "rollback requires zero active I2V work"
  render_profile "$controller_env" "$current_env" normalize
  chown 10001:10001 "$current_env"
  resume_env="$current_env"
  merge_saved_i2v_profile "$current_env" "$active_state/original.env" "$original_env"
  chown 10001:10001 "$original_env"
  install -o 10001 -g 10001 -m 0600 "$active_state/provider-rollback.json" "$provider_state"
  render_profile "$controller_env" "$maintenance_env" maintenance
  chown 10001:10001 "$maintenance_env"
  rollback_armed=1
  maintenance_id="$(restart_into "$maintenance_env" "$expected_revision")"
  assert_container_flags "$maintenance_id" false false false false
  if ! run_one_off "$original_env" "$maintenance_id" 1900s rollback \
    --rollback-state-input /run/i2v-lora-rollout/provider-rollback.json \
    --provider-mutation-marker-output \
      /run/i2v-lora-rollout/provider-mutation-attempted.json; then
    [ -f "$provider_marker" ] && provider_rollback_needed=1 || provider_rollback_needed=0
    false
  fi
  provider_rollback_needed=0
  resume_env="$original_env"
  restart_into "$original_env" "$expected_revision" >/dev/null
  rollback_armed=0
  mv -- "$active_state" "$state_root/rolled-back-$(date -u +%Y%m%dT%H%M%SZ)"
  printf '%s\n' "I2V LoRA worker rollback completed."
  exit 0
fi

[ ! -e "$active_state" ] || fail "a prior reviewed-worker rollback bundle already exists"
resume_env="$original_env"
# Prove the operation is rollout-ready before the serving controller is put
# into maintenance. Promotion repeats the same guards immediately before PATCH.
if [ "$operation" = "promote" ]; then
  run_one_off "$original_env" "$initial_container" 900s dry-run \
    --expected-worker-image "$expected_worker_image" \
    --expected-worker-source-revision "$expected_worker_source_revision" \
    --expected-private-manifest-bucket "$manifest_bucket" \
    --expected-private-manifest-key "$manifest_key" \
    --expected-private-manifest-version "$manifest_version" \
    --expected-private-manifest-source-sha256 "$manifest_source_sha256" \
    --prepared-host-env-output /run/i2v-lora-rollout/target.patch >/dev/null
fi
rollback_armed=1
maintenance_id="$(restart_into "$maintenance_env" "$expected_revision")"
assert_container_flags "$maintenance_id" false false false false

# The one-off receives the exact pre-maintenance profile for safe baseline or
# capable-to-capable rollback, while the serving controller stays frozen. The
# recycle-promote variant prepares its complete target before STOP and performs
# STOP -> PATCH while stopped -> START -> Ready inside this single invocation.
if ! run_one_off "$original_env" "$maintenance_id" 10000s "$operation" \
  --expected-worker-image "$expected_worker_image" \
  --expected-worker-source-revision "$expected_worker_source_revision" \
  --expected-private-manifest-bucket "$manifest_bucket" \
  --expected-private-manifest-key "$manifest_key" \
  --expected-private-manifest-version "$manifest_version" \
  --expected-private-manifest-source-sha256 "$manifest_source_sha256" \
  --prepared-host-env-output /run/i2v-lora-rollout/target.patch \
  --rollback-state-output /run/i2v-lora-rollout/provider-rollback.json \
  --provider-mutation-marker-output \
    /run/i2v-lora-rollout/provider-mutation-attempted.json; then
  [ -f "$provider_marker" ] && provider_rollback_needed=1
  false
fi
provider_rollback_needed=1

target_env="$work_dir/target.env"
render_profile "$original_env" "$target_env" target "$work_dir/target.patch"
chown root:root "$target_env"
chmod 0600 "$target_env"
run_one_off "$target_env" "$maintenance_id" 900s profile-preflight \
  --expected-worker-image "$expected_worker_image" \
  --expected-worker-source-revision "$expected_worker_source_revision" \
  --expected-public-profile false >/dev/null

# Persist a root-only rollback bundle before the target scheduler resumes, but
# retain the user-private operation copy so EXIT recovery can still traverse
# and update its marker if the target restart fails.
active_temporary="$(mktemp -d "$state_root/.active.XXXXXX")"
for name in original.env maintenance.env target.env provider-rollback.json; do
  install -o root -g root -m 0600 "$work_dir/$name" "$active_temporary/$name"
done
chmod 0700 "$active_temporary"
mv -- "$active_temporary" "$active_state"
active_persisted=1
target_container="$(restart_into "$target_env" "$expected_revision")"
assert_container_flags "$target_container" true true true false
provider_rollback_needed=0
rollback_armed=0
rm -rf --one-file-system -- "$work_dir"
work_dir=""
printf '%s\n' "Reviewed I2V LoRA worker is ready; public LoRA selection remains disabled."
