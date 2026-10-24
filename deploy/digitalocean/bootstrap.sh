#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 22.04/24.04 droplet. Run as root:
#   ssh root@IP 'bash -s' < bootstrap.sh
# Installs Docker, opens only SSH/HTTP/HTTPS, turns on automatic security updates, adds swap on
# small machines, and installs the nightly database backup.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APP_DIR=/opt/callpulse

apt-get update -y
apt-get install -y ca-certificates curl gnupg ufw fail2ban unattended-upgrades rsync cron

if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

# Firewall: nothing but SSH and web traffic. Docker publishes only Caddy's 80/443.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

systemctl enable --now fail2ban
dpkg-reconfigure -f noninteractive unattended-upgrades || true

# Swap, so a build or a memory spike does not get the kernel killing the database.
if ! swapon --show | grep -q . ; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

mkdir -p "$APP_DIR/app" "$APP_DIR/frontend" /var/backups/callpulse
# Nightly backup at 02:30 (kept 14 days; see backup.sh).
cat > /etc/cron.d/callpulse-backup <<CRON
30 2 * * * root $APP_DIR/app/deploy/digitalocean/backup.sh >> /var/log/callpulse-backup.log 2>&1
CRON
chmod 644 /etc/cron.d/callpulse-backup
echo "Bootstrap done. Next: copy .env to $APP_DIR/app/deploy/digitalocean/.env and run deploy.sh from your computer."
