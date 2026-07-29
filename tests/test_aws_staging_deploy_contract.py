import re
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
        "GEN_AUTOMATION_NGINX_IMAGE",
        "GEN_AUTOMATION_CADDY_IMAGE",
    ):
        assert f'image: "${{{key}:?set a repository@sha256:64-hex digest}}"' in compose
        assert re.search(rf"(?m)^{key}=$", deploy_example)
        assert key in validator
    assert "@sha256:[0-9a-f]{64}" in validator
    assert "build:" not in compose
    assert ":latest" not in compose


def test_imds_network_boundary_and_loopback_ingress_are_explicit() -> None:
    compose = _text("compose.yaml")
    caddyfile = _text("Caddyfile")
    nginx = _text("nginx.conf")
    controller = _service(compose, "control-plane-mega", "ingress-guard")
    ingress = _service(compose, "ingress-guard", "caddy")
    patreon = _service(compose, "patreon-browser", "control-plane-mega")

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
    patreon = _service(compose, "patreon-browser", "control-plane-mega")
    caddy = _service(compose, "caddy", "patreon-egress")

    for service in (patreon, controller, ingress, caddy):
        assert "restart: unless-stopped" in service
        assert "healthcheck:" in service
        assert "cap_drop:\n      - ALL" in service
        assert "no-new-privileges:true" in service
        assert "read_only: true" in service
        assert "privileged:" not in service
        assert "/var/run/docker.sock" not in service
        assert "/run/docker.sock" not in service

    assert "patreon-browser:\n        condition: service_healthy" in controller
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


def test_environment_templates_contain_placeholders_not_secret_values() -> None:
    controller = _text("control-plane.env.example")
    patreon = _text("patreon-browser.env.example")
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
    assert re.search(r"(?m)^GEN_AUTOMATION_HOSTNAME=$", caddy)
    assert "GEN_AUTOMATION_STORAGE_ACCESS_KEY_ID=" not in controller
    assert "GEN_AUTOMATION_STORAGE_SECRET_ACCESS_KEY=" not in controller
    assert "GEN_AUTOMATION_STORAGE_SESSION_TOKEN=" not in controller
    assert "GEN_AUTOMATION_BACKGROUND_PUBLICATION_MAX_PACKAGE_BYTES=167772160" in controller
    assert "GEN_AUTOMATION_PATREON_BROWSER_MAX_PACKAGE_BYTES=167772160" in patreon


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
