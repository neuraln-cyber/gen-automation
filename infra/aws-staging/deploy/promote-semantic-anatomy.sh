#!/usr/bin/env bash
set -Eeuo pipefail

config_root="/etc/gen-automation"
deploy_root="/opt/gen-automation/deploy"
controller_env="$config_root/control-plane.env"
deploy_env="$config_root/deploy.env"
compose_file="$deploy_root/compose.yaml"
validator="/usr/local/libexec/gen-automation-validate-deployment"
service_name="gen-automation-staging.service"
activation_lock="/run/lock/gen-automation-semantic-gateway-activation.lock"
update_lock="/run/lock/gen-automation-control-plane-update.lock"
default_max_assessments=400
max_initial_backlog=1000

operation="status"
requested_max="$default_max_assessments"
expected_revision=""
temporary_env=""
backup_env=""
rollback_armed=0
rollback_failed=0

fail() {
  printf '%s\n' "semantic anatomy promotion failed: $*" >&2
  return 1
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

health_status() {
  local url="$1"
  if curl --fail --silent --show-error --max-time 2 "$url" >/dev/null 2>&1; then
    printf '%s' ready
  else
    printf '%s' unavailable
  fi
}

print_status() {
  local allowlist
  local allowlist_count
  local controller_health
  local enabled
  local gateway_health
  local maximum
  local mode
  local projection_cap
  local revision

  projection_cap="${1:-}"

  enabled="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED "$controller_env")"
  mode="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE "$controller_env")"
  maximum="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE "$controller_env")"
  [ -n "$projection_cap" ] || projection_cap="$maximum"
  allowlist="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST "$controller_env")"
  allowlist_count="$(
    python3 -c \
      'import json, sys; value=json.loads(sys.argv[1]); assert isinstance(value, list); print(len(value))' \
      "$allowlist"
  )" || fail "semantic anatomy asset allowlist must be a JSON array"
  gateway_health="$(health_status http://127.0.0.1:8091/health/ready)"
  controller_health="$(health_status http://127.0.0.1:8000/api/v1/health/ready)"
  revision="$(control_plane_revision)"

  printf '%s\n' \
    "semantic_anatomy_enabled=$enabled" \
    "semantic_anatomy_mode=$mode" \
    "semantic_anatomy_configured_per_scoring_run_cap=$maximum" \
    "semantic_anatomy_asset_allowlist_count=$allowlist_count" \
    "control_plane_source_revision=$revision" \
    "semantic_gateway_health=$gateway_health" \
    "control_plane_health=$controller_health"

  print_coverage "$projection_cap"
}

print_coverage() {
  local coverage_program
  local projection_cap="$1"
  [[ "$projection_cap" =~ ^[0-9]{1,5}$ ]] || fail "projection cap is invalid"
  [ "$projection_cap" -le 10000 ] || fail "projection cap exceeds the application bound"
  read -r -d '' coverage_program <<'PY' || true
import asyncio
import os
import sys

from sqlalchemy import text

from gen_automation.db.session import Database
from gen_automation.semantic import assessment_profile_sha256


async def main() -> None:
    projection_cap = int(sys.argv[1])
    aggregate_guard = int(sys.argv[2])
    profile_sha256 = assessment_profile_sha256(
        model=os.environ["GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL"],
        model_revision=os.environ["GEN_AUTOMATION_SEMANTIC_ANATOMY_MODEL_REVISION"],
    )
    maximum_attempts = int(
        os.environ["GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS"]
    )
    database = Database(os.environ["GEN_AUTOMATION_DATABASE_URL"])
    try:
        async with database.engine.connect() as connection:
            state_rows = (
                await connection.execute(
                    text(
                        "SELECT state, count(*) FROM semantic_assessments "
                        "WHERE profile_sha256 = :profile_sha256 GROUP BY state"
                    ),
                    {"profile_sha256": profile_sha256},
                )
            ).all()
            review_row = (
                await connection.execute(
                    text(
                        "SELECT count(*), COALESCE(sum(ranked_asset_count), 0) "
                        "FROM review_tasks WHERE state = 'open'"
                    )
                )
            ).one()
            projection_rows = (
                await connection.execute(
                text(
                    "WITH missing_by_run AS ("
                    "SELECT score.scoring_run_id, count(*) AS missing_count "
                    "FROM asset_scores AS score "
                    "JOIN scoring_runs AS run ON run.id = score.scoring_run_id "
                    "JOIN review_tasks AS task ON task.scoring_run_id = score.scoring_run_id "
                    "JOIN asset_rankings AS ranking "
                    "ON ranking.scoring_run_id = score.scoring_run_id "
                    "AND ranking.asset_id = score.asset_id "
                    "JOIN assets AS asset ON asset.id = score.asset_id "
                    "WHERE run.state = 'completed' AND task.state = 'open' "
                    "AND score.state IN ('scored', 'flagged_blank', 'flagged_corrupt', 'dead_letter') "
                    "AND score.completed_at IS NOT NULL "
                    "AND asset.kind = 'raw_master' AND asset.state = 'available' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM semantic_assessments AS assessment "
                    "WHERE assessment.scoring_run_id = score.scoring_run_id "
                    "AND assessment.asset_id = score.asset_id "
                    "AND assessment.profile_sha256 = :profile_sha256) "
                    "GROUP BY score.scoring_run_id), "
                    "existing_by_run AS ("
                    "SELECT scoring_run_id, count(*) AS existing_count "
                    "FROM semantic_assessments "
                    "WHERE profile_sha256 = :profile_sha256 GROUP BY scoring_run_id) "
                    "SELECT missing.missing_count, COALESCE(existing.existing_count, 0) "
                    "FROM missing_by_run AS missing LEFT JOIN existing_by_run AS existing "
                    "ON existing.scoring_run_id = missing.scoring_run_id"
                ),
                {"profile_sha256": profile_sha256},
                )
            ).all()
    finally:
        await database.dispose()

    states = {str(state): int(count) for state, count in state_rows}
    missing_count = sum(int(row[0]) for row in projection_rows)
    projected_count = sum(
        min(int(missing), max(0, projection_cap - int(existing)))
        for missing, existing in projection_rows
    )
    print(f"semantic_profile_sha256={profile_sha256}")
    for state in ("pending", "processing", "retry_wait", "completed", "unavailable"):
        print(f"semantic_current_profile_{state}_count={states.get(state, 0)}")
    print(f"semantic_current_profile_total_count={sum(states.values())}")
    print(
        "semantic_successful_canary_gate="
        + ("pass" if states.get("completed", 0) >= 1 else "fail")
    )
    print(f"semantic_open_review_task_count={int(review_row[0])}")
    print(f"semantic_open_review_ranked_asset_count={int(review_row[1])}")
    print(f"semantic_open_review_missing_current_profile_count={missing_count}")
    print(f"semantic_projection_per_scoring_run_cap={projection_cap}")
    print(f"semantic_projected_new_assessment_count={projected_count}")
    print(f"semantic_projected_attempt_ceiling={projected_count * maximum_attempts}")
    print(f"semantic_initial_backlog_guard_limit={aggregate_guard}")
    print(
        "semantic_initial_backlog_guard="
        + ("pass" if projected_count <= aggregate_guard else "fail")
    )


asyncio.run(main())
PY
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    -f "$compose_file" \
    exec --no-TTY control-plane-mega \
    python3.12 -c "$coverage_program" "$projection_cap" "$max_initial_backlog" ||
    fail "could not read semantic anatomy coverage from the running control plane"
}

control_plane_revision() {
  local container_id
  local revision
  container_id="$(
    /usr/bin/docker compose \
      --env-file "$deploy_env" \
      -f "$compose_file" \
      ps --status running --quiet control-plane-mega 2>/dev/null || true
  )"
  [ -n "$container_id" ] || fail "running control-plane container was not found"
  [ "$(printf '%s\n' "$container_id" | sed '/^$/d' | wc -l)" -eq 1 ] ||
    fail "expected exactly one running control-plane container"
  revision="$(
    /usr/bin/docker inspect \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
      "$container_id"
  )"
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "running control-plane image has no valid source revision"
  printf '%s' "$revision"
}

wait_for_stack() {
  for _ in $(seq 1 50); do
    if systemctl is-active --quiet "$service_name" &&
      curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:8091/health/ready >/dev/null 2>&1 &&
      curl --fail --silent --show-error --max-time 2 \
        http://127.0.0.1:8000/api/v1/health/ready >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
  done
  return 1
}

restore_previous_environment() {
  local rollback_status=0

  printf '%s\n' "Promotion did not become ready; restoring the previous environment." >&2
  if [ -z "$backup_env" ] || [ ! -f "$backup_env" ]; then
    printf '%s\n' "Rollback copy is unavailable." >&2
    return 1
  fi
  if mv -- "$backup_env" "$controller_env"; then
    backup_env=""
  else
    rollback_status=1
  fi
  "$validator" || rollback_status=1
  /usr/bin/docker compose \
    --env-file "$deploy_env" \
    -f "$compose_file" \
    config --quiet || rollback_status=1
  systemctl restart --no-block "$service_name" || rollback_status=1
  wait_for_stack || rollback_status=1
  [ "$rollback_status" -eq 0 ] ||
    printf '%s\n' "Automatic rollback requires operator attention." >&2
  return "$rollback_status"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e
  if [ "$status" -ne 0 ] && [ "$rollback_armed" -eq 1 ]; then
    restore_previous_environment || rollback_failed=1
  fi
  [ -z "$temporary_env" ] || rm -f -- "$temporary_env"
  if [ -n "$backup_env" ] && [ "$rollback_failed" -eq 1 ]; then
    printf '%s\n' "Rollback copy preserved at $backup_env." >&2
  elif [ -n "$backup_env" ]; then
    rm -f -- "$backup_env"
  fi
  exit "$status"
}
trap cleanup EXIT

[ "$(id -u)" -eq 0 ] || fail "run as root through AWS Systems Manager"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --status)
      operation="status"
      shift
      ;;
    --dry-run)
      operation="dry-run"
      shift
      ;;
    --promote)
      operation="promote"
      shift
      ;;
    --pause)
      operation="pause"
      shift
      ;;
    --max-assessments)
      [ "$#" -ge 2 ] || fail "--max-assessments requires a value"
      requested_max="$2"
      shift 2
      ;;
    --expected-control-plane-revision)
      [ "$#" -ge 2 ] || fail "--expected-control-plane-revision requires a value"
      expected_revision="$2"
      shift 2
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$requested_max" =~ ^[1-9][0-9]{0,3}$ ]] ||
  fail "max assessments must be an integer from 1 through 1000"
[ "$requested_max" -le 1000 ] ||
  fail "max assessments must be an integer from 1 through 1000"
if [ -n "$expected_revision" ]; then
  [[ "$expected_revision" =~ ^[0-9a-f]{40}$ ]] ||
    fail "expected control-plane revision must be 40 lowercase hexadecimal characters"
fi
[ -f "$controller_env" ] || fail "missing $controller_env"
[ -f "$deploy_env" ] || fail "missing $deploy_env"
[ -f "$compose_file" ] || fail "missing $compose_file"
[ -x "$validator" ] || fail "missing $validator"
for command in curl flock python3; do
  command -v "$command" >/dev/null || fail "$command is required"
done
[ -x /usr/bin/docker ] || fail "/usr/bin/docker is required"

if [ "$operation" = "status" ]; then
  print_status
  exit 0
fi

if [ "$operation" = "dry-run" ] || [ "$operation" = "promote" ]; then
  [ -n "$expected_revision" ] ||
    fail "$operation requires --expected-control-plane-revision"
  actual_revision="$(control_plane_revision)"
  [ "$actual_revision" = "$expected_revision" ] ||
    fail "running control-plane revision $actual_revision does not match $expected_revision"
  current_max="$(
    env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE "$controller_env"
  )"
  [[ "$current_max" =~ ^[0-9]{1,5}$ ]] || fail "current assessment limit is invalid"
  [ "$current_max" -le 1000 ] || fail "current assessment limit exceeds the supported bound"
  [ "$requested_max" -ge "$current_max" ] ||
    fail "requested cap $requested_max would lower the current cap $current_max"
  "$validator"
  [ "$(health_status http://127.0.0.1:8091/health/ready)" = ready ] ||
    fail "semantic gateway is not ready; no configuration was changed"
  if [ "$operation" = "dry-run" ]; then
    print_status "$requested_max"
    printf '%s\n' \
      "planned_semantic_anatomy_enabled=true" \
      "planned_semantic_anatomy_mode=shadow" \
      "planned_semantic_anatomy_configured_per_scoring_run_cap=$requested_max" \
      "planned_semantic_anatomy_asset_allowlist_count=0" \
      "dry_run=true"
    exit 0
  fi
elif [ "$operation" = "pause" ]; then
  "$validator"
else
  fail "invalid operation"
fi

exec 8>"$activation_lock"
flock --exclusive --wait 60 8 || fail "semantic gateway activation is in progress"
exec 9>"$update_lock"
flock --exclusive --wait 60 9 || fail "control-plane update is in progress"

# Re-read under both deployment locks so a concurrent rollout cannot make the
# revision or monotonic cap checks stale.
current_enabled="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED "$controller_env")"
if [ "$operation" = "promote" ]; then
  actual_revision="$(control_plane_revision)"
  [ "$actual_revision" = "$expected_revision" ] ||
    fail "running control-plane revision $actual_revision does not match $expected_revision"
  current_max="$(
    env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE "$controller_env"
  )"
  [[ "$current_max" =~ ^[0-9]{1,5}$ ]] || fail "current assessment limit is invalid"
  [ "$current_max" -le 1000 ] || fail "current assessment limit exceeds the supported bound"
  [ "$requested_max" -ge "$current_max" ] ||
    fail "requested cap $requested_max would lower the current cap $current_max"
  coverage="$(print_coverage "$requested_max")"
  printf '%s\n' "$coverage"
  completed_count="$(
    printf '%s\n' "$coverage" |
      sed -n 's/^semantic_current_profile_completed_count=//p'
  )"
  [[ "$completed_count" =~ ^[0-9]+$ ]] || fail "could not verify the completed canary count"
  [ "$completed_count" -ge 1 ] ||
    fail "promotion requires at least one completed current-profile canary assessment"
  projected_count="$(
    printf '%s\n' "$coverage" |
      sed -n 's/^semantic_projected_new_assessment_count=//p'
  )"
  [[ "$projected_count" =~ ^[0-9]+$ ]] || fail "could not verify the projected backlog"
  [ "$projected_count" -le "$max_initial_backlog" ] ||
    fail "projected initial backlog $projected_count exceeds the hard limit $max_initial_backlog"
  current_mode="$(env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE "$controller_env")"
  current_allowlist="$(
    env_value GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST "$controller_env"
  )"
  if [ "$current_enabled" = true ] && [ "$current_mode" = shadow ] &&
    [ "$current_max" = "$requested_max" ] && [ "$current_allowlist" = "[]" ]; then
    wait_for_stack || fail "requested promotion is configured but the stack is not ready"
    printf '%s\n' "Semantic anatomy is already promoted with the requested settings."
    print_status
    exit 0
  fi
elif [ "$current_enabled" = false ]; then
  printf '%s\n' \
    "Semantic anatomy assessment creation is already paused." \
    "semantic_anatomy_enabled=false" \
    "control_plane_health=$(health_status http://127.0.0.1:8000/api/v1/health/ready)" \
    "semantic_gateway_health=$(health_status http://127.0.0.1:8091/health/ready)"
  exit 0
fi

backup_env="$(mktemp "$config_root/.control-plane.env.semantic.rollback.XXXXXX")"
temporary_env="$(mktemp "$config_root/.control-plane.env.semantic.update.XXXXXX")"
install -o root -g root -m 0600 "$controller_env" "$backup_env"
python3 - "$controller_env" "$temporary_env" "$requested_max" "$operation" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
maximum = sys.argv[3]
operation = sys.argv[4]
if operation == "pause":
    updates = {"GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED": "false"}
elif operation == "promote":
    updates = {
        "GEN_AUTOMATION_SEMANTIC_ANATOMY_ENABLED": "true",
        "GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE": "shadow",
        "GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE": maximum,
        "GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST": "[]",
    }
else:
    raise SystemExit("unsupported semantic anatomy operation")
lines = source.read_text(encoding="utf-8").splitlines()
for key, value in updates.items():
    matches = [index for index, line in enumerate(lines) if line.startswith(f"{key}=")]
    if len(matches) != 1:
        raise SystemExit(f"{source} must define {key} exactly once")
    lines[matches[0]] = f"{key}={value}"
with destination.open("w", encoding="utf-8") as output:
    output.write("\n".join(lines) + "\n")
    output.flush()
    os.fsync(output.fileno())
PY
chown root:root "$temporary_env"
chmod 0600 "$temporary_env"

rollback_armed=1
mv -- "$temporary_env" "$controller_env"
temporary_env=""
"$validator"
/usr/bin/docker compose \
  --env-file "$deploy_env" \
  -f "$compose_file" \
  config --quiet
if [ "$operation" = "pause" ]; then
  # Once the disabled environment validates, never restore enabled=true merely
  # because an already-degraded runtime cannot become healthy. The durable config
  # is the emergency safety control; health is reported independently.
  rollback_armed=0
  rm -f -- "$backup_env"
  backup_env=""
  systemctl restart --no-block "$service_name" || true
  printf '%s\n' \
    "Semantic anatomy assessment creation paused; existing rows were preserved." \
    "semantic_anatomy_enabled=false" \
    "control_plane_health=$(health_status http://127.0.0.1:8000/api/v1/health/ready)" \
    "semantic_gateway_health=$(health_status http://127.0.0.1:8091/health/ready)"
else
  systemctl restart --no-block "$service_name"
  wait_for_stack || fail "$operation anatomy configuration did not become ready"
  rollback_armed=0
  rm -f -- "$backup_env"
  backup_env=""
  printf '%s\n' "Semantic anatomy coverage promoted in shadow mode."
  print_status
fi
