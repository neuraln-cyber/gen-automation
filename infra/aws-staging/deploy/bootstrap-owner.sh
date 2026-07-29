#!/usr/bin/env bash
set -euo pipefail

config_root="/etc/gen-automation"
ca_path="$config_root/rds-global-bundle.pem"

[ -t 0 ] && [ -t 1 ] || {
  printf '%s\n' "Owner bootstrap requires an interactive terminal." >&2
  exit 2
}

/usr/local/libexec/gen-automation-validate-deployment

image="$(
  sed -n \
    's/^GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE=//p' \
    "$config_root/deploy.env"
)"
[[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  printf '%s\n' "The control-plane image is not an immutable digest." >&2
  exit 1
}

exec /usr/bin/docker run \
  --rm \
  --interactive \
  --tty \
  --network host \
  --user 10001:10001 \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m,uid=10001,gid=10001,mode=1770 \
  --env-file "$config_root/bootstrap-owner.env" \
  --mount "type=bind,src=${ca_path},dst=/run/gen-automation/rds-global-bundle.pem,readonly" \
  "$image" \
  python3.12 -m gen_automation.cli bootstrap-owner
