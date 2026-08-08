#!/usr/bin/env bash
set -euo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
profile_home="/var/lib/gen-automation/integration-profiles/mega"
profile_cache="$profile_home/.megaCmd"
unit="gen-automation-staging.service"
enable_https=true
verify_only=false

fail() {
  printf '%s\n' "MEGA profile bootstrap failed: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: gen-automation-bootstrap-mega-profile [--verify-only] [--skip-https]

With no options, opens the official MEGAcmd shell for a one-time account login,
then verifies the persistent session and private profile. HTTPS transfers are
enabled by default. --verify-only checks an existing profile without opening a
login shell. --skip-https leaves the current MEGAcmd transfer setting unchanged.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --verify-only)
      verify_only=true
      ;;
    --skip-https)
      enable_https=false
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      usage >&2
      fail "unknown option: $1"
      ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || fail "run this command as root from an interactive SSM shell"
[ -t 0 ] && [ -t 1 ] || fail "an interactive terminal is required"
[ -r "$config_root/deploy.env" ] || fail "missing $config_root/deploy.env"
[ -r "$config_root/control-plane.env" ] || fail "missing $config_root/control-plane.env"
[ -r "$deploy_root/compose.yaml" ] || fail "missing deployed compose.yaml"
[ -r "$deploy_root/compose.bootstrap.yaml" ] || fail "missing deployed compose.bootstrap.yaml"

image_matches="$(
  grep -E '^GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE=' "$config_root/deploy.env" || true
)"
[ "$(printf '%s\n' "$image_matches" | sed '/^$/d' | wc -l)" -eq 1 ] ||
  fail "deploy.env must define the MEGAcmd-enabled control-plane image exactly once"
image="${image_matches#*=}"
[[ "$image" =~ ^ghcr[.]io/neuraln-cyber/gen-automation/control-plane-mega@sha256:[0-9a-f]{64}$ ]] ||
  fail "the MEGAcmd-enabled control-plane image must be the approved immutable digest"

remote_root_matches="$(
  grep -E '^GEN_AUTOMATION_MEGA_REMOTE_ROOT=' "$config_root/control-plane.env" || true
)"
[ "$(printf '%s\n' "$remote_root_matches" | sed '/^$/d' | wc -l)" -eq 1 ] ||
  fail "control-plane.env must define the MEGA remote root exactly once"
remote_root="${remote_root_matches#*=}"
[[ "$remote_root" =~ ^/[^*?\\]*[^/]$ ]] ||
  fail "the MEGA remote root must be a normalized absolute non-root path"
[[ "/$remote_root/" != *"/../"* && "/$remote_root/" != *"/./"* ]] ||
  fail "the MEGA remote root cannot contain traversal components"

require_private_directory() {
  local path="$1"
  [ -d "$path" ] && [ ! -L "$path" ] || fail "$path must be a real directory"
  [ "$(stat -c '%u:%g' "$path")" = "10001:10001" ] ||
    fail "$path must be owned by service UID/GID 10001:10001"
  [ "$(stat -c '%a' "$path")" = "700" ] || fail "$path must have mode 0700"
}

require_private_profile() {
  require_private_directory "$profile_home"
  require_private_directory "$profile_cache"
  local unsafe_entry
  unsafe_entry="$(
    find "$profile_cache" -xdev \
      \( -type l -o ! -uid 10001 -o ! -gid 10001 -o -perm /077 \) \
      -print -quit
  )"
  [ -z "$unsafe_entry" ] ||
    fail "profile contains an unsafe owner, mode, or symbolic link: $unsafe_entry"
}

require_private_directory "$profile_home"

compose=(/usr/bin/docker compose
  --env-file "$config_root/deploy.env"
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

run_megacmd() {
  local executable="$1"
  shift
  "${compose[@]}" --profile bootstrap run \
    --rm \
    --no-deps \
    --no-TTY \
    --entrypoint /bin/sh \
    mega-profile-bootstrap \
    -c 'umask 077; exec "$@"' \
    mega-bootstrap-command \
    "$executable" \
    "$@"
}

if [ "$verify_only" = false ]; then
  cat <<'EOF'
The official MEGAcmd shell will open next. At its "MEGA CMD>" prompt:

  1. Type: login YOUR_MEGA_EMAIL
  2. Enter the password only at MEGAcmd's hidden Password prompt.
  3. Complete an MFA prompt if MEGA requests it.
  4. After "Login complete", type: quit --only-shell

Never put the password, MFA code, session ID, or a writable-folder key on the
login command line. Do not run logout: logout deletes the cached session.
EOF
  "${compose[@]}" --profile bootstrap run \
    --rm \
    --no-deps \
    mega-profile-bootstrap
fi

require_private_profile

# Suppress identity and remote filenames so SSM transcripts contain only the
# outcome, not account or library details.
run_megacmd mega-whoami >/dev/null || fail "the persisted MEGA session is not authenticated"
if [ "$enable_https" = true ]; then
  run_megacmd mega-https on >/dev/null || fail "could not enable HTTPS transfers"
fi
run_megacmd mega-ls "$remote_root" >/dev/null ||
  fail "the persisted MEGA session cannot read the configured remote root"
require_private_profile

if [ "$enable_https" = true ]; then
  printf '%s\n' "MEGA profile is authenticated, private, readable, and configured for HTTPS transfers."
else
  printf '%s\n' "MEGA profile is authenticated, private, and readable; HTTPS setting was left unchanged."
fi
printf '%s\n' "The persistent session was retained. Do not run mega-logout."
