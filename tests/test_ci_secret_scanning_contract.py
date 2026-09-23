import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
CONFIG_PATH = ROOT / ".gitleaks.toml"

GITLEAKS_VERSION = "8.30.1"
GITLEAKS_LINUX_X64_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
CHECKOUT_PIN = "3d3c42e5aac5ba805825da76410c181273ba90b1"


def _ci() -> str:
    return CI_PATH.read_text(encoding="utf-8")


def _secret_job(ci: str) -> str:
    match = re.search(r"(?ms)^  secrets:\n(?P<body>.*?)(?=^  verify:\n)", ci)
    assert match is not None
    return match.group("body")


def test_secret_scan_fetches_complete_history_with_a_pinned_checkout() -> None:
    job = _secret_job(_ci())

    assert f"actions/checkout@{CHECKOUT_PIN}" in job
    assert "fetch-depth: 0" in job
    assert "persist-credentials: false" in job
    assert "gitleaks/gitleaks-action@" not in job


def test_gitleaks_release_archive_is_exactly_pinned_and_verified_before_use() -> None:
    job = _secret_job(_ci())

    assert f'GITLEAKS_VERSION: "{GITLEAKS_VERSION}"' in job
    assert f'GITLEAKS_ARCHIVE_SHA256: "{GITLEAKS_LINUX_X64_SHA256}"' in job
    assert (
        "https://github.com/gitleaks/gitleaks/releases/download/"
        "v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
    ) in job
    assert "--proto '=https' --tlsv1.2" in job
    assert "sha256sum --check --strict -" in job
    assert job.index("sha256sum --check --strict -") < job.index("tar --extract --gzip")
    assert 'test "$("${binary}" version)" = "${GITLEAKS_VERSION}"' in job


def test_secret_scan_is_fail_closed_redacted_and_bounded() -> None:
    job = _secret_job(_ci())

    assert '"${RUNNER_TEMP}/gitleaks" git' in job
    assert "--config .gitleaks.toml" in job
    assert '--log-opts="--all"' in job
    assert 'test "$(git rev-list --all --count)" -gt 0' in job
    assert 'test ! -e "$(git rev-parse --git-path shallow)"' in job
    assert "git rev-list --objects --all" in job
    assert "$2 > 20971520" in job
    assert "Secret scan refuses Git blobs larger than 20 MiB" in job
    assert "--max-archive-depth=1" in job
    assert "--max-decode-depth=5" in job
    assert "--max-target-megabytes=20" in job
    assert "--timeout=300" in job
    assert "--redact=100" in job
    assert "--verbose" in job
    assert "|| true" not in job
    assert "continue-on-error" not in job


def test_secret_scanner_extends_defaults_with_only_narrow_non_secret_exceptions() -> None:
    config = CONFIG_PATH.read_text(encoding="utf-8")

    assert "useDefault = true" in config
    assert config.count("[[rules.allowlists]]") == 3
    assert 'id = "generic-api-key"' in config
    assert 'condition = "AND"' in config
    assert 'regexTarget = "line"' in config
    for path in (
        "tests/test_admin_bootstrap",
        "tests/test_compliance_api",
        "tests/test_compliance_registry",
        "tests/test_salad_service",
        "tests/test_x_teaser_revisions",
    ):
        assert path in config
    for fixture in (
        "scheduler-claim-1",
        "subject-reaffirm-v1",
        "subject-revoke-v2",
        "approve-subject-v1",
        "revoke-subject-v1",
        "v1\\.key-1\\.corrupt",
        "x-idempotency-k[12]",
        "x-idempotency-k2",
    ):
        assert fixture in config
    assert "[[allowlists]]" not in config


def test_historical_patch_checksum_exception_rejects_other_files_and_values() -> None:
    config = tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    rules = config["rules"]
    assert len(rules) == 1
    assert rules[0]["id"] == "generic-api-key"
    allowlists = rules[0]["allowlists"]
    assert len(allowlists) == 3
    for allowlist in allowlists:
        assert allowlist["condition"] == "AND"
        assert allowlist["regexTarget"] == "line"
    checksum_exception = allowlists[2]
    assert set(checksum_exception) == {
        "description",
        "condition",
        "regexTarget",
        "paths",
        "regexes",
    }
    assert checksum_exception["paths"] == [r"(^|/)Dockerfile\.worker$"]
    assert len(checksum_exception["regexes"]) == 2

    def matches(path: str, line: str) -> bool:
        return any(re.search(pattern, path) for pattern in checksum_exception["paths"]) and any(
            re.search(pattern, line) for pattern in checksum_exception["regexes"]
        )

    checksum = "916f22c8075481a2924aabc2f1d1b57d891681eb5bd10b6081487df7b57a336d"
    for line in (
        f"SALAD_QUEUE_WORKER_TOKEN_PATCH_SHA256={checksum}",
        f'org.opencontainers.image.salad-queue-worker.token-patch-sha256="{checksum}"',
    ):
        assert matches("Dockerfile.worker", line)
        assert matches("project/Dockerfile.worker", f"  {line}  ")
        for path in ("Dockerfile", "Dockerfile.worker.bak", "OtherDockerfile.worker"):
            assert not matches(path, line)
        assert not matches("Dockerfile.worker", line.replace(checksum, "0" * 64))
        assert not matches("Dockerfile.worker", f"{line} extra-secret")
        assert not matches("Dockerfile.worker", f"other-secret {line}")
    assert not matches("Dockerfile.worker", f"API_TOKEN={checksum}")


def test_ci_executes_the_complete_migration_path_on_postgresql() -> None:
    ci = _ci()

    assert "postgresql-migrations:" in ci
    assert "image: postgres:16.14-alpine3.24" in ci
    assert "postgresql+psycopg://" in ci
    assert "python -m alembic upgrade head" in ci
    assert "python -m alembic downgrade base" in ci
    assert ci.count("python -m alembic check") >= 2
