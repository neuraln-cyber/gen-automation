#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "Run this installer as root from an SSM session." >&2
  exit 1
}

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_root="/opt/gen-automation/deploy"
config_root="/etc/gen-automation"

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
  "$source_dir/install-compose-plugin.sh" \
  /usr/local/sbin/gen-automation-install-compose-plugin
install -o root -g root -m 0755 \
  "$source_dir/install-imds-egress-rules.sh" \
  /usr/local/libexec/gen-automation-install-imds-egress-rules
install -o root -g root -m 0755 \
  "$source_dir/bootstrap-patreon-profile.sh" \
  /usr/local/sbin/gen-automation-bootstrap-patreon-profile
install -o root -g root -m 0644 \
  "$source_dir/gen-automation-staging.service" \
  /etc/systemd/system/gen-automation-staging.service
install -o root -g root -m 0644 \
  "$source_dir/gen-automation-imds-egress.service" \
  /etc/systemd/system/gen-automation-imds-egress.service

for example in "$source_dir"/*.env.example; do
  install -o root -g root -m 0600 "$example" "$config_root/examples/$(basename "$example")"
done

/usr/local/sbin/gen-automation-install-compose-plugin
systemctl daemon-reload
printf '%s\n' \
  "Bundle and pinned Docker Compose plugin installed without credentials." \
  "Create the four root-owned 0600 files under $config_root, validate them, then enable the unit."
