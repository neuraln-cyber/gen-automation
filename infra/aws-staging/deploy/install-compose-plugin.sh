#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "Run this installer as root from an SSM session." >&2
  exit 1
}

readonly compose_version="5.1.2"
readonly compose_sha256="c372e512a36e67716b0b3a1264ccdc461dec7a7beff601b81f7c5fb008e3511e"
readonly compose_asset="docker-compose-linux-x86_64"
readonly compose_url="https://github.com/docker/compose/releases/download/v${compose_version}/${compose_asset}"
readonly plugin_dir="/usr/local/lib/docker/cli-plugins"
readonly plugin_path="${plugin_dir}/docker-compose"

[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] || {
  printf '%s\n' "The reviewed Docker Compose pin supports Linux x86_64 only." >&2
  exit 1
}
command -v curl >/dev/null || {
  printf '%s\n' "curl is required to install the pinned Docker Compose plugin." >&2
  exit 1
}
command -v sha256sum >/dev/null || {
  printf '%s\n' "sha256sum is required to verify the Docker Compose plugin." >&2
  exit 1
}
[ -x /usr/bin/docker ] || {
  printf '%s\n' "The Amazon Linux Docker CLI is required at /usr/bin/docker." >&2
  exit 1
}

install -d -o root -g root -m 0755 "$plugin_dir"

if [ ! -f "$plugin_path" ] ||
  ! printf '%s  %s\n' "$compose_sha256" "$plugin_path" | sha256sum --check --status; then
  temporary="$(mktemp)"
  trap 'rm -f -- "$temporary"' EXIT
  curl \
    --fail \
    --location \
    --proto '=https' \
    --retry 3 \
    --retry-all-errors \
    --silent \
    --show-error \
    --output "$temporary" \
    "$compose_url"
  printf '%s  %s\n' "$compose_sha256" "$temporary" | sha256sum --check --status || {
    printf '%s\n' "Downloaded Docker Compose plugin failed SHA-256 verification." >&2
    exit 1
  }
  install -o root -g root -m 0755 "$temporary" "$plugin_path"
fi

actual_version="$(/usr/bin/docker compose version --short)"
actual_version="${actual_version#v}"
[ "$actual_version" = "$compose_version" ] || {
  printf '%s\n' \
    "Docker resolved Compose $actual_version instead of reviewed version $compose_version." >&2
  exit 1
}

printf '%s\n' "Docker Compose v${compose_version} is installed and checksum-verified."
