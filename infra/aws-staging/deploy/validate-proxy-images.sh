#!/usr/bin/env bash
set -euo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"

fail() {
  printf '%s\n' "proxy image validation failed: $*" >&2
  exit 1
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

for image_key in GEN_AUTOMATION_CADDY_IMAGE GEN_AUTOMATION_NGINX_IMAGE; do
  image="$(env_value "$image_key" "$config_root/deploy.env")"
  [[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] ||
    fail "$image_key must be an immutable repository@sha256:64-hex reference"
done
caddy_image="$(env_value GEN_AUTOMATION_CADDY_IMAGE "$config_root/deploy.env")"
nginx_image="$(env_value GEN_AUTOMATION_NGINX_IMAGE "$config_root/deploy.env")"

/usr/bin/docker run \
  --rm \
  --network none \
  --read-only \
  --user 10002:10002 \
  --cap-drop ALL \
  --cap-add NET_BIND_SERVICE \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,uid=10002,gid=10002,mode=1770 \
  --tmpfs /data:rw,nosuid,nodev,noexec,size=64m,uid=10002,gid=10002,mode=0700 \
  --tmpfs /config:rw,nosuid,nodev,noexec,size=16m,uid=10002,gid=10002,mode=0700 \
  --env-file "$config_root/caddy.env" \
  --mount "type=bind,src=${deploy_root}/Caddyfile,dst=/etc/caddy/Caddyfile,readonly" \
  --entrypoint caddy \
  "$caddy_image" \
  validate --config /etc/caddy/Caddyfile --adapter caddyfile ||
  fail "Caddyfile did not validate with the immutable Caddy image"

caddy_probe="gen-automation-caddy-capability-probe-$$"
cleanup_caddy_probe() {
  /usr/bin/docker rm --force "$caddy_probe" >/dev/null 2>&1 || true
}
trap cleanup_caddy_probe EXIT
/usr/bin/docker run \
  --detach \
  --name "$caddy_probe" \
  --network none \
  --read-only \
  --user 10002:10002 \
  --cap-drop ALL \
  --cap-add NET_BIND_SERVICE \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m,uid=10002,gid=10002,mode=1770 \
  --tmpfs /data:rw,nosuid,nodev,noexec,size=64m,uid=10002,gid=10002,mode=0700 \
  --tmpfs /config:rw,nosuid,nodev,noexec,size=16m,uid=10002,gid=10002,mode=0700 \
  --entrypoint caddy \
  "$caddy_image" \
  file-server --listen :81 >/dev/null
sleep 1
[ "$(/usr/bin/docker inspect --format '{{.State.Status}}' "$caddy_probe")" = "running" ] ||
  fail "immutable Caddy image could not bind a privileged port as its runtime UID"
cleanup_caddy_probe
trap - EXIT

/usr/bin/docker run \
  --rm \
  --network none \
  --read-only \
  --user 10003:10003 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=128m,uid=10003,gid=10003,mode=1770 \
  --mount "type=bind,src=${deploy_root}/nginx.conf,dst=/etc/nginx/nginx.conf,readonly" \
  --entrypoint nginx \
  "$nginx_image" \
  -t -c /etc/nginx/nginx.conf ||
  fail "nginx request-guard config did not validate with the immutable image"

printf '%s\n' "Caddy and nginx proxy configurations passed immutable-image validation."
