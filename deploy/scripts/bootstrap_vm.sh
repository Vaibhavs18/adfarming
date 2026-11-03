#!/usr/bin/env bash
# Run ONCE on a fresh Ubuntu 22.04/24.04 VM, as root. Safe to re-run (idempotent).
#   From the Mac:  ssh root@<IP> 'bash -s' < deploy/scripts/bootstrap_vm.sh
# Installs Docker + compose plugin, firewall (22/80/443), optional fail2ban, a non-root `deploy`
# user (uid 1000, matches the container user), and the /opt/adfarming layout. Touches no secrets.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || { echo "expected Ubuntu, found ${ID:-unknown}" >&2; exit 1; }

DEPLOY_USER="${DEPLOY_USER:-deploy}"
INSTALL_FAIL2BAN="${INSTALL_FAIL2BAN:-1}"
export DEBIAN_FRONTEND=noninteractive

echo "== packages"
apt-get update -y
apt-get install -y --no-install-recommends ca-certificates curl gnupg ufw rsync python3 openssl

echo "== docker"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  arch="$(dpkg --print-architecture)"
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "== firewall (22, 80, 443 only)"
ufw default deny incoming
ufw default allow outgoing
ufw limit 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

if [ "$INSTALL_FAIL2BAN" = "1" ]; then
  echo "== fail2ban (sshd jail)"
  apt-get install -y --no-install-recommends fail2ban
  systemctl enable --now fail2ban
fi

echo "== deploy user"
if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
  if getent passwd 1000 >/dev/null; then
    echo "uid 1000 is already taken by $(getent passwd 1000 | cut -d: -f1)." >&2
    echo "The container runs as uid 1000 and must own the data dirs. Re-run with DEPLOY_USER=<that user>." >&2
    exit 1
  fi
  useradd --create-home --uid 1000 --shell /bin/bash "$DEPLOY_USER"
fi
[ "$(id -u "$DEPLOY_USER")" -eq 1000 ] || { echo "$DEPLOY_USER must have uid 1000" >&2; exit 1; }
usermod -aG docker "$DEPLOY_USER"   # note: docker group is root-equivalent; keep this user key-only
home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$home/.ssh"
if [ ! -s "$home/.ssh/authorized_keys" ] && [ -s /root/.ssh/authorized_keys ]; then
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /root/.ssh/authorized_keys "$home/.ssh/authorized_keys"
fi

echo "== directories"
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/adfarming /opt/adfarming/src /opt/adfarming/data
install -d -m 755 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/adfarming/data/private /opt/adfarming/data/out /opt/adfarming/data/db
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /opt/adfarming/backups
if [ ! -e /opt/adfarming/.env ]; then
  install -m 600 -o "$DEPLOY_USER" -g "$DEPLOY_USER" /dev/null /opt/adfarming/.env
fi
chmod 600 /opt/adfarming/.env

echo "== ssh hardening (only if the deploy user can already log in with a key)"
if [ -s "$home/.ssh/authorized_keys" ]; then
  cat > /etc/ssh/sshd_config.d/50-adfarming.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
  sshd -t && systemctl reload ssh
else
  echo "no authorized_keys for $DEPLOY_USER; leaving sshd untouched" >&2
fi

echo
docker --version
docker compose version
ufw status | head -n 8
echo "bootstrap done. Next: put your secrets into /opt/adfarming/.env yourself (see deploy/README.md step 8)."
