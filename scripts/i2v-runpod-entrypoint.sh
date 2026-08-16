#!/usr/bin/env bash
set -Eeuo pipefail

volume_root="/runpod-volume"
namespace="$volume_root/gen-automation"
worker_uid="10002"
worker_gid="10002"

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "RunPod I2V entrypoint requires its initial root setup." >&2
  exit 1
}
[ -d "$volume_root" ] || {
  printf '%s\n' "RunPod network volume is not mounted." >&2
  exit 1
}
[ "$(stat -c '%d' /)" != "$(stat -c '%d' "$volume_root")" ] || {
  printf '%s\n' "RunPod network volume mount is unavailable." >&2
  exit 1
}

install -d -o "$worker_uid" -g "$worker_gid" -m 0700 "$namespace"
find "$namespace" -xdev \( ! -user "$worker_uid" -o ! -group "$worker_gid" \) \
  -exec chown "$worker_uid:$worker_gid" {} +
find "$namespace" -xdev -type d -exec chmod 0700 {} +

exec setpriv \
  --reuid "$worker_uid" \
  --regid "$worker_gid" \
  --init-groups \
  --no-new-privs \
  /opt/i2v-venv/bin/python -m gen_automation.i2v_worker.runpod_handler
