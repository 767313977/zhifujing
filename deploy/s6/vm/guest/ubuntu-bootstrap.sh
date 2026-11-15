#!/usr/bin/env bash
# S6.8 guest bootstrap (run inside the Ubuntu 24.04 VM as root, after OS install).
#
# - configures static IP 172.31.240.10/24 via the dedicated internal vSwitch;
# - installs acceptance dependencies (nftables/curl/python3/tcpdump/gcc);
# - creates the unprivileged user fupan (used by verify-egress.sh runuser);
# - prepares /opt/s6 for the deploy/s6 scripts and evidence.
#
# It does not touch the host, does not enable external data collection and does
# not apply any firewall policy (that is run-egress-acceptance.sh).
set -euo pipefail

if [[ "$(id -u)" != "0" ]]; then
  echo "must run as root" >&2
  exit 2
fi
if grep -qi microsoft /proc/version 2>/dev/null; then
  echo "refusing to run in WSL" >&2
  exit 3
fi

IFACE="${IFACE:-$(ip -o -4 route show to default | awk '{print $5; exit}')}"
if [[ -z "${IFACE}" ]]; then
  IFACE="$(ip -o link show | awk -F': ' '/state UP/ && $2 != "lo" {print $2; exit}')"
fi
if [[ -z "${IFACE}" ]]; then
  echo "cannot detect the primary interface" >&2
  exit 4
fi

echo "configuring ${IFACE}: 172.31.240.10/24 gw 172.31.240.1"
cat > /etc/netplan/99-s6-static.yaml <<EOF
network:
  version: 2
  renderer: networkd
  ethernets:
    ${IFACE}:
      dhcp4: false
      dhcp6: false
      addresses: [172.31.240.10/24]
      routes:
        - to: default
          via: 172.31.240.1
      nameservers:
        addresses: [8.8.8.8, 1.1.1.1]
EOF
chmod 600 /etc/netplan/99-s6-static.yaml
netplan apply
sleep 3

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  nftables curl ca-certificates python3 tcpdump dnsutils iproute2 gcc libc6-dev openssh-server

if ! id fupan >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash fupan
fi

mkdir -p /opt/s6/deploy /opt/s6/evidence
chown -R fupan:fupan /opt/s6/evidence

if [[ "${1:-}" == "--ssh-pubkey-file" && -n "${2:-}" ]]; then
  install -d -m 700 -o fupan -g fupan /home/fupan/.ssh
  install -m 600 -o fupan -g fupan "$2" /home/fupan/.ssh/authorized_keys
  echo "installed SSH public key for fupan"
fi

echo "nft: $(nft --version 2>/dev/null || echo missing)"
echo "python3: $(python3 --version 2>&1)"
echo "ip: $(ip -4 -o addr show dev "${IFACE}" | awk '{print $4}')"
echo "bootstrap complete; next: run deploy/s6/vm/invoke-guest-egress-acceptance.ps1"
