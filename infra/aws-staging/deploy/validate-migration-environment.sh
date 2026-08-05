#!/usr/bin/env bash
set -euo pipefail

config_root="/etc/gen-automation"
migration_env="$config_root/migration.env"
runtime_env="$config_root/control-plane.env"

fail() {
  printf '%s\n' "migration environment validation failed: $*" >&2
  exit 1
}

require_private_root_file() {
  local path="$1"
  [ -f "$path" ] || fail "missing $path"
  [ "$(stat -c '%U:%G' "$path")" = "root:root" ] ||
    fail "$path must be owned by root:root"
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

database_username() {
  local url="$1"
  local authority
  local username
  authority="${url#postgresql+psycopg://}"
  [ "$authority" != "$url" ] || fail "database URL must use postgresql+psycopg"
  username="${authority%%:*}"
  [ -n "$username" ] && [ "$username" != "$authority" ] ||
    fail "database URL must include a username and password"
  [[ "$username" != *['/@']* ]] || fail "database URL username is invalid"
  printf '%s' "$username"
}

require_private_root_file "$migration_env"
require_private_root_file "$runtime_env"

active_migration_lines="$(
  sed \
    -e '/^[[:space:]]*#/d' \
    -e '/^[[:space:]]*$/d' \
    "$migration_env"
)"
[ "$(printf '%s\n' "$active_migration_lines" | sed '/^$/d' | wc -l)" -eq 1 ] ||
  fail "$migration_env must contain exactly one active assignment"
case "$active_migration_lines" in
  GEN_AUTOMATION_DATABASE_URL=*) ;;
  *) fail "$migration_env may define only GEN_AUTOMATION_DATABASE_URL" ;;
esac

migration_url="$(env_value GEN_AUTOMATION_DATABASE_URL "$migration_env")"
runtime_url="$(env_value GEN_AUTOMATION_DATABASE_URL "$runtime_env")"
[ -n "$migration_url" ] || fail "migration database URL must not be blank"
[ "$migration_url" != "$runtime_url" ] ||
  fail "migration and runtime database URLs must be distinct"

migration_username="$(database_username "$migration_url")"
runtime_username="$(database_username "$runtime_url")"
[ "$migration_username" != "$runtime_username" ] ||
  fail "migration and runtime database usernames must be distinct"

[[ "$migration_url" =~ [\?\&]sslmode=verify-full([\&]|$) ]] ||
  fail "migration database URL must require sslmode=verify-full"
[[ "$migration_url" =~ [\?\&]sslrootcert=/run/gen-automation/rds-global-bundle.pem([\&]|$) ]] ||
  fail "migration database URL must use the pinned RDS CA path"

printf '%s\n' "Gen Automation migration environment passed static validation."
