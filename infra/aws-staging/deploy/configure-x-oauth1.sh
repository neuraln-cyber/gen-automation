#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
validator="/usr/local/libexec/gen-automation-validate-deployment"
service_name="gen-automation-staging.service"
activation_lock="/run/lock/gen-automation-semantic-gateway-activation.lock"
update_lock="/run/lock/gen-automation-control-plane-update.lock"
account_id="861912887470"
region="eu-central-1"
safe_message="Publication is stopped and no publication effect is active."
configured_message="The running controller has the exact OAuth 1.0a runtime settings."
binding_message="X OAuth 1.0a account binding passed. No media was uploaded and no post was created."
publishing_runtime_message="The running controller has publication orchestration enabled and the Patreon browser driver disabled."
publishing_enabled_message="Staging publication orchestration is enabled for the configured X runtime. Publication guard remains stopped and no publication effect is active."
publishing_already_enabled_message="Staging publication orchestration was already enabled for the configured X runtime. Publication guard remains stopped and no publication effect is active."
backup_env=""
rollback_armed=0
validator_backup=""
validator_rollback_armed=0

fail() {
  printf '%s\n' "X OAuth 1.0a staging operation failed safely: $*" >&2
  exit 1
}

require_private_runtime_env() {
  [ -d "$config_root" ] || fail "the private configuration directory is missing"
  [ ! -L "$config_root" ] || fail "the private configuration directory cannot be a symlink"
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$config_root")" = "0:0:700" ] ||
    fail "the private configuration directory must remain root:root mode 0700"
  [ -f "$controller_env" ] || fail "the controller environment is missing"
  [ ! -L "$controller_env" ] || fail "the controller environment cannot be a symlink"
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$controller_env")" = "0:0:600" ] ||
    fail "the controller environment must remain root:root mode 0600"
  [ "$(/usr/bin/stat -c '%s' "$controller_env")" -le 1048576 ] ||
    fail "the controller environment is unexpectedly large"
}

env_value() {
  local key="$1"
  local matches
  matches="$(/usr/bin/grep -E "^${key}=" "$controller_env" || true)"
  [ "$(printf '%s\n' "$matches" | /usr/bin/sed '/^$/d' | /usr/bin/wc -l)" -eq 1 ] ||
    fail "the controller environment must define $key exactly once"
  printf '%s' "${matches#*=}"
}

compose() {
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    --project-directory "$deploy_root" \
    --file "$compose_file" \
    "$@"
}

validate_deployment() {
  "$validator" >/dev/null 2>&1 && compose config --quiet >/dev/null 2>&1
}

wait_for_ready() {
  local container_id
  for _ in $(/usr/bin/seq 1 90); do
    if /usr/bin/systemctl is-active --quiet "$service_name" &&
      /usr/bin/curl \
        --fail \
        --silent \
        --show-error \
        --max-time 3 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
      container_id="$(compose ps --status running --quiet control-plane-mega 2>/dev/null || true)"
      if [ -n "$container_id" ] && [ "$(printf '%s\n' "$container_id" | /usr/bin/wc -l)" -eq 1 ]; then
        return 0
      fi
    fi
    /usr/bin/sleep 2
  done
  return 1
}

run_container_check() {
  local mode="$1"
  local expected="$2"
  local output
  shift 2
  output="$(
    compose exec --no-TTY control-plane-mega \
      python3.12 -m gen_automation.x_runtime_check_cli "$mode" "$@" 2>/dev/null
  )" || fail "the bounded controller check did not pass"
  [ "$output" = "$expected" ] || fail "the bounded controller check returned an invalid result"
}

run_stopped_container_check() {
  local mode="$1"
  local expected="$2"
  local output
  shift 2
  output="$(
    compose run --rm --no-deps --no-TTY control-plane-mega \
      python3.12 -m gen_automation.x_runtime_check_cli "$mode" "$@" 2>/dev/null
  )" || fail "the bounded stopped-controller check did not pass"
  [ "$output" = "$expected" ] ||
    fail "the bounded stopped-controller check returned an invalid result"
}

runtime_values_match() {
  [ "$(/usr/bin/grep -Fxc 'GEN_AUTOMATION_X_AUTH_MODE=oauth1' "$controller_env" || true)" -eq 1 ] &&
    [ "$(/usr/bin/grep -Fxc "GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE=aws-secrets-manager://$secret_arn" "$controller_env" || true)" -eq 1 ] &&
    [ "$(/usr/bin/grep -Fxc "GEN_AUTOMATION_X_CREATOR_USER_ID=$creator_user_id" "$controller_env" || true)" -eq 1 ] &&
    [ "$(/usr/bin/grep -Ec '^GEN_AUTOMATION_X_AUTH_MODE=' "$controller_env" || true)" -eq 1 ] &&
    [ "$(/usr/bin/grep -Ec '^GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE=' "$controller_env" || true)" -eq 1 ] &&
    [ "$(/usr/bin/grep -Ec '^GEN_AUTOMATION_X_CREATOR_USER_ID=' "$controller_env" || true)" -eq 1 ]
}

atomic_copy_file() {
  local source="$1"
  local target="$2"
  local mode="$3"
  /usr/bin/python3 - "$source" "$target" "$mode" <<'PY'
from __future__ import annotations

import os
import pathlib
import stat
import sys
import tempfile

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
mode = int(sys.argv[3], 8)
metadata = source.lstat()
if not stat.S_ISREG(metadata.st_mode):
    raise SystemExit("invalid atomic copy source")
payload = source.read_bytes()
if len(payload) > 1024 * 1024 or b"\x00" in payload:
    raise SystemExit("invalid atomic copy source")

descriptor, temporary = tempfile.mkstemp(
    prefix=f".{target.name}.x-oauth1.install.", dir=target.parent
)
try:
    os.fchmod(descriptor, mode)
    os.fchown(descriptor, 0, 0)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
}

restore_backup_atomically() {
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$backup_env")" = "0:0:600" ] || return 1
  atomic_copy_file "$backup_env" "$controller_env" 0600
}

install_candidate_validator() {
  /usr/bin/printf '%s  %s\n' "$validator_candidate_sha256" "$validator_candidate" |
    /usr/bin/sha256sum --check --status ||
    fail "the reviewed deployment validator digest changed"
  /usr/bin/bash -n "$validator_candidate" || fail "the reviewed deployment validator is invalid"
  validator_backup="$(
    /usr/bin/mktemp "/usr/local/libexec/.gen-automation-validate-deployment.rollback.XXXXXX"
  )"
  /usr/bin/install -o root -g root -m 0700 "$validator" "$validator_backup"
  validator_rollback_armed=1
  atomic_copy_file "$validator_candidate" "$validator" 0755 ||
    fail "the reviewed deployment validator could not be installed"
  [ "$(/usr/bin/stat -c '%u:%g:%a' "$validator")" = "0:0:755" ] ||
    fail "the installed deployment validator permissions are invalid"
  /usr/bin/bash -n "$validator" || fail "the installed deployment validator is invalid"
  validate_deployment || fail "the installed deployment validator did not accept the deployment"
}

cleanup() {
  local status="$?"
  local rollback_failed=0
  local validator_rollback_failed=0
  trap - EXIT
  if [ "$status" -ne 0 ] && [ "$validator_rollback_armed" -eq 1 ] && [ -n "$validator_backup" ]; then
    printf '%s\n' "The validator update did not complete; restoring the previous validator." >&2
    [ "$(/usr/bin/stat -c '%u:%g:%a' "$validator_backup")" = "0:0:700" ] ||
      validator_rollback_failed=1
    if [ "$validator_rollback_failed" -eq 0 ]; then
      atomic_copy_file "$validator_backup" "$validator" 0755 || validator_rollback_failed=1
    fi
    if [ "$validator_rollback_failed" -eq 0 ]; then
      /usr/bin/bash -n "$validator" >/dev/null 2>&1 || validator_rollback_failed=1
    fi
    if [ "$validator_rollback_failed" -eq 0 ]; then
      /usr/bin/rm -f -- "$validator_backup"
      printf '%s\n' "The previous deployment validator was restored." >&2
    else
      printf '%s\n' \
        "Validator rollback needs operator attention; the root-only backup was retained at $validator_backup." >&2
    fi
  elif [ -n "$validator_backup" ]; then
    /usr/bin/rm -f -- "$validator_backup"
  fi
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ] && [ -n "$backup_env" ]; then
    printf '%s\n' "The configuration change did not become healthy; restoring the previous file." >&2
    restore_backup_atomically || rollback_failed=1
    if [ "$rollback_failed" -eq 0 ] && [ "$operation" = "--enable-publishing" ]; then
      [ "$(/usr/bin/grep -Fxc 'GEN_AUTOMATION_PUBLISHING_ENABLED=false' "$controller_env" || true)" -eq 1 ] &&
        [ "$(/usr/bin/grep -Ec '^GEN_AUTOMATION_PUBLISHING_ENABLED=' "$controller_env" || true)" -eq 1 ] ||
        rollback_failed=1
    fi
    if [ "$rollback_failed" -eq 0 ]; then
      validate_deployment || rollback_failed=1
    fi
    if [ "$rollback_failed" -eq 0 ]; then
      /usr/bin/systemctl restart "$service_name" >/dev/null 2>&1 || rollback_failed=1
    fi
    if [ "$rollback_failed" -eq 0 ]; then
      wait_for_ready || rollback_failed=1
    fi
    if [ "$rollback_failed" -eq 0 ]; then
      /usr/bin/rm -f -- "$backup_env"
      printf '%s\n' "The previous controller environment was restored and is healthy." >&2
    else
      printf '%s\n' \
        "Automatic rollback needs operator attention; the root-only backup was retained at $backup_env." >&2
    fi
  elif [ -n "$backup_env" ]; then
    /usr/bin/rm -f -- "$backup_env"
  fi
  exit "$status"
}
trap cleanup EXIT

[ "$(/usr/bin/id -u)" -eq 0 ] || fail "this operation must run as root"
[ "$#" -ge 1 ] || fail "one documented operation is required"
operation="$1"
shift
case "$operation" in
  --configure)
    [ "$#" -eq 4 ] ||
      fail "configure requires the exact secret ARN, creator ID, and reviewed validator"
    secret_arn="$1"
    creator_user_id="$2"
    validator_candidate="$3"
    validator_candidate_sha256="$4"
    [[ "$secret_arn" =~ ^arn:aws:secretsmanager:${region}:${account_id}:secret:gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}$ ]] ||
      fail "the secret ARN is outside the exact OAuth 1.0a staging boundary"
    [[ "$creator_user_id" =~ ^[1-9][0-9]{0,18}$ ]] || fail "the creator ID is invalid"
    [[ "$validator_candidate" =~ ^/tmp/gen-automation-x-oauth1\.[A-Za-z0-9]{6}/validate-deployment[.]sh$ ]] ||
      fail "the reviewed deployment validator path is invalid"
    [[ "$validator_candidate_sha256" =~ ^[0-9a-f]{64}$ ]] ||
      fail "the reviewed deployment validator digest is invalid"
    [ -f "$validator_candidate" ] && [ ! -L "$validator_candidate" ] ||
      fail "the reviewed deployment validator is unavailable"
    validator_candidate_dir="${validator_candidate%/*}"
    [ "$validator_candidate_dir" != "$validator_candidate" ] ||
      fail "the reviewed deployment validator directory is invalid"
    [ "$(/usr/bin/stat -c '%u:%g:%a' "$validator_candidate_dir")" = "0:0:700" ] ||
      fail "the reviewed deployment validator directory is not private"
    [ "$(/usr/bin/stat -c '%u:%g:%a' "$validator_candidate")" = "0:0:600" ] ||
      fail "the reviewed deployment validator file is not private"
    ;;
  --canary)
    [ "$#" -eq 0 ] || fail "canary accepts no values"
    secret_arn=""
    creator_user_id=""
    validator_candidate=""
    validator_candidate_sha256=""
    ;;
  --enable-publishing)
    [ "$#" -eq 0 ] || fail "enable-publishing accepts no values"
    secret_arn=""
    creator_user_id=""
    validator_candidate=""
    validator_candidate_sha256=""
    ;;
  *) fail "the requested operation is not allowed" ;;
esac

for executable in \
  /usr/bin/bash \
  /usr/bin/curl \
  /usr/bin/docker \
  /usr/bin/flock \
  /usr/bin/grep \
  /usr/bin/id \
  /usr/bin/install \
  /usr/bin/mktemp \
  /usr/bin/python3 \
  /usr/bin/printf \
  /usr/bin/rm \
  /usr/bin/sed \
  /usr/bin/seq \
  /usr/bin/sha256sum \
  /usr/bin/sleep \
  /usr/bin/stat \
  /usr/bin/systemctl \
  /usr/bin/wc; do
  [ -x "$executable" ] || fail "a required reviewed host executable is unavailable"
done
[ -f "$validator" ] && [ ! -L "$validator" ] && [ -x "$validator" ] ||
  fail "the root-owned deployment validator is unavailable"
[ "$(/usr/bin/stat -c '%u:%g:%a' "$validator")" = "0:0:755" ] ||
  fail "the deployment validator must remain root:root mode 0755"
[ -f "$compose_file" ] && [ ! -L "$compose_file" ] || fail "the deployment bundle is unavailable"
[ -f "$deploy_env" ] && [ ! -L "$deploy_env" ] || fail "the deployment image file is unavailable"

# Match the established semantic-promotion lock order.  Both operations edit the
# controller environment, and the second lock also excludes image rollouts.
exec 8>"$activation_lock"
/usr/bin/flock --exclusive --wait 120 8 || fail "semantic gateway activation is in progress"
exec 9>"$update_lock"
/usr/bin/flock --exclusive --wait 120 9 || fail "a control-plane update is in progress"

require_private_runtime_env
validate_deployment || fail "the current deployment inputs are invalid"
wait_for_ready || fail "the current controller is not healthy"
run_container_check --assert-safe-to-configure "$safe_message"

if [ "$operation" = "--canary" ]; then
  [ "$(env_value GEN_AUTOMATION_X_AUTH_MODE)" = "oauth1" ] ||
    fail "the controller is not configured for OAuth 1.0a"
  configured_arn="$(env_value GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE)"
  configured_id="$(env_value GEN_AUTOMATION_X_CREATOR_USER_ID)"
  [[ "$configured_arn" =~ ^aws-secrets-manager://arn:aws:secretsmanager:${region}:${account_id}:secret:gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}$ ]] ||
    fail "the configured secret reference is outside the staging boundary"
  [[ "$configured_id" =~ ^[1-9][0-9]{0,18}$ ]] || fail "the configured creator ID is invalid"
  run_container_check --account-binding "$binding_message"
  printf '%s\n' "$binding_message"
  exit 0
fi

if [ "$operation" = "--enable-publishing" ]; then
  [ "$(env_value GEN_AUTOMATION_ENVIRONMENT)" = "staging" ] ||
    fail "the controller is not the exact staging environment"
  [ "$(env_value GEN_AUTOMATION_X_AUTH_MODE)" = "oauth1" ] ||
    fail "the controller is not configured for OAuth 1.0a"
  configured_arn="$(env_value GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE)"
  configured_id="$(env_value GEN_AUTOMATION_X_CREATOR_USER_ID)"
  [[ "$configured_arn" =~ ^aws-secrets-manager://arn:aws:secretsmanager:${region}:${account_id}:secret:gen-automation-staging/x/oauth1-[A-Za-z0-9]{6}$ ]] ||
    fail "the configured secret reference is outside the staging boundary"
  [[ "$configured_id" =~ ^[1-9][0-9]{0,18}$ ]] || fail "the configured creator ID is invalid"
  [ "$(env_value GEN_AUTOMATION_PATREON_BROWSER_PUBLISHING_ENABLED)" = "false" ] ||
    fail "the Patreon publisher must remain disabled"
  publishing_value="$(env_value GEN_AUTOMATION_PUBLISHING_ENABLED)"
  case "$publishing_value" in
    false|true) ;;
    *) fail "the publishing runtime gate must be one exact boolean" ;;
  esac

  run_container_check \
    --assert-oauth1-configured \
    "$configured_message" \
    "$configured_arn" \
    "$configured_id"
  run_container_check --account-binding "$binding_message"
  run_container_check --assert-safe-to-configure "$safe_message"

  if [ "$publishing_value" = "true" ]; then
    run_container_check --assert-publishing-enabled "$publishing_runtime_message"
    printf '%s\n' "$publishing_already_enabled_message"
    exit 0
  fi

  backup_env="$(/usr/bin/mktemp "$config_root/.control-plane.env.x-publishing.rollback.XXXXXX")"
  /usr/bin/install -o root -g root -m 0600 "$controller_env" "$backup_env"
  rollback_armed=1

  /usr/bin/systemctl stop "$service_name" >/dev/null
  for _ in $(/usr/bin/seq 1 60); do
    if ! /usr/bin/systemctl is-active --quiet "$service_name"; then
      break
    fi
    /usr/bin/sleep 1
  done
  ! /usr/bin/systemctl is-active --quiet "$service_name" ||
    fail "the controller did not stop for its bounded publishing update"

  run_stopped_container_check --assert-safe-to-configure "$safe_message"

  /usr/bin/python3 - "$controller_env" <<'PY'
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
key = "GEN_AUTOMATION_PUBLISHING_ENABLED"
raw = path.read_bytes()
if len(raw) > 1024 * 1024 or b"\x00" in raw:
    raise SystemExit("invalid controller environment")
text = raw.decode("utf-8", errors="strict")
if "\r" in text:
    raise SystemExit("invalid controller environment")
lines = text.splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
if len(matches) != 1 or lines[matches[0]] != f"{key}=false":
    raise SystemExit("publishing runtime gate is not exactly false")
updated = list(lines)
updated[matches[0]] = f"{key}=true"
if len(lines) != len(updated) or [
    index for index, values in enumerate(zip(lines, updated)) if values[0] != values[1]
] != matches:
    raise SystemExit("publishing update changed an unexpected assignment")

descriptor, temporary = tempfile.mkstemp(prefix=".control-plane.env.x-publishing.", dir=path.parent)
try:
    os.fchmod(descriptor, 0o600)
    os.fchown(descriptor, 0, 0)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(updated) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY

  require_private_runtime_env
  [ "$(env_value GEN_AUTOMATION_PUBLISHING_ENABLED)" = "true" ] ||
    fail "the publishing runtime gate did not persist"
  [ "$(env_value GEN_AUTOMATION_PATREON_BROWSER_PUBLISHING_ENABLED)" = "false" ] ||
    fail "the Patreon publisher changed unexpectedly"
  validate_deployment || fail "the publishing-enabled deployment inputs are invalid"
  /usr/bin/systemctl restart "$service_name" >/dev/null
  wait_for_ready || fail "the publishing-enabled controller did not become healthy"
  run_container_check \
    --assert-oauth1-configured \
    "$configured_message" \
    "$configured_arn" \
    "$configured_id"
  run_container_check --assert-publishing-enabled "$publishing_runtime_message"
  run_container_check --assert-safe-to-configure "$safe_message"

  rollback_armed=0
  /usr/bin/rm -f -- "$backup_env"
  backup_env=""
  printf '%s\n' "$publishing_enabled_message"
  exit 0
fi

if runtime_values_match; then
  run_container_check \
    --assert-oauth1-configured \
    "$configured_message" \
    "aws-secrets-manager://$secret_arn" \
    "$creator_user_id"
  run_container_check --account-binding "$binding_message"
  install_candidate_validator
  validator_rollback_armed=0
  /usr/bin/rm -f -- "$validator_backup"
  validator_backup=""
  printf '%s\n' "The exact OAuth 1.0a runtime settings are already configured and healthy."
  exit 0
fi

backup_env="$(/usr/bin/mktemp "$config_root/.control-plane.env.x-oauth1.rollback.XXXXXX")"
/usr/bin/install -o root -g root -m 0600 "$controller_env" "$backup_env"
rollback_armed=1

/usr/bin/systemctl stop "$service_name" >/dev/null
for _ in $(/usr/bin/seq 1 60); do
  if ! /usr/bin/systemctl is-active --quiet "$service_name"; then
    break
  fi
  /usr/bin/sleep 1
done
! /usr/bin/systemctl is-active --quiet "$service_name" ||
  fail "the controller did not stop for its bounded configuration update"

/usr/bin/python3 - "$controller_env" "$secret_arn" "$creator_user_id" <<'PY'
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
secret_arn = sys.argv[2]
creator_user_id = sys.argv[3]
assignments = {
    "GEN_AUTOMATION_X_AUTH_MODE": "oauth1",
    "GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE": f"aws-secrets-manager://{secret_arn}",
    "GEN_AUTOMATION_X_CREATOR_USER_ID": creator_user_id,
}

raw = path.read_bytes()
if len(raw) > 1024 * 1024 or b"\x00" in raw:
    raise SystemExit("invalid controller environment")
text = raw.decode("utf-8", errors="strict")
if "\r" in text:
    raise SystemExit("invalid controller environment")
lines = text.splitlines()
for key, value in assignments.items():
    matches = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
    if len(matches) > 1:
        raise SystemExit("duplicate controller environment key")
    replacement = f"{key}={value}"
    if matches:
        lines[matches[0]] = replacement
    else:
        lines.append(replacement)

descriptor, temporary = tempfile.mkstemp(prefix=".control-plane.env.x-oauth1.", dir=path.parent)
try:
    os.fchmod(descriptor, 0o600)
    os.fchown(descriptor, 0, 0)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY

require_private_runtime_env
[ "$(env_value GEN_AUTOMATION_X_AUTH_MODE)" = "oauth1" ] || fail "OAuth mode did not persist"
[ "$(env_value GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE)" = "aws-secrets-manager://$secret_arn" ] ||
  fail "the exact secret reference did not persist"
[ "$(env_value GEN_AUTOMATION_X_CREATOR_USER_ID)" = "$creator_user_id" ] ||
  fail "the exact creator binding did not persist"
validate_deployment || fail "the configured deployment inputs are invalid"
/usr/bin/systemctl restart "$service_name" >/dev/null
wait_for_ready || fail "the configured controller did not become healthy"
run_container_check \
  --assert-oauth1-configured \
  "$configured_message" \
  "aws-secrets-manager://$secret_arn" \
  "$creator_user_id"
run_container_check --assert-safe-to-configure "$safe_message"
run_container_check --account-binding "$binding_message"
install_candidate_validator

rollback_armed=0
validator_rollback_armed=0
/usr/bin/rm -f -- "$backup_env"
/usr/bin/rm -f -- "$validator_backup"
backup_env=""
validator_backup=""
printf '%s\n' "X OAuth 1.0a runtime settings were configured and the stopped controller is healthy."
