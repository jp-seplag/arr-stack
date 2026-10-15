#!/usr/bin/env bash
# Prepara um Debian 13 (trixie) recém-instalado para rodar a stack.
# Idempotente: pode ser rodado de novo sem estragar nada.
#
#   sudo LAN_CIDR=192.168.0.0/24 ./scripts/host-setup.sh
#   sudo LAN_CIDR=192.168.0.0/24 MEDIA_DISK_UUID=<uuid do blkid> ./scripts/host-setup.sh   (mídia em disco USB)
#
# Faz: desliga suspensão do notebook, firewall (ufw), Docker oficial,
# endurecimento do SSH, atualizações automáticas de segurança, log rotation do Docker.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Rode com sudo." >&2; exit 1; }
LAN_CIDR="${LAN_CIDR:-192.168.0.0/24}"
SSH_USER="${SUDO_USER:-$(logname)}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export DEBIAN_FRONTEND=noninteractive

echo "== 1/6 Energia: tampa fechada não suspende"
install -D -m 0644 "$HERE/host/logind-no-suspend.conf" /etc/systemd/logind.conf.d/no-suspend.conf
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
systemctl kill -s HUP systemd-logind

echo "== 2/6 Firewall: SSH liberado só para $LAN_CIDR ANTES de ativar"
apt-get update -qq
apt-get install -y -qq ufw ca-certificates curl unattended-upgrades apt-listchanges >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN_CIDR" to any port 22 proto tcp
ufw --force enable

echo "== 3/6 Docker (repositório oficial para Debian)"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
fi
install -D -m 0644 "$HERE/host/docker-daemon.json" /etc/docker/daemon.json
systemctl restart docker
# De propósito o usuário NÃO entra no grupo docker (equivale a root sem senha): use "sudo docker ...".

echo "== 4/6 SSH endurecido (usuário permitido: $SSH_USER)"
sed "s/^AllowUsers .*/AllowUsers $SSH_USER/" "$HERE/host/sshd-90-hardening.conf" > /etc/ssh/sshd_config.d/90-hardening.conf
sshd -t
systemctl reload ssh

echo "== 5/6 Atualizações automáticas de segurança"
install -m 0644 "$HERE/host/apt-20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades

if [ -n "${MEDIA_DISK_UUID:-}" ]; then
  echo "== Disco de mídia USB ($MEDIA_DISK_UUID -> /mnt/media1)"
  mkdir -p /mnt/media1
  grep -q "$MEDIA_DISK_UUID" /etc/fstab || \
    echo "UUID=$MEDIA_DISK_UUID  /mnt/media1  ext4  defaults,noatime,nofail,x-systemd.device-timeout=90s  0  2" >> /etc/fstab
  mountpoint -q /mnt/media1 || chattr +i /mnt/media1   # sem o disco, ninguém grava no SSD por engano
  systemctl daemon-reload
  mountpoint -q /mnt/media1 || mount /mnt/media1
  install -D -m 0644 "$HERE/host/docker-media-disk.conf" /etc/systemd/system/docker.service.d/media-disk.conf
  systemctl daemon-reload
fi

echo "== 6/6 Pastas de mídia"
MEDIA_ROOT="$(grep -oP '^MEDIA_ROOT=\K[^#[:space:]]+' "$HERE/.env" 2>/dev/null || echo "/home/$SSH_USER/media")"
install -d -o "$SSH_USER" -g "$SSH_USER" "$MEDIA_ROOT"/{downloads/movies,downloads/tv,movies,shows}

echo
echo "Pronto. ABRA UMA NOVA SESSÃO SSH antes de fechar esta, para confirmar o acesso."
