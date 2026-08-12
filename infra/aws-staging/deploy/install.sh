#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "Run this installer as root from an SSM session." >&2
  exit 1
}

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_root="/opt/gen-automation/deploy"
config_root="/etc/gen-automation"
rds_ca_url="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
rds_ca_sha256="e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
rds_ca_path="$config_root/rds-global-bundle.pem"

install -d -o root -g root -m 0755 "$deploy_root"
install -d -o root -g root -m 0700 "$config_root"
install -d -o root -g root -m 0700 "$config_root/examples"
install -d -o root -g root -m 0755 /usr/local/libexec
install -d -o root -g root -m 0755 /usr/local/sbin
install -d -o 10002 -g 10002 -m 0700 /var/lib/gen-automation/caddy/data
install -d -o 10002 -g 10002 -m 0700 /var/lib/gen-automation/caddy/config

install -o root -g root -m 0644 "$source_dir/compose.yaml" "$deploy_root/compose.yaml"
install -o root -g root -m 0644 \
  "$source_dir/compose.bootstrap.yaml" \
  "$deploy_root/compose.bootstrap.yaml"
install -o root -g root -m 0644 "$source_dir/Caddyfile" "$deploy_root/Caddyfile"
install -o root -g root -m 0644 "$source_dir/nginx.conf" "$deploy_root/nginx.conf"
install -o root -g root -m 0644 "$source_dir/README.md" "$deploy_root/README.md"
install -o root -g root -m 0755 \
  "$source_dir/validate-deployment.sh" \
  /usr/local/libexec/gen-automation-validate-deployment
install -o root -g root -m 0755 \
  "$source_dir/validate-proxy-images.sh" \
  /usr/local/libexec/gen-automation-validate-proxy-images
install -o root -g root -m 0755 \
  "$source_dir/validate-migration-environment.sh" \
  /usr/local/libexec/gen-automation-validate-migration-environment
install -o root -g root -m 0755 \
  "$source_dir/configure-lora-manager.sh" \
  /usr/local/libexec/gen-automation-configure-lora-manager
install -o root -g root -m 0755 \
  "$source_dir/install-compose-plugin.sh" \
  /usr/local/sbin/gen-automation-install-compose-plugin
install -o root -g root -m 0755 \
  "$source_dir/install-imds-egress-rules.sh" \
  /usr/local/libexec/gen-automation-install-imds-egress-rules
install -o root -g root -m 0755 \
  "$source_dir/bootstrap-patreon-profile.sh" \
  /usr/local/sbin/gen-automation-bootstrap-patreon-profile
install -o root -g root -m 0755 \
  "$source_dir/bootstrap-mega-profile.sh" \
  /usr/local/sbin/gen-automation-bootstrap-mega-profile
install -o root -g root -m 0755 \
  "$source_dir/bootstrap-owner.sh" \
  /usr/local/sbin/gen-automation-bootstrap-owner
install -o root -g root -m 0755 \
  "$source_dir/update-control-plane.sh" \
  /usr/local/sbin/gen-automation-update-control-plane
install -o root -g root -m 0755 \
  "$source_dir/activate-semantic-gateway.sh" \
  /usr/local/sbin/gen-automation-activate-semantic-gateway
install -o root -g root -m 0755 \
  "$source_dir/promote-semantic-anatomy.sh" \
  /usr/local/sbin/gen-automation-promote-semantic-anatomy
# Remove the privileged helper from the retired image-to-video implementation.
rm -f -- /usr/local/sbin/gen-automation-cutover-video-worker-a14b
install -o root -g root -m 0644 \
  "$source_dir/gen-automation-staging.service" \
  /etc/systemd/system/gen-automation-staging.service
install -o root -g root -m 0644 \
  "$source_dir/gen-automation-imds-egress.service" \
  /etc/systemd/system/gen-automation-imds-egress.service

for example in "$source_dir"/*.env.example; do
  install -o root -g root -m 0600 "$example" "$config_root/examples/$(basename "$example")"
done

if [ ! -f "$rds_ca_path" ] ||
  ! printf '%s  %s\n' "$rds_ca_sha256" "$rds_ca_path" | sha256sum --check --status; then
  temporary_rds_ca="$(mktemp)"
  trap 'rm -f -- "$temporary_rds_ca"' EXIT
  curl \
    --fail \
    --location \
    --proto '=https' \
    --retry 3 \
    --retry-all-errors \
    --silent \
    --show-error \
    --output "$temporary_rds_ca" \
    "$rds_ca_url"
  printf '%s  %s\n' "$rds_ca_sha256" "$temporary_rds_ca" | sha256sum --check --status || {
    printf '%s\n' "Downloaded AWS RDS CA bundle failed SHA-256 verification." >&2
    exit 1
  }
  install -o root -g root -m 0644 "$temporary_rds_ca" "$rds_ca_path"
fi

/usr/local/sbin/gen-automation-install-compose-plugin
systemctl daemon-reload
printf '%s\n' \
  "Bundle and pinned Docker Compose plugin installed without credentials." \
  "Create the six root-owned 0600 files under $config_root, validate them, then enable the unit."
