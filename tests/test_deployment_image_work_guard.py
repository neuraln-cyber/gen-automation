from pathlib import Path

import pytest
import sqlalchemy as sa

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "infra/aws-staging/deploy/update-control-plane.sh"
WORKFLOW = ROOT / ".github/workflows/deploy-staging.yml"


def _guard():
    script = UPDATER.read_text(encoding="utf-8")
    program = script.split("<<'IMAGE_WORK_PREFLIGHT' ||\n", 1)[1].split(
        "\nIMAGE_WORK_PREFLIGHT\n", 1
    )[0]
    namespace = {"__name__": "deployment_guard_test"}
    exec(compile(program, str(UPDATER), "exec"), namespace)  # noqa: S102 - reviewed local payload.
    return namespace


@pytest.mark.parametrize(
    ("kind", "state", "phase", "version", "blocked"),
    [
        ("job", "running", "generating", 1, True),
        ("job", "collecting", "generating", 1, True),
        ("job", "verifying", "generating", 1, True),
        ("job", "unknown", "cancelled", 1, True),
        ("attempt", "created", "generating", 1, True),
        ("attempt", "submitted", "generating", 1, True),
        ("attempt", "unknown", "cancelled", 1, True),
        ("attempt", "cancel_requested", "cancelled", 1, True),
        ("job", "queued", "ready", 1, True),
        ("job", "retry_wait", "paused", 1, True),
        ("job", "queued", "cancelled", 1, False),
        ("job", "queued", "generating", 2, False),
        ("job", "succeeded", "reviewing", 1, False),
        ("attempt", "failed", "cancelled", 1, False),
    ],
)
def test_deployment_guard_refuses_image_work_without_mutating_rows(
    kind, state, phase, version, blocked
) -> None:
    guard = _guard()
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    releases = sa.Table(
        "releases",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("phase", sa.String),
        sa.Column("current_version_no", sa.Integer),
        sa.Column("lock_version", sa.Integer, default=1),
    )
    versions = sa.Table(
        "release_versions",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("release_id", sa.Integer),
        sa.Column("version_no", sa.Integer),
    )
    jobs = sa.Table(
        "generation_jobs",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String),
        sa.Column("state", sa.String),
        sa.Column("release_version_id", sa.Integer),
    )
    attempts = sa.Table(
        "generation_attempts",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String),
        sa.Column("state", sa.String),
    )
    sa.Table(
        "audit_events",
        metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("resource_type", sa.String),
        sa.Column("resource_id", sa.Integer),
        sa.Column("action", sa.String),
        sa.Column("correlation_id", sa.String),
    )
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(releases.insert(), {"id": 1, "phase": phase, "current_version_no": 1})
        connection.execute(versions.insert(), {"id": 1, "release_id": 1, "version_no": version})
        table = jobs if kind == "job" else attempts
        row = {"id": 1, "provider": "salad", "state": state}
        if kind == "job":
            row["release_version_id"] = 1
        connection.execute(table.insert(), row)
        before = list(connection.execute(sa.select(table)))
        snapshot = guard["image_work_snapshot"](connection)
        assert any(snapshot.values()) is blocked
        assert list(connection.execute(sa.select(table))) == before
    engine.dispose()


@pytest.mark.parametrize(
    ("phase", "lock_version", "marker_version", "resumed", "active", "blocked"),
    [
        ("paused", 2, 1, False, False, False),
        ("ready", 2, 1, False, False, True),
        ("generating", 2, 1, False, False, True),
        ("paused", 3, 1, False, False, True),
        ("paused", 2, 2, False, False, True),
        ("paused", 2, 1, True, False, True),
        ("paused", 2, 1, False, True, True),
    ],
)
def test_only_exact_unreleased_maintenance_pause_exempts_preserved_queue(
    phase, lock_version, marker_version, resumed, active, blocked
) -> None:
    guard = _guard()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE releases (id INTEGER, phase TEXT, current_version_no INTEGER, "
            "lock_version INTEGER)",
            "CREATE TABLE release_versions (id INTEGER, release_id INTEGER, version_no INTEGER)",
            "CREATE TABLE generation_jobs (id INTEGER, provider TEXT, state TEXT, "
            "release_version_id INTEGER)",
            "CREATE TABLE generation_attempts (id INTEGER, provider TEXT, state TEXT)",
            "CREATE TABLE audit_events (id INTEGER, resource_type TEXT, resource_id INTEGER, "
            "action TEXT, correlation_id TEXT)",
        ):
            connection.exec_driver_sql(statement)
        connection.execute(
            sa.text("INSERT INTO releases VALUES (1, :phase, 1, :lock_version)"),
            {"phase": phase, "lock_version": lock_version},
        )
        connection.exec_driver_sql("INSERT INTO release_versions VALUES (1, 1, 1)")
        connection.exec_driver_sql("INSERT INTO generation_jobs VALUES (1, 'salad', 'queued', 1)")
        connection.execute(
            sa.text(
                "INSERT INTO audit_events VALUES "
                "(1, 'release', 1, 'release.generation_maintenance_paused', :correlation)"
            ),
            {"correlation": f"image-maintenance:{marker_version}:2"},
        )
        if resumed:
            connection.exec_driver_sql(
                "INSERT INTO audit_events VALUES "
                "(2, 'release', 1, 'release.generation_maintenance_resumed', "
                "'image-maintenance:1:2')"
            )
        if active:
            connection.exec_driver_sql(
                "INSERT INTO generation_attempts VALUES (1, 'salad', 'running')"
            )
        assert any(guard["image_work_snapshot"](connection).values()) is blocked
        assert connection.scalar(sa.text("SELECT state FROM generation_jobs")) == "queued"
    engine.dispose()


def test_deployment_guard_fails_closed_without_disclosing_connection_errors(
    monkeypatch, capsys
) -> None:
    guard = _guard()
    private = "credential-must-not-appear"
    monkeypatch.setenv("GEN_AUTOMATION_DATABASE_URL", f"postgresql+psycopg://user:{private}@db/app")

    def unavailable(*args, **kwargs):
        raise RuntimeError(private)

    guard["create_engine"] = unavailable
    assert guard["main"]() == 2
    output = capsys.readouterr()
    assert "could not verify idle image work" in output.err
    assert private not in output.out + output.err


def test_deployment_guard_is_read_only_bounded_and_runs_before_stop_or_replacement() -> None:
    script = UPDATER.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "SET TRANSACTION READ ONLY" in script
    assert "SET LOCAL statement_timeout = '10s'" in script
    assert "SET LOCAL lock_timeout = '2s'" in script
    assert 'connect_args={"connect_timeout": 5}' in script
    assert "timeout --signal=TERM --kill-after=5s 45s" in script
    assert '--entrypoint python3.12 "$old_image" -' in script
    assert '--env-file "$config_root/migration.env"' in script
    assert "--check-idle-only)" in script
    assert script.index("\nassert_image_work_idle\nbackup_env=") < script.index(
        "\nrollback_armed=1\n"
    )
    command = workflow.split("unlocked_command = (", 1)[1].split("\n          command = (", 1)[0]
    assert command.index("{refresh_host_bootstrap}") < command.index("{image_work_preflight}")
    assert command.index("{image_work_preflight}") < command.index("{backup_runtime_env}")
    assert command.index("{image_work_preflight}") < command.index("{stop_control_plane}")
    assert "--check-idle-only --external-lock-held" in workflow
    assert "not an atomic admission/drain lock" in script
