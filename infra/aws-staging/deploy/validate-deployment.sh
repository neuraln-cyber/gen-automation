#!/usr/bin/env bash
set -euo pipefail

config_root="/etc/gen-automation"
profile_root="/var/lib/gen-automation/integration-profiles"
compose_plugin="/usr/local/lib/docker/cli-plugins/docker-compose"
compose_version="5.1.2"
compose_sha256="c372e512a36e67716b0b3a1264ccdc461dec7a7beff601b81f7c5fb008e3511e"
rds_ca_path="$config_root/rds-global-bundle.pem"
rds_ca_sha256="e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"

fail() {
  printf '%s\n' "deployment validation failed: $*" >&2
  exit 1
}

require_private_root_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing $path"
  [ "$(stat -c '%U:%G' "$path")" = "root:root" ] || fail "$path must be owned by root:root"
  case "$(stat -c '%a' "$path")" in
    400|600) ;;
    *) fail "$path must have mode 0400 or 0600" ;;
  esac
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

require_service_directory() {
  local path="$1"
  local owner="$2"
  [ -d "$path" ] || fail "missing persistent directory $path"
  [ "$(stat -c '%u:%g' "$path")" = "$owner" ] ||
    fail "$path must be owned by service UID/GID $owner"
  [ "$(stat -c '%a' "$path")" = "700" ] ||
    fail "$path must have mode 0700"
}

for file in deploy.env control-plane.env patreon-browser.env semantic-gateway.env caddy.env; do
  require_private_root_file "$config_root/$file"
done

[ -x "$compose_plugin" ] || fail "missing executable pinned Docker Compose plugin"
printf '%s  %s\n' "$compose_sha256" "$compose_plugin" | sha256sum --check --status ||
  fail "Docker Compose plugin checksum does not match the reviewed pin"
installed_compose_version="$(/usr/bin/docker compose version --short 2>/dev/null || true)"
installed_compose_version="${installed_compose_version#v}"
[ "$installed_compose_version" = "$compose_version" ] ||
  fail "Docker Compose plugin must be version $compose_version"
[ -f "$rds_ca_path" ] || fail "missing pinned AWS RDS CA bundle"
printf '%s  %s\n' "$rds_ca_sha256" "$rds_ca_path" | sha256sum --check --status ||
  fail "AWS RDS CA bundle checksum does not match the reviewed pin"

for directory in \
  "$profile_root/mega" \
  "$profile_root/patreon-browser/profiles" \
  "$profile_root/patreon-browser/state"; do
  require_service_directory "$directory" "10001:10001"
done
for directory in \
  /var/lib/gen-automation/caddy/data \
  /var/lib/gen-automation/caddy/config; do
  require_service_directory "$directory" "10002:10002"
done

iptables_binary="$(command -v iptables || true)"
[ -n "$iptables_binary" ] || fail "iptables is required for host-network IMDS isolation"
for blocked_uid in 10002 10003; do
  "$iptables_binary" --wait 5 --check OUTPUT \
    --destination 169.254.169.254/32 \
    --match owner \
    --uid-owner "$blocked_uid" \
    --jump REJECT ||
    fail "IMDS egress rejection is missing for host UID $blocked_uid"
done

for image_key in \
  GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE \
  GEN_AUTOMATION_PATREON_BROWSER_IMAGE \
  GEN_AUTOMATION_SEMANTIC_GATEWAY_IMAGE \
  GEN_AUTOMATION_NGINX_IMAGE \
  GEN_AUTOMATION_CADDY_IMAGE; do
  image="$(env_value "$image_key" "$config_root/deploy.env")"
  [[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] ||
    fail "$image_key must be an immutable repository@sha256:64-hex reference"
done

[ "$(env_value GEN_AUTOMATION_PATREON_BROWSER_MAX_PACKAGE_BYTES "$config_root/patreon-browser.env")" = "167772160" ] ||
  fail "Patreon browser package cap must be exactly 167772160 bytes"
[ "$(env_value GEN_AUTOMATION_BACKGROUND_PUBLICATION_MAX_PACKAGE_BYTES "$config_root/control-plane.env")" = "167772160" ] ||
  fail "control-plane publication package cap must be exactly 167772160 bytes"
[ "$(env_value GEN_AUTOMATION_PATREON_BROWSER_SIDECAR_URL "$config_root/control-plane.env")" = "http://127.0.0.1:8090/v1/publish" ] ||
  fail "control-plane Patreon sidecar URL must remain loopback-only"
[ "$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL "$config_root/control-plane.env")" = "http://127.0.0.1:8091/v1/anatomy/assess" ] ||
  fail "control-plane semantic gateway URL must remain loopback-only"
[ "$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL "$config_root/control-plane.env")" = "$(env_value GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL "$config_root/semantic-gateway.env")" ] ||
  fail "control-plane and semantic gateway model identifiers must match"
[ "$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL_REVISION "$config_root/control-plane.env")" = "$(env_value GEN_AUTOMATION_SEMANTIC_GATEWAY_MODEL_REVISION "$config_root/semantic-gateway.env")" ] ||
  fail "control-plane and semantic gateway model revisions must match"
semantic_upstream="$(env_value GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_CHAT_COMPLETIONS_URL "$config_root/semantic-gateway.env")"
[[ "$semantic_upstream" =~ ^https://api[.]runpod[.]ai/v2/[A-Za-z0-9_-]+/openai/v1/chat/completions$ ]] ||
  fail "semantic gateway upstream must be one exact official RunPod endpoint"
[ -n "$(env_value GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_API_KEY "$config_root/semantic-gateway.env")" ] ||
  fail "semantic gateway RunPod API key must be configured"
[ "$(env_value GEN_AUTOMATION_INGRESS_RATE_LIMIT_CONFIGURED "$config_root/control-plane.env")" = "true" ] ||
  fail "control-plane must assert the validated nginx rate limit"
[ "$(env_value GEN_AUTOMATION_INGRESS_REQUEST_GUARDS_CONFIGURED "$config_root/control-plane.env")" = "true" ] ||
  fail "control-plane must assert the validated nginx request guards"

hostname="$(env_value GEN_AUTOMATION_HOSTNAME "$config_root/caddy.env")"
[[ "$hostname" =~ ^[A-Za-z0-9.-]+$ ]] || fail "Caddy hostname is missing or invalid"

printf '%s\n' "Gen Automation staging deployment inputs passed static validation."
