#!/usr/bin/env bash
set -Eeuo pipefail

env_file="/etc/gen-automation/control-plane.env"
deploy_env="/etc/gen-automation/deploy.env"
compose_file="/opt/gen-automation/deploy/compose.yaml"
service_name="gen-automation-staging.service"
lock_file="/run/lock/gen-automation-control-plane-update.lock"
active_root="/var/lib/gen-automation/i2v-runpod-cutover/active"
original_env="$active_root/control-plane.env"
marker="$active_root/state.json"
preseed_state="/var/lib/gen-automation/runpod-i2v/preseed-state.json"
ssm_parameter="/gen-automation-staging/runpod/inference-api-key"
aws_region="eu-central-1"

operation=""
runpod_mode=""
endpoint_id=""
network_volume_id=""
worker_image=""
worker_source_revision=""
rollback_armed=0

fail() {
  printf '%s\n' "I2V RunPod cutover failed: $*" >&2
  return 1
}

rewrite_env() {
  local mode="$1"
  local temporary
  temporary="$(mktemp "${env_file}.runpod.XXXXXX")"
  chmod 0600 "$temporary"
  chown --reference="$env_file" "$temporary"
  CUTOVER_MODE="$mode" \
  CUTOVER_RUNPOD_MODE="$runpod_mode" \
  CUTOVER_ENDPOINT_ID="$endpoint_id" \
  CUTOVER_NETWORK_VOLUME_ID="$network_volume_id" \
  CUTOVER_WORKER_IMAGE="$worker_image" \
  CUTOVER_WORKER_SOURCE_REVISION="$worker_source_revision" \
  CUTOVER_RUNPOD_KEY="${CUTOVER_RUNPOD_KEY:-}" \
  python3 - "$env_file" "$temporary" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
mode = os.environ["CUTOVER_MODE"]
runpod_mode = os.environ["CUTOVER_RUNPOD_MODE"] or "pod"
endpoint_id = os.environ["CUTOVER_ENDPOINT_ID"]
values = {
    "GEN_AUTOMATION_I2V_ENABLED": "false",
    "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED": "false",
    "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "false",
    "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "false",
    "GEN_AUTOMATION_I2V_RUNPOD_ENABLED": "false",
    "GEN_AUTOMATION_I2V_RUNPOD_MODE": "pod",
    "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID": "",
    "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID": "",
}
if mode == "enable":
    if runpod_mode not in {"pod", "serverless"}:
        raise SystemExit("invalid RunPod mode")
    if runpod_mode == "serverless" and not endpoint_id:
        raise SystemExit("missing RunPod Serverless endpoint ID")
    if runpod_mode == "pod" and endpoint_id:
        raise SystemExit("Pod mode must not configure a Serverless endpoint ID")
    public_base = None
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEN_AUTOMATION_PUBLIC_BASE_URL="):
            if public_base is not None:
                raise SystemExit("duplicate public base URL")
            public_base = line.partition("=")[2].rstrip("/")
    if not public_base or not public_base.startswith("https://"):
        raise SystemExit("invalid public base URL")
    values.update(
        {
            "GEN_AUTOMATION_I2V_ENABLED": "true",
            "GEN_AUTOMATION_I2V_HIRES_PROFILE_ENABLED": "true",
            "GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED": "true",
            "GEN_AUTOMATION_I2V_LORA_PROFILE_ENABLED": "true",
            "GEN_AUTOMATION_I2V_RUNPOD_ENABLED": "true",
            "GEN_AUTOMATION_I2V_RUNPOD_MODE": runpod_mode,
            "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID": endpoint_id,
            "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID": os.environ[
                "CUTOVER_NETWORK_VOLUME_ID"
            ],
            "GEN_AUTOMATION_I2V_WORKER_IMAGE": os.environ["CUTOVER_WORKER_IMAGE"],
            "GEN_AUTOMATION_I2V_WORKER_SOURCE_REVISION": os.environ[
                "CUTOVER_WORKER_SOURCE_REVISION"
            ],
            "GEN_AUTOMATION_I2V_RUNPOD_API_KEY": os.environ["CUTOVER_RUNPOD_KEY"],
            "GEN_AUTOMATION_I2V_RUNPOD_CLAIM_URL": (
                public_base + "/api/v1/i2v/runpod/claim"
            ),
        }
    )
optional_empty = {
    "GEN_AUTOMATION_I2V_RUNPOD_ENDPOINT_ID",
    "GEN_AUTOMATION_I2V_RUNPOD_NETWORK_VOLUME_ID",
}
for key, value in values.items():
    if (not value and key not in optional_empty) or any(
        character in value for character in "\r\n\0"
    ):
        raise SystemExit("invalid environment value")

lines = source.read_text(encoding="utf-8").splitlines()
seen = {key: 0 for key in values}
result = []
for line in lines:
    key, separator, _ = line.partition("=")
    if separator and key in values:
        seen[key] += 1
        if seen[key] > 1:
            raise SystemExit(f"duplicate environment key: {key}")
        result.append(f"{key}={values[key]}")
    else:
        result.append(line)
for key, count in seen.items():
    if count == 0:
        result.append(f"{key}={values[key]}")
target.write_text("\n".join(result) + "\n", encoding="utf-8")
PY
  mv -- "$temporary" "$env_file"
}

wait_for_control_plane() {
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

restart_control_plane() {
  /usr/local/libexec/gen-automation-validate-deployment
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    -f "$compose_file" \
    config --quiet
  systemctl restart --no-block "$service_name"
  wait_for_control_plane || fail "control plane did not become ready"
}

verify_runpod_provider() {
  CUTOVER_RUNPOD_MODE="$runpod_mode" \
  CUTOVER_ENDPOINT_ID="$endpoint_id" \
  CUTOVER_NETWORK_VOLUME_ID="$network_volume_id" \
  CUTOVER_WORKER_IMAGE="$worker_image" \
  CUTOVER_WORKER_SOURCE_REVISION="$worker_source_revision" \
  CUTOVER_RUNPOD_KEY="$CUTOVER_RUNPOD_KEY" \
  CUTOVER_PRESEED_VOLUME_ID="$PRESEED_VOLUME_ID" \
  python3 - <<'PY'
import json
import os
import urllib.parse
import urllib.request

provider_mode = os.environ["CUTOVER_RUNPOD_MODE"]
endpoint_id = os.environ["CUTOVER_ENDPOINT_ID"]
volume_id = os.environ["CUTOVER_NETWORK_VOLUME_ID"]
worker_image = os.environ["CUTOVER_WORKER_IMAGE"]
worker_source_revision = os.environ["CUTOVER_WORKER_SOURCE_REVISION"]
api_key = os.environ["CUTOVER_RUNPOD_KEY"]
preseed_volume_id = os.environ["CUTOVER_PRESEED_VOLUME_ID"]
try:
    def get_json(url: str) -> object:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read(64 * 1024))

    if volume_id != preseed_volume_id:
        raise ValueError
    volumes = get_json("https://rest.runpod.io/v1/networkvolumes")
    if not isinstance(volumes, list):
        raise ValueError
    matches = [item for item in volumes if isinstance(item, dict) and item.get("id") == volume_id]
    if len(matches) != 1:
        raise ValueError
    volume = matches[0]
    if volume.get("dataCenterId") != "EU-RO-1":
        raise ValueError
    if not isinstance(volume.get("size"), int) or volume["size"] < 40:
        raise ValueError
    pods = get_json("https://rest.runpod.io/v1/pods?computeType=GPU")
    if not isinstance(pods, list):
        raise ValueError
    if any(
        isinstance(pod, dict)
        and isinstance(pod.get("name"), str)
        and pod["name"].startswith("gen-automation-i2v-")
        and (
            pod.get("networkVolumeId") == volume_id
            or (
                isinstance(pod.get("networkVolume"), dict)
                and pod["networkVolume"].get("id") == volume_id
            )
        )
        for pod in pods
    ):
        raise ValueError
    if provider_mode == "serverless":
        endpoint = get_json(
            "https://rest.runpod.io/v1/endpoints/"
            + urllib.parse.quote(endpoint_id, safe="")
        )
        if not isinstance(endpoint, dict) or endpoint.get("id") != endpoint_id:
            raise ValueError
        endpoint_volumes = endpoint.get("networkVolumeIds")
        if endpoint.get("networkVolumeId") != volume_id and endpoint_volumes != [volume_id]:
            raise ValueError
        if (
            endpoint.get("name") != "gen-automation-i2v-staging"
            or endpoint.get("workersMin") != 0
            or endpoint.get("workersMax") != 1
            or endpoint.get("flashboot") is not True
        ):
            raise ValueError
        template_id = endpoint.get("templateId")
        if not isinstance(template_id, str) or not template_id:
            raise ValueError
        template = get_json(
            "https://rest.runpod.io/v1/templates/"
            + urllib.parse.quote(template_id, safe="")
        )
        environment = template.get("env") if isinstance(template, dict) else None
        if (
            not isinstance(template, dict)
            or template.get("imageName") != worker_image
            or not isinstance(environment, dict)
            or environment.get("GEN_I2V_WORKER_SOURCE_REVISION") != worker_source_revision
            or environment.get("GEN_I2V_WORKER_VOLUME_ROOT") != "/runpod-volume"
            or environment.get("GEN_I2V_WORKER_LORA_WORKER_ENABLED") != "true"
        ):
            raise ValueError
    elif provider_mode != "pod" or endpoint_id:
        raise ValueError
except Exception:
    raise SystemExit("RunPod provider verification failed") from None
PY
}

verify_preseed_identity() {
  [ -f "$preseed_state" ] && [ ! -L "$preseed_state" ] ||
    fail "RunPod volume preseed proof is unavailable"
  [ "$(stat -c '%u:%g:%a' "$preseed_state")" = "0:0:600" ] ||
    fail "RunPod volume preseed proof permissions are invalid"
  local image
  image="$(grep '^GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE=' "$deploy_env" | cut -d= -f2-)"
  [[ "$image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
    fail "deployed control-plane image is not an immutable official digest"
  /usr/bin/docker run --rm --init --interactive --read-only \
    --user 0:0 \
    --cap-drop ALL \
    --cap-add DAC_READ_SEARCH \
    --security-opt no-new-privileges:true \
    --pids-limit 128 \
    --env-file "$env_file" \
    --env GEN_AUTOMATION_I2V_ENABLED=true \
    --env GEN_AUTOMATION_I2V_LORA_WORKER_ENABLED=true \
    --volume "$preseed_state:/run/preseed-state.json:ro" \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,uid=10001,gid=10001,mode=1770 \
    --entrypoint python3.12 \
    "$image" - <<'PY'
import json
import pathlib

from gen_automation.config import Settings
from gen_automation.services.i2v_environment import i2v_worker_identity

state = json.loads(pathlib.Path("/run/preseed-state.json").read_text(encoding="utf-8"))
objects_sha, artifact_sha = i2v_worker_identity(Settings())
volume_id = state.get("network_volume_id")
if (
    state.get("schema") != "gen-automation/i2v-runpod-preseed-state/v1"
    or state.get("status") != "ready"
    or state.get("model_objects_sha256") != objects_sha
    or state.get("artifact_identity_sha256") != artifact_sha
    or not isinstance(volume_id, str)
    or not volume_id
):
    raise SystemExit(1)
print(volume_id)
PY
}

assert_zero_i2v_work() {
  local image
  image="$(grep '^GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE=' "$deploy_env" | cut -d= -f2-)"
  [[ "$image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
    fail "deployed control-plane image is not an immutable official digest"
  /usr/bin/docker run --rm --init --interactive --read-only --network host \
    --user 10001:10001 \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --pids-limit 128 \
    --env-file "$env_file" \
    --volume /etc/gen-automation/rds-global-bundle.pem:/run/gen-automation/rds-global-bundle.pem:ro \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m,uid=10001,gid=10001,mode=1770 \
    --entrypoint python3.12 \
    "$image" - <<'PY'
import asyncio
from sqlalchemy import func, select
from gen_automation.config import Settings
from gen_automation.db.models import I2VAttempt, I2VJob
from gen_automation.db.session import Database
from gen_automation.domain.i2v import I2VAttemptState, I2VJobState

async def main() -> None:
    settings = Settings()
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            jobs = await session.scalar(
                select(func.count()).select_from(I2VJob).where(
                    I2VJob.state.in_((I2VJobState.QUEUED, I2VJobState.CLAIMED,
                                      I2VJobState.RUNNING, I2VJobState.CANCEL_REQUESTED))
                )
            )
            attempts = await session.scalar(
                select(func.count()).select_from(I2VAttempt).where(
                    I2VAttempt.state.in_((I2VAttemptState.CREATED, I2VAttemptState.RUNNING))
                )
            )
        if int(jobs or 0) != 0 or int(attempts or 0) != 0:
            raise SystemExit(1)
    finally:
        await database.dispose()

asyncio.run(main())
PY
}

restore_original() {
  [ -f "$original_env" ] || return 1
  local temporary
  temporary="$(mktemp "${env_file}.rollback.XXXXXX")"
  cp -- "$original_env" "$temporary"
  chmod --reference="$env_file" "$temporary"
  chown --reference="$env_file" "$temporary"
  mv -- "$temporary" "$env_file"
  restart_control_plane
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    printf '%s\n' "Cutover failed; restoring the prior provider configuration." >&2
    if restore_original; then
      rm -f -- "$marker" "$original_env"
      rmdir -- "$active_root" ||
        printf '%s\n' "Automatic rollback cleanup requires operator attention." >&2
    else
      printf '%s\n' "Automatic rollback requires operator attention." >&2
    fi
  fi
  unset CUTOVER_RUNPOD_KEY
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
[ "$#" -ge 1 ] ||
  fail "usage: $0 --enable --network-volume-id <id> --worker-image <ref> --worker-source-revision <sha> | --enable-serverless --endpoint-id <id> --network-volume-id <id> --worker-image <ref> --worker-source-revision <sha> | --rollback"
case "$1" in
  --enable)
    [ "$#" -eq 7 ] && [ "$2" = "--network-volume-id" ] &&
      [ "$4" = "--worker-image" ] && [ "$6" = "--worker-source-revision" ] ||
      fail "enable requires exact network volume, worker image, and worker source"
    operation="enable"
    runpod_mode="pod"
    network_volume_id="$3"
    worker_image="$5"
    worker_source_revision="$7"
    [[ "$network_volume_id" =~ ^[A-Za-z0-9_-]{3,128}$ ]] ||
      fail "invalid network volume ID"
    [[ "$worker_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/i2v-worker@sha256:[0-9a-f]{64}$ ]] ||
      fail "invalid immutable I2V worker image"
    [[ "$worker_source_revision" =~ ^[0-9a-f]{40}$ ]] ||
      fail "invalid I2V worker source revision"
    ;;
  --enable-serverless)
    [ "$#" -eq 9 ] && [ "$2" = "--endpoint-id" ] &&
      [ "$4" = "--network-volume-id" ] && [ "$6" = "--worker-image" ] &&
      [ "$8" = "--worker-source-revision" ] ||
      fail "serverless enable requires exact endpoint, network volume, worker image, and worker source"
    operation="enable"
    runpod_mode="serverless"
    endpoint_id="$3"
    network_volume_id="$5"
    worker_image="$7"
    worker_source_revision="$9"
    [[ "$endpoint_id" =~ ^[A-Za-z0-9_-]{3,128}$ ]] ||
      fail "invalid RunPod Serverless endpoint ID"
    [[ "$network_volume_id" =~ ^[A-Za-z0-9_-]{3,128}$ ]] ||
      fail "invalid network volume ID"
    [[ "$worker_image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/i2v-worker@sha256:[0-9a-f]{64}$ ]] ||
      fail "invalid immutable I2V worker image"
    [[ "$worker_source_revision" =~ ^[0-9a-f]{40}$ ]] ||
      fail "invalid I2V worker source revision"
    ;;
  --rollback)
    [ "$#" -eq 1 ] || fail "rollback accepts no additional arguments"
    operation="rollback"
    ;;
  *) fail "unknown operation" ;;
esac

for command in aws curl docker flock python3; do
  command -v "$command" >/dev/null || fail "$command is required"
done
[ -f "$env_file" ] || fail "control-plane environment is unavailable"
install -d -o root -g root -m 0700 "$(dirname "$active_root")"
exec 9>"$lock_file"
flock --exclusive --wait 120 9 || fail "another control-plane update holds the lock"

if [ "$operation" = "rollback" ]; then
  [ -f "$marker" ] && [ -f "$original_env" ] || fail "no active RunPod cutover exists"
  python3 - "$marker" "$env_file" <<'PY'
import hashlib
import json
import pathlib
import sys

marker = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
actual = hashlib.sha256(pathlib.Path(sys.argv[2]).read_bytes()).hexdigest()
if marker.get("schema") != "gen-automation/i2v-runpod-cutover/v1" or marker.get(
    "cutover_env_sha256"
) != actual:
    raise SystemExit(1)
PY
  restore_original
  rm -f -- "$marker" "$original_env"
  rmdir -- "$active_root"
  printf '%s\n' "I2V provider rollback completed."
  exit 0
fi

[ ! -e "$active_root" ] || fail "an active provider cutover already exists"
CUTOVER_RUNPOD_KEY="$(
  aws ssm get-parameter --region "$aws_region" --name "$ssm_parameter" \
    --with-decryption --query Parameter.Value --output text
)"
[[ "$CUTOVER_RUNPOD_KEY" =~ ^[A-Za-z0-9._-]{20,512}$ ]] || fail "RunPod API key is unavailable"
export CUTOVER_RUNPOD_KEY
PRESEED_VOLUME_ID="$(verify_preseed_identity)"
[[ "$PRESEED_VOLUME_ID" =~ ^[A-Za-z0-9_-]{3,128}$ ]] ||
  fail "RunPod volume preseed proof is invalid"
export PRESEED_VOLUME_ID
verify_runpod_provider

install -d -o root -g root -m 0700 "$active_root"
cp -- "$env_file" "$original_env"
chmod 0600 "$original_env"
chown root:root "$original_env"
rollback_armed=1

rewrite_env freeze
restart_control_plane
assert_zero_i2v_work || fail "I2V queue or attempt work is not zero"

rewrite_env enable
unset CUTOVER_RUNPOD_KEY
restart_control_plane

python3 - "$marker" "$runpod_mode" "$endpoint_id" "$network_volume_id" "$worker_image" \
  "$worker_source_revision" "$env_file" <<'PY'
import hashlib
import json
import os
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
environment = pathlib.Path(sys.argv[7]).read_bytes()
payload = {
    "schema": "gen-automation/i2v-runpod-cutover/v1",
    "runpod_mode": sys.argv[2],
    "endpoint_id": sys.argv[3] or None,
    "network_volume_id": sys.argv[4],
    "worker_image": sys.argv[5],
    "worker_source_revision": sys.argv[6],
    "cutover_env_sha256": hashlib.sha256(environment).hexdigest(),
}
path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
os.chmod(path, 0o600)
PY
chown root:root "$marker"
rollback_armed=0
printf '%s\n' "I2V RunPod cutover completed."
