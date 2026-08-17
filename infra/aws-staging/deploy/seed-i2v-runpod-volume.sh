#!/usr/bin/env bash
set -Eeuo pipefail

deploy_env="/etc/gen-automation/deploy.env"
compose_file="/opt/gen-automation/deploy/compose.yaml"
state_root="/var/lib/gen-automation/runpod-i2v"
state_file=""
active_state_file="$state_root/preseed-state.json"
lock_file="/run/lock/gen-automation-i2v-runpod-seed.lock"
access_parameter="/gen-automation-staging/runpod/s3-access-key-id"
secret_parameter="/gen-automation-staging/runpod/s3-secret-access-key"
aws_region="eu-central-1"

volume_id=""
datacenter="EU-RO-1"
source_runpod_volume_id=""
source_runpod_datacenter=""
work_root=""
credentials_file=""
model_objects_file=""
RUNPOD_S3_ACCESS_KEY_ID=""
RUNPOD_S3_SECRET_ACCESS_KEY=""

fail() {
  printf '%s\n' "I2V RunPod volume preseed failed: $*" >&2
  return 1
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  unset RUNPOD_S3_ACCESS_KEY_ID RUNPOD_S3_SECRET_ACCESS_KEY
  if [ -n "$work_root" ] && [[ "$work_root" == /run/gen-automation-runpod-seed.* ]]; then
    rm -rf -- "$work_root"
  fi
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --network-volume-id)
      [ "$#" -ge 2 ] || fail "missing network volume ID"
      volume_id="$2"
      shift 2
      ;;
    --datacenter)
      [ "$#" -ge 2 ] || fail "missing RunPod datacenter"
      datacenter="$2"
      shift 2
      ;;
    --source-runpod-volume-id)
      [ "$#" -ge 2 ] || fail "missing source RunPod volume ID"
      source_runpod_volume_id="$2"
      shift 2
      ;;
    --source-runpod-datacenter)
      [ "$#" -ge 2 ] || fail "missing source RunPod datacenter"
      source_runpod_datacenter="$2"
      shift 2
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "$volume_id" =~ ^[A-Za-z0-9_-]{3,128}$ ]] || fail "invalid network volume ID"
[[ "$datacenter" =~ ^[A-Z]{2,4}-[A-Z]{2,4}-[0-9]$ ]] || fail "invalid RunPod datacenter"
if [ -n "$source_runpod_volume_id" ] || [ -n "$source_runpod_datacenter" ]; then
  [[ "$source_runpod_volume_id" =~ ^[A-Za-z0-9_-]{3,128}$ ]] ||
    fail "invalid source RunPod volume ID"
  [[ "$source_runpod_datacenter" =~ ^[A-Z]{2,4}-[A-Z]{2,4}-[0-9]$ ]] ||
    fail "invalid source RunPod datacenter"
  [ "$source_runpod_volume_id" != "$volume_id" ] ||
    fail "source and destination RunPod volumes must differ"
fi
state_file="$state_root/preseed-state-$volume_id.json"
for command in aws docker flock mktemp python3; do
  command -v "$command" >/dev/null || fail "$command is required"
done
[ -f "$deploy_env" ] || fail "deployment environment is unavailable"
[ -f "$compose_file" ] || fail "deployment compose file is unavailable"

install -d -o root -g root -m 0700 "$state_root"
exec 9>"$lock_file"
flock --exclusive --wait 120 9 || fail "another RunPod volume preseed is active"

container_id="$(
  /usr/bin/docker compose --env-file "$deploy_env" --file "$compose_file" \
    ps --status running --quiet control-plane-mega 2>/dev/null || true
)"
[[ "$container_id" =~ ^[0-9a-f]{12,64}$ ]] || fail "control plane is not running"
image_ref="$(grep '^GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE=' "$deploy_env" | cut -d= -f2-)"
[[ "$image_ref" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
  fail "deployed control-plane image is not an immutable official digest"
running_image="$(docker inspect --format '{{.Config.Image}}' "$container_id")"
[ "$running_image" = "$image_ref" ] || fail "running control plane image drifted"
running_state="$(docker inspect --format '{{.State.Running}}' "$container_id")"
[ "$running_state" = "true" ] || fail "control plane is not running"

work_root="$(mktemp -d /run/gen-automation-runpod-seed.XXXXXX)"
chmod 0700 "$work_root"
model_objects_file="$work_root/model-objects.json"
credentials_file="$work_root/runpod-s3.env"

/usr/bin/docker exec --interactive \
  --env GEN_AUTOMATION_I2V_ENABLED=true \
  --env GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true \
  "$container_id" python3.12 - >"$model_objects_file" <<'PY'
import json
from gen_automation.config import Settings
from gen_automation.services.i2v_environment import i2v_worker_model_objects

models = i2v_worker_model_objects(Settings())
if len(models) != 14:
    raise SystemExit("the complete I2V model and LoRA artifact set is unavailable")
json.dump(
    [model.model_dump(mode="json") for model in models],
    __import__("sys").stdout,
    ensure_ascii=True,
    separators=(",", ":"),
    sort_keys=True,
)
PY
chmod 0640 "$model_objects_file"
chown root:10001 "$model_objects_file"
python3 - "$model_objects_file" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(value, list) or len(value) != 14:
    raise SystemExit(1)
PY

RUNPOD_S3_ACCESS_KEY_ID="$(
  aws ssm get-parameter --region "$aws_region" --name "$access_parameter" \
    --with-decryption --query Parameter.Value --output text
)"
RUNPOD_S3_SECRET_ACCESS_KEY="$(
  aws ssm get-parameter --region "$aws_region" --name "$secret_parameter" \
    --with-decryption --query Parameter.Value --output text
)"
[[ "$RUNPOD_S3_ACCESS_KEY_ID" =~ ^user_[A-Za-z0-9]+$ ]] ||
  fail "RunPod S3 access key ID is unavailable"
[[ "$RUNPOD_S3_SECRET_ACCESS_KEY" =~ ^rps_[A-Za-z0-9_-]{16,512}$ ]] ||
  fail "RunPod S3 secret access key is unavailable"
printf 'RUNPOD_S3_ACCESS_KEY_ID=%s\nRUNPOD_S3_SECRET_ACCESS_KEY=%s\nGEN_AUTOMATION_RUNPOD_I2V_SEED_ALLOWED=true\n' \
  "$RUNPOD_S3_ACCESS_KEY_ID" "$RUNPOD_S3_SECRET_ACCESS_KEY" >"$credentials_file"
chmod 0600 "$credentials_file"
chown root:root "$credentials_file"
unset RUNPOD_S3_ACCESS_KEY_ID RUNPOD_S3_SECRET_ACCESS_KEY

chown 10001:10001 "$state_root"
chmod 0700 "$state_root"
seed_args=(
  /app/scripts/runpod_i2v_seed_volume.py apply
  --model-objects-file /run/seed/model-objects.json
  --network-volume-id "$volume_id"
  --state-file "/run/state/$(basename "$state_file")"
  --datacenter "$datacenter"
  --endpoint "https://s3api-${datacenter,,}.runpod.io/"
  --acknowledge-upload
)
if [ -n "$source_runpod_volume_id" ]; then
  seed_args+=(
    --source-runpod-volume-id "$source_runpod_volume_id"
    --source-runpod-datacenter "$source_runpod_datacenter"
    --source-runpod-endpoint "https://s3api-${source_runpod_datacenter,,}.runpod.io/"
  )
fi
/usr/bin/docker run --rm --init --read-only --network host \
  --user 10001:10001 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --pids-limit 128 \
  --env-file "$credentials_file" \
  --volume "$model_objects_file:/run/seed/model-objects.json:ro" \
  --volume "$state_root:/run/state:rw" \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m,uid=10001,gid=10001,mode=1770 \
  --entrypoint python3.12 \
  "$image_ref" \
  "${seed_args[@]}"
state_temporary="$state_root/.preseed-state.json.partial"
install -o root -g root -m 0600 "$state_file" "$state_temporary"
mv -f -- "$state_temporary" "$active_state_file"
chown root:root "$state_root" "$state_file" "$active_state_file"
chmod 0700 "$state_root"
chmod 0600 "$state_file" "$active_state_file"

rm -f -- "$credentials_file" "$model_objects_file"
printf '%s\n' "I2V RunPod network volume preseed completed."
