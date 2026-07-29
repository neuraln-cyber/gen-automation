#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "IMDS egress rules must be installed as root." >&2
  exit 1
}

iptables_binary="$(command -v iptables || true)"
[ -n "$iptables_binary" ] || {
  printf '%s\n' "iptables is required for host-network IMDS isolation." >&2
  exit 1
}

for blocked_uid in 10002 10003; do
  if ! "$iptables_binary" --wait 5 --check OUTPUT \
    --destination 169.254.169.254/32 \
    --match owner \
    --uid-owner "$blocked_uid" \
    --jump REJECT 2>/dev/null; then
    "$iptables_binary" --wait 5 --insert OUTPUT 1 \
      --destination 169.254.169.254/32 \
      --match owner \
      --uid-owner "$blocked_uid" \
      --jump REJECT
  fi
done

printf '%s\n' "IMDS egress is blocked for the Caddy and nginx host UIDs."
