import re
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "infra" / "aws-staging" / "deploy"


def _text(name: str) -> str:
    return (DEPLOY / name).read_text(encoding="utf-8")


def _service(compose: str, name: str, next_section: str) -> str:
    return compose.split(f"\n  {name}:\n", maxsplit=1)[1].split(
        f"\n  {next_section}:\n",
        maxsplit=1,
    )[0]


def test_staging_images_are_runtime_required_immutable_digests() -> None:
    compose = _text("compose.yaml")
    deploy_example = _text("deploy.env.example")
    validator = _text("validate-deployment.sh")

    for key in (
        "GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE",
        "GEN_AUTOMATION_PATREON_BROWSER_IMAGE",
        "GEN_AUTOMATION_SEMANTIC_GATEWAY_IMAGE",
        "GEN_AUTOMATION_NGINX_IMAGE",
        "GEN_AUTOMATION_CADDY_IMAGE",
    ):
        assert f'image: "${{{key}:?set a repository@sha256:64-hex digest}}"' in compose
        assert re.search(rf"(?m)^{key}=$", deploy_example)
        assert key in validator
    assert "@sha256:[0-9a-f]{64}" in validator
    assert "build:" not in compose
    assert ":latest" not in compose


def test_staging_salad_prefetch_runway_is_pinned_to_three_jobs() -> None:
    controller = _text("control-plane.env.example")
    validator = _text("validate-deployment.sh")

    assert re.search(
        r"(?m)^GEN_AUTOMATION_SALAD_MAX_QUEUED_JOBS=3$",
        controller,
    )
    assert (
        '[ "$(env_value GEN_AUTOMATION_SALAD_MAX_QUEUED_JOBS '
        '"$config_root/control-plane.env")" = "3" ] ||' in validator
    )
    assert "prefetch must be exactly 3" in validator


def test_staging_salad_container_priority_is_pinned_high() -> None:
    controller = _text("control-plane.env.example")
    validator = _text("validate-deployment.sh")

    assert re.search(
        r"(?m)^GEN_AUTOMATION_SALAD_CONTAINER_PRIORITY=high$",
        controller,
    )
    assert (
        '[ "$(env_value GEN_AUTOMATION_SALAD_CONTAINER_PRIORITY '
        '"$config_root/control-plane.env")" = "high" ] ||' in validator
    )
    assert "container priority must be exactly high" in validator


def test_staging_salad_budget_supports_three_bounded_high_priority_reservations() -> None:
    controller = _text("control-plane.env.example")
    validator = _text("validate-deployment.sh")

    expected = {
        "GEN_AUTOMATION_SALAD_MAX_HOURLY_COST_USD": "0.35",
        "GEN_AUTOMATION_SALAD_DAILY_BUDGET_USD": "5.00",
        "GEN_AUTOMATION_SALAD_MONTHLY_BUDGET_USD": "25.00",
    }
    for name, value in expected.items():
        assert re.search(rf"(?m)^{name}={re.escape(value)}$", controller)
        assert (
            f'[ "$(env_value {name} "$config_root/control-plane.env")" = "{value}" ] ||'
            in validator
        )
    assert Decimal(expected["GEN_AUTOMATION_SALAD_MAX_HOURLY_COST_USD"]) * 3 <= Decimal(
        expected["GEN_AUTOMATION_SALAD_DAILY_BUDGET_USD"]
    )
    assert Decimal(expected["GEN_AUTOMATION_SALAD_DAILY_BUDGET_USD"]) <= Decimal(
        expected["GEN_AUTOMATION_SALAD_MONTHLY_BUDGET_USD"]
    )


def test_staging_salad_attempt_watchdog_is_pinned_before_signature_expiry() -> None:
    controller = _text("control-plane.env.example")
    validator = _text("validate-deployment.sh")

    assert re.search(
        r"(?m)^GEN_AUTOMATION_SALAD_ATTEMPT_WATCHDOG_SECONDS=6300$",
        controller,
    )
    assert (
        '[ "$(env_value GEN_AUTOMATION_SALAD_ATTEMPT_WATCHDOG_SECONDS '
        '"$config_root/control-plane.env")" = "6300" ] ||' in validator
    )
    assert "attempt watchdog must be exactly 6300 seconds" in validator


def test_imds_network_boundary_and_loopback_ingress_are_explicit() -> None:
    compose = _text("compose.yaml")
    caddyfile = _text("Caddyfile")
    nginx = _text("nginx.conf")
    controller = _service(compose, "control-plane-mega", "ingress-guard")
    ingress = _service(compose, "ingress-guard", "caddy")
    patreon = _service(compose, "patreon-browser", "semantic-gateway")
    semantic = _service(compose, "semantic-gateway", "control-plane-mega")

    assert "network_mode: host" in controller
    assert '\n      - --host\n      - "127.0.0.1"' in controller
    assert "ports:" not in controller
    assert "http://127.0.0.1:8090/v1/publish" in controller
    assert "/var/lib/gen-automation/integration-profiles/mega" in controller
    assert "/run/gen-automation/mega-profile" in controller

    assert "network_mode:" not in patreon
    assert "patreon-egress" in patreon
    assert '"127.0.0.1:8090:8090/tcp"' in patreon
    assert "/patreon-browser/profiles" in patreon
    assert "target: /profiles" in patreon
    assert "/patreon-browser/state" in patreon
    assert "target: /state" in patreon
    assert "AWS_ACCESS_KEY" not in patreon
    assert "AWS_SECRET" not in patreon

    assert "network_mode:" not in semantic
    assert "semantic-egress" in semantic
    assert '"127.0.0.1:8091:8080/tcp"' in semantic
    assert "AWS_ACCESS_KEY" not in semantic
    assert "AWS_SECRET" not in semantic
    assert "/var/run/docker.sock" not in semantic
    assert "http://127.0.0.1:8091/v1/anatomy/assess" in controller

    assert 'user: "10003:10003"' in ingress
    assert "network_mode: host" in ingress
    assert "listen 127.0.0.1:8080;" in nginx
    assert "server 127.0.0.1:8000;" in nginx

    assert compose.count("network_mode: host") == 3
    assert "driver: bridge" in compose
    assert "reverse_proxy 127.0.0.1:8080" in caddyfile
    assert "protocols h1 h2" in caddyfile
    assert "admin off" in caddyfile
    assert "admin 127.0.0.1" not in caddyfile


def test_loopback_nginx_request_guards_and_assertions_are_enforced() -> None:
    caddyfile = _text("Caddyfile")
    nginx = _text("nginx.conf")
    proxy_validator = _text("validate-proxy-images.sh")
    controller = _text("control-plane.env.example")
    deployment_validator = _text("validate-deployment.sh")
    unit = _text("gen-automation-staging.service")

    assert "limit_req_zone $guard_client zone=all_requests_per_client" in nginx
    assert "limit_req_zone $guard_client zone=authentication_per_client" in nginx
    assert "limit_conn_zone $guard_client zone=connections_per_client" in nginx
    assert "location = /login" in nginx
    assert "location ^~ /api/v1/auth/" in nginx
    assert "client_max_body_size 8m" in nginx
    for temporary_path in ("fastcgi", "proxy", "scgi", "uwsgi"):
        assert f"{temporary_path}_temp_path /tmp/{temporary_path}" in nginx
    assert "large_client_header_buffers 4 8k" in nginx
    for timeout in (
        "client_header_timeout 10s",
        "client_body_timeout 30s",
        "send_timeout 60s",
        "keepalive_timeout 120s",
    ):
        assert timeout in nginx
    assert "max_header_size 32KB" in caddyfile
    for timeout in ("read_header 10s", "read_body 30s", "write 60s", "idle 2m"):
        assert timeout in caddyfile
    for header in (
        "Forwarded",
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Real-IP",
    ):
        assert f"request_header -{header}" in caddyfile
    assert "header_up X-Forwarded-For {remote_host}" in caddyfile
    assert "proxy_set_header X-Forwarded-For $guard_client" in nginx

    assert "--network none" in proxy_validator
    assert "--user 10002:10002" in proxy_validator
    assert "--cap-add NET_BIND_SERVICE" in proxy_validator
    assert "file-server --listen :81" in proxy_validator
    assert "/data:rw,nosuid,nodev,noexec,size=64m,uid=10002,gid=10002,mode=0700" in (
        proxy_validator
    )
    assert "/config:rw,nosuid,nodev,noexec,size=16m,uid=10002,gid=10002,mode=0700" in (
        proxy_validator
    )
    assert "caddy_image" in proxy_validator
    assert "nginx_image" in proxy_validator
    assert " validate --config /etc/caddy/Caddyfile" in proxy_validator
    assert "-t -c /etc/nginx/nginx.conf" in proxy_validator
    assert "ExecStartPre=/usr/local/libexec/gen-automation-validate-proxy-images" in unit
    assert unit.index(" pull --quiet") < unit.index("validate-proxy-images")

    for key in (
        "GEN_AUTOMATION_INGRESS_RATE_LIMIT_CONFIGURED",
        "GEN_AUTOMATION_INGRESS_REQUEST_GUARDS_CONFIGURED",
    ):
        assert f"{key}=true" in controller
        assert key in deployment_validator


def test_containers_are_ordered_health_checked_and_not_privileged() -> None:
    compose = _text("compose.yaml")
    controller = _service(compose, "control-plane-mega", "ingress-guard")
    ingress = _service(compose, "ingress-guard", "caddy")
    patreon = _service(compose, "patreon-browser", "semantic-gateway")
    semantic = _service(compose, "semantic-gateway", "control-plane-mega")
    caddy = _service(compose, "caddy", "patreon-egress")

    for service in (patreon, semantic, controller, ingress, caddy):
        assert "restart: unless-stopped" in service
        assert "healthcheck:" in service
        assert "cap_drop:\n      - ALL" in service
        assert "read_only: true" in service
        assert "privileged:" not in service
        assert "/var/run/docker.sock" not in service
        assert "/run/docker.sock" not in service

    for service in (patreon, semantic, controller, ingress):
        assert "no-new-privileges:true" in service
    assert "no-new-privileges:true" not in caddy
    assert "cap_add:\n      - NET_BIND_SERVICE" in caddy
    assert "patreon-browser:\n        condition: service_healthy" in controller
    assert "semantic-gateway:\n        condition: service_healthy" in controller
    assert "control-plane-mega:\n        condition: service_healthy" in ingress
    assert "ingress-guard:\n        condition: service_healthy" in caddy
    assert "restart: true" in controller
    assert "restart: true" in ingress
    assert "restart: true" in caddy


def test_systemd_activation_validates_pulls_and_joins_prepared_target() -> None:
    unit = _text("gen-automation-staging.service")
    installer = _text("install.sh")
    validator = _text("validate-deployment.sh")

    assert "After=docker.service gen-automation-imds-egress.service network-online.target" in unit
    assert "Requires=docker.service gen-automation-imds-egress.service" in unit
    assert "PartOf=gen-automation-deploy.target" in unit
    assert "RequiresMountsFor=/var/lib/gen-automation/integration-profiles" in unit
    assert "ExecStartPre=/usr/local/libexec/gen-automation-validate-deployment" in unit
    assert " compose " in unit
    assert " config --quiet" in unit
    assert " pull --quiet" in unit
    assert "gen-automation-validate-proxy-images" in unit
    assert " up --remove-orphans" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=gen-automation-deploy.target" in unit
    assert unit.index("[Unit]") < unit.index("StartLimitIntervalSec=300") < unit.index("[Service]")

    assert "systemctl daemon-reload" in installer
    assert "systemctl enable" not in installer
    assert "systemctl start" not in installer
    assert '"$config_root/examples"' in installer
    assert "require_private_root_file" in validator
    assert "require_service_directory" in validator
    assert 'require_service_directory "$directory" "10001:10001"' in validator
    assert 'require_service_directory "$directory" "10002:10002"' in validator
    assert "mode 0400 or 0600" in validator
    assert "repository@sha256:64-hex" in validator


def test_host_network_edge_uids_are_non_root_and_blocked_from_imds() -> None:
    compose = _text("compose.yaml")
    firewall_script = _text("install-imds-egress-rules.sh")
    firewall_unit = _text("gen-automation-imds-egress.service")
    installer = _text("install.sh")
    validator = _text("validate-deployment.sh")

    ingress = _service(compose, "ingress-guard", "caddy")
    caddy = _service(compose, "caddy", "patreon-egress")
    assert 'user: "10003:10003"' in ingress
    assert 'user: "10002:10002"' in caddy
    assert "network_mode: host" in ingress
    assert "network_mode: host" in caddy
    assert "169.254.169.254/32" in firewall_script
    assert "--match owner" in firewall_script
    assert "blocked_uid in 10002 10003" in firewall_script
    for blocked_uid in ("10002", "10003"):
        assert blocked_uid in validator
    assert "10001" not in firewall_script
    assert "RemainAfterExit=yes" in firewall_unit
    assert "Before=gen-automation-staging.service" in firewall_unit
    assert "gen-automation-install-imds-egress-rules" in installer
    assert "gen-automation-imds-egress.service" in installer


def test_al2023_compose_plugin_is_pinned_checksum_verified_and_installed() -> None:
    compose_installer = _text("install-compose-plugin.sh")
    bundle_installer = _text("install.sh")
    validator = _text("validate-deployment.sh")

    version = "5.1.2"
    checksum = "c372e512a36e67716b0b3a1264ccdc461dec7a7beff601b81f7c5fb008e3511e"
    for text in (compose_installer, validator):
        assert version in text
        assert checksum in text
    assert "docker-compose-linux-x86_64" in compose_installer
    assert "https://github.com/docker/compose/releases/download/" in compose_installer
    assert "--proto '=https'" in compose_installer
    assert "sha256sum --check --status" in compose_installer
    assert "/usr/local/lib/docker/cli-plugins" in compose_installer
    assert "/usr/bin/docker compose version --short" in compose_installer
    assert "/usr/local/sbin/gen-automation-install-compose-plugin" in bundle_installer
    assert "/usr/local/lib/docker/cli-plugins/docker-compose" in validator


def test_rds_ca_bundle_is_pinned_verified_and_mounted_read_only() -> None:
    compose = _text("compose.yaml")
    installer = _text("install.sh")
    validator = _text("validate-deployment.sh")
    controller = _service(compose, "control-plane-mega", "ingress-guard")

    checksum = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"
    assert "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem" in installer
    for text in (installer, validator):
        assert checksum in text
        assert "sha256sum --check --status" in text
    assert "source: /etc/gen-automation/rds-global-bundle.pem" in controller
    assert "target: /run/gen-automation/rds-global-bundle.pem" in controller
    assert "read_only: true" in controller


def test_environment_templates_contain_placeholders_not_secret_values() -> None:
    controller = _text("control-plane.env.example")
    patreon = _text("patreon-browser.env.example")
    semantic = _text("semantic-gateway.env.example")
    caddy = _text("caddy.env.example")

    for key in (
        "GEN_AUTOMATION_DATABASE_URL",
        "GEN_AUTOMATION_SESSION_SECRET",
        "GEN_AUTOMATION_SALAD_API_KEY",
        "GEN_AUTOMATION_SALAD_WEBHOOK_SECRET",
        "GEN_AUTOMATION_WORKER_SIGNING_PRIVATE_KEY",
        "GEN_AUTOMATION_PATREON_BROWSER_SHARED_SECRET",
        "GEN_AUTOMATION_X_OAUTH_SECRET_REFERENCE",
    ):
        assert re.search(rf"(?m)^{key}=$", controller)
    assert re.search(r"(?m)^GEN_AUTOMATION_PATREON_BROWSER_SHARED_SECRET=$", patreon)
    assert re.search(
        r"(?m)^GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_API_KEY=$",
        semantic,
    )
    assert re.search(
        r"(?m)^GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_CHAT_COMPLETIONS_URL=$",
        semantic,
    )
    assert re.search(r"(?m)^GEN_AUTOMATION_HOSTNAME=$", caddy)
    assert "GEN_AUTOMATION_STORAGE_ACCESS_KEY_ID=" not in controller
    assert "GEN_AUTOMATION_STORAGE_SECRET_ACCESS_KEY=" not in controller
    assert "GEN_AUTOMATION_STORAGE_SESSION_TOKEN=" not in controller
    assert "GEN_AUTOMATION_BACKGROUND_PUBLICATION_MAX_PACKAGE_BYTES=167772160" in controller
    assert "GEN_AUTOMATION_PATREON_BROWSER_MAX_PACKAGE_BYTES=167772160" in patreon
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_MODE=shadow" in controller
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_MAX_ASSESSMENTS_PER_PROFILE=0" in controller
    assert "GEN_AUTOMATION_SEMANTIC_ANATOMY_ASSET_ALLOWLIST=[]" in controller
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_REQUEST_TIMEOUT_SECONDS=630" in controller
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_LEASE_SECONDS=720" in controller
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_MAX_ATTEMPTS=5" in controller
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_BASE_SECONDS=30" in controller
    assert "GEN_AUTOMATION_BACKGROUND_SEMANTIC_RETRY_MAX_SECONDS=120" in controller
    assert "GEN_AUTOMATION_SEMANTIC_GATEWAY_UPSTREAM_TIMEOUT_SECONDS=600" in semantic
    assert "GEN_AUTOMATION_SEMANTIC_GATEWAY_MAX_IMAGE_BYTES=12582912" in semantic
    assert (
        "GEN_AUTOMATION_SEMANTIC_ANATOMY_ENDPOINT_URL="
        "http://127.0.0.1:8091/v1/anatomy/assess" in controller
    )


def test_single_owner_staging_session_is_persistent_but_step_up_stays_short() -> None:
    controller = _text("control-plane.env.example")

    assert "GEN_AUTOMATION_AUTH_SESSION_ABSOLUTE_SECONDS=7776000" in controller
    assert "GEN_AUTOMATION_AUTH_SESSION_IDLE_SECONDS=2592000" in controller
    assert "GEN_AUTOMATION_AUTH_RECENT_AUTH_SECONDS=3600" in controller


def test_patreon_profile_has_an_ssm_only_cloud_bootstrap_path() -> None:
    bootstrap_compose = _text("compose.bootstrap.yaml")
    bootstrap_command = _text("bootstrap-patreon-profile.sh")
    installer = _text("install.sh")
    dockerfile = (ROOT / "Dockerfile.patreon-browser").read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "patreon-browser-publisher.md").read_text(encoding="utf-8")

    assert "gen_automation.patreon_browser.bootstrap" in bootstrap_compose
    assert '"127.0.0.1:6080:6080/tcp"' in bootstrap_compose
    assert "patreon-bootstrap-egress" in bootstrap_compose
    assert "privileged:" not in bootstrap_compose
    assert "/var/run/docker.sock" not in bootstrap_compose
    assert "cap_drop:\n      - ALL" in bootstrap_compose
    assert "no-new-privileges:true" in bootstrap_compose
    assert "systemctl stop" in bootstrap_command
    assert "--service-ports" in bootstrap_command
    assert "trap restore_runtime" in bootstrap_command
    assert "gen-automation-bootstrap-patreon-profile" in installer
    for package in ("novnc", "websockify", "x11vnc", "xvfb"):
        assert package in dockerfile
    assert "AWS-StartPortForwardingSession" in runbook
    assert "127.0.0.1:6080/vnc.html" in runbook


def test_mega_profile_has_a_private_interactive_bootstrap_path() -> None:
    bootstrap_compose = _text("compose.bootstrap.yaml")
    bootstrap_command = _text("bootstrap-mega-profile.sh")
    installer = _text("install.sh")
    runbook = (ROOT / "docs" / "mega-profile-bootstrap.md").read_text(encoding="utf-8")

    assert "mega-profile-bootstrap:" in bootstrap_compose
    assert "GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE" in bootstrap_compose
    assert "HOME: /run/gen-automation/mega-profile" in bootstrap_compose
    assert "umask 077; exec mega-cmd" in bootstrap_compose
    assert "stdin_open: true" in bootstrap_compose
    assert "tty: true" in bootstrap_compose
    assert "mega-bootstrap-egress" in bootstrap_compose
    assert "/var/lib/gen-automation/integration-profiles/mega" in bootstrap_compose
    assert (
        "env_file:"
        not in bootstrap_compose.split("  mega-profile-bootstrap:", maxsplit=1)[1].split(
            "  patreon-browser-bootstrap:", maxsplit=1
        )[0]
    )
    assert "network_mode: host" not in bootstrap_compose
    assert "privileged:" not in bootstrap_compose
    assert "/var/run/docker.sock" not in bootstrap_compose

    assert "[ -t 0 ] && [ -t 1 ]" in bootstrap_command
    assert "control-plane-mega@sha256:[0-9a-f]{64}" in bootstrap_command
    assert "systemctl stop" in bootstrap_command
    assert "trap restore_runtime" in bootstrap_command
    assert "mega-whoami" in bootstrap_command
    assert "mega-https on" in bootstrap_command
    assert 'mega-ls "$remote_root"' in bootstrap_command
    assert "GEN_AUTOMATION_MEGA_REMOTE_ROOT" in bootstrap_command
    assert "--verify-only" in bootstrap_command
    assert "--skip-https" in bootstrap_command
    assert "mega-logout" in bootstrap_command
    assert "mega-login" not in bootstrap_command
    assert "read -" not in bootstrap_command
    assert "PASSWORD" not in bootstrap_command
    assert "SESSION" not in bootstrap_command
    assert "AUTH_KEY" not in bootstrap_command
    assert 'find "$profile_cache" -xdev' in bootstrap_command
    assert "-perm /077" in bootstrap_command

    assert "gen-automation-bootstrap-mega-profile" in installer
    assert '"$source_dir/bootstrap-mega-profile.sh"' in installer
    assert "quit --only-shell" in runbook
    assert "--verify-only" in runbook
    assert "Do not run `logout`" in runbook


def test_owner_bootstrap_wrapper_is_tty_only_and_digest_pinned() -> None:
    bootstrap = _text("bootstrap-owner.sh")
    installer = _text("install.sh")

    assert "[ -t 0 ] && [ -t 1 ]" in bootstrap
    assert "gen-automation-validate-deployment" in bootstrap
    assert "GEN_AUTOMATION_CONTROL_PLANE_MEGA_IMAGE" in bootstrap
    assert "@sha256:[0-9a-f]{64}" in bootstrap
    assert '--env-file "$config_root/bootstrap-owner.env"' in bootstrap
    assert "rds-global-bundle.pem,readonly" in bootstrap
    assert "--read-only" in bootstrap
    assert "--cap-drop ALL" in bootstrap
    assert "python3.12 -m gen_automation.cli bootstrap-owner" in bootstrap
    assert "gen-automation-bootstrap-owner" in installer


def test_patreon_browser_uses_checksum_pinned_current_chrome() -> None:
    dockerfile = (ROOT / "Dockerfile.patreon-browser").read_text(encoding="utf-8")
    publisher = (ROOT / "src" / "gen_automation" / "patreon_browser" / "publisher.py").read_text(
        encoding="utf-8"
    )
    bootstrap = (ROOT / "src" / "gen_automation" / "patreon_browser" / "bootstrap.py").read_text(
        encoding="utf-8"
    )

    assert (
        "ADD --checksum="
        "sha256:920efa0a56b47835a5dcdb3e90ae75b9723b63b91d6f4aa086617b9f4d212d7e" in dockerfile
    )
    assert "chrome-for-testing-public/151.0.7922.109/linux64/chrome-linux64.zip" in dockerfile
    assert "playwright install-deps chromium" in dockerfile
    assert "playwright install --with-deps chromium" not in dockerfile
    assert publisher.count("channel=_BROWSER_CHANNEL") == 1
    assert bootstrap.count("channel=_BROWSER_CHANNEL") == 1


def test_runbook_activates_only_after_local_validation_and_keeps_effects_off() -> None:
    runbook = (ROOT / "docs" / "aws-staging-runbook.md").read_text(encoding="utf-8")

    assert "infra/aws-staging/deploy" in runbook
    assert "repository@sha256:<64 lowercase hex>" in runbook
    assert "Docker Compose v5.1.2" in runbook
    assert "nginx" in runbook
    assert "IPv4 IMDS owner blocks" in runbook
    assert "/usr/local/libexec/gen-automation-validate-deployment" in runbook
    assert "systemctl enable --now gen-automation-staging.service" in runbook
    assert "Uvicorn listens only on `127.0.0.1:8000`" in runbook
    assert "Patreon publishes only `127.0.0.1:8090`" in runbook
    assert "no privileged container" in runbook
    assert "Docker-socket mount" in runbook
    assert (
        "Keep GPU allocation, Patreon publication, MEGA delivery, and X publication\n"
        "disabled until their individual canaries pass."
    ) in runbook
