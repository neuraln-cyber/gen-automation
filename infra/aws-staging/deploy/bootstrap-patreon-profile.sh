#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "Run this command as root from an SSM shell session." >&2
  exit 1
fi

deploy_root="/opt/gen-automation/deploy"
unit="gen-automation-staging.service"
compose=(/usr/bin/docker compose
  --env-file /etc/gen-automation/deploy.env
  --project-directory "$deploy_root"
  --file "$deploy_root/compose.yaml"
  --file "$deploy_root/compose.bootstrap.yaml")

was_active=false
if systemctl is-active --quiet "$unit"; then
  was_active=true
  systemctl stop "$unit"
fi

restore_runtime() {
  if [ "$was_active" = true ]; then
    systemctl start "$unit"
  fi
}
trap restore_runtime EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" --profile bootstrap run \
  --rm \
  --no-deps \
  --service-ports \
  patreon-browser-bootstrap
