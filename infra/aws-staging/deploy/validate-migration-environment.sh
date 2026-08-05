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

require_private_root_file "$migration_env"
require_private_root_file "$runtime_env"
command -v python3 >/dev/null || fail "python3 is required"

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

env_value GEN_AUTOMATION_DATABASE_URL "$migration_env" >/dev/null
env_value GEN_AUTOMATION_DATABASE_URL "$runtime_env" >/dev/null

python3 - "$migration_env" "$runtime_env" <<'PY'
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit


def fail(message: str) -> None:
    print(f"migration environment validation failed: {message}", file=sys.stderr)
    raise SystemExit(1)


def database_url(path: str) -> str:
    prefix = "GEN_AUTOMATION_DATABASE_URL="
    matches = [
        line[len(prefix) :]
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        fail(f"{path} must define one non-blank database URL")
    return matches[0]


def parse_database_url(url: str, *, label: str):
    try:
        parsed = urlsplit(url)
        username = parsed.username
        password = parsed.password
    except ValueError:
        fail(f"{label} database URL is invalid")
    if parsed.scheme != "postgresql+psycopg":
        fail(f"{label} database URL must use postgresql+psycopg")
    if not username or password is None:
        fail(f"{label} database URL must include a username and password")
    return parsed, username


migration_url = database_url(sys.argv[1])
runtime_url = database_url(sys.argv[2])
if migration_url == runtime_url:
    fail("migration and runtime database URLs must be distinct")

migration, migration_username = parse_database_url(migration_url, label="migration")
_, runtime_username = parse_database_url(runtime_url, label="runtime")
if migration_username == runtime_username:
    fail("migration and runtime database usernames must be distinct")

query = parse_qs(migration.query, keep_blank_values=True)
if query.get("sslmode") != ["verify-full"]:
    fail("migration database URL must require sslmode=verify-full")
if query.get("sslrootcert") != ["/run/gen-automation/rds-global-bundle.pem"]:
    fail("migration database URL must use the pinned RDS CA path")
PY

printf '%s\n' "Gen Automation migration environment passed static validation."
