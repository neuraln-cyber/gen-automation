# AWS staging container bundle

This directory is a credential-free deployment bundle for the single staging
EC2 host. It does not build images, create secrets, authenticate MEGA or
Patreon, run database migrations, or enable external effects.

The four image references are supplied through `/etc/gen-automation/deploy.env`
and must be immutable `repository@sha256:<64 lowercase hex>` references:

- the control-plane image containing the pinned official MEGAcmd build;
- the Patreon browser sidecar image;
- a reviewed official nginx image; and
- a reviewed official Caddy image.

Service startup pulls those exact digests and validates the committed Caddy and
nginx configurations inside the same images before any container starts. Caddy
terminates TLS and replaces client-supplied forwarding headers. The loopback
nginx ingress guard enforces per-client global/authentication rate limits and
connection limits, an 8 MiB request-body ceiling (covering the supported 4 MiB
watermark upload plus multipart overhead), bounded header/body/proxy
timeouts, and a 32 KiB large-header ceiling.

The control plane, nginx, and Caddy use host networking. Uvicorn binds only
`127.0.0.1:8000`; nginx binds only `127.0.0.1:8080`; Caddy is the only process
binding public ports 80/443. Caddy and nginx run as dedicated host UIDs 10002
and 10003. A required persistent systemd oneshot installs and verifies OUTPUT
owner rules rejecting their traffic to IPv4 IMDS. The Patreon sidecar stays on
a Docker bridge and publishes port 8090 only on host loopback. With EC2 IMDSv2
response hop limit 1, the bridged sidecar cannot obtain the instance role,
while the host-networked control plane UID 10001 can use ambient AWS
credentials.

No service is privileged, no service mounts the Docker socket, and the Patreon
sidecar receives no AWS credentials. The persistent MEGA profile and the
Patreon Chromium/idempotency paths are the encrypted EBS mount prepared by the
AWS module.

Install from an SSM session:

```shell
sudo ./install.sh
sudo cp /etc/gen-automation/examples/deploy.env.example /etc/gen-automation/deploy.env
sudo cp /etc/gen-automation/examples/control-plane.env.example /etc/gen-automation/control-plane.env
sudo cp /etc/gen-automation/examples/patreon-browser.env.example /etc/gen-automation/patreon-browser.env
sudo cp /etc/gen-automation/examples/caddy.env.example /etc/gen-automation/caddy.env
sudo chmod 0600 /etc/gen-automation/*.env
```

`install.sh` also installs Docker Compose v5.1.2 for all users from Docker's
official release asset and verifies its committed SHA-256 before installation.
It does not start the application. Re-running the installer re-verifies the
installed plugin and downloads it only when the reviewed binary is absent.
It also installs
`/usr/local/sbin/gen-automation-bootstrap-patreon-profile`. After configuring
the immutable images and environment files, use that one-time command with an
AWS SSM port-forward to `127.0.0.1:6080` to complete Patreon login in the
cloud-hosted browser. The executable sequence is in
`docs/patreon-browser-publisher.md`; no public VNC/noVNC ingress is required.

Populate the four files out-of-band. Keep external effects disabled through the
first health, storage, MEGA, Patreon, and X canaries. The validator reads only
the minimum non-secret deployment invariants and never sources an environment
file. The systemd unit validates inputs, renders Compose, pulls the four exact
digests, validates both proxy configurations offline, then starts the
health-gated service chain: Patreon, controller, nginx, and Caddy.

```shell
sudo /usr/local/libexec/gen-automation-validate-deployment
sudo docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml config --quiet
sudo systemctl enable --now gen-automation-staging.service
```

Inspect health and ordering with:

```shell
systemctl status gen-automation-staging.service
systemctl status gen-automation-imds-egress.service
docker compose \
  --env-file /etc/gen-automation/deploy.env \
  -f /opt/gen-automation/deploy/compose.yaml ps
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:8080/api/v1/health/ready
curl --fail http://127.0.0.1:8090/health/live
```

To deploy a reviewed digest update, replace only the affected immutable image
reference and restart the unit. Roll back by restoring the previous digest and
restarting again.
