#!/usr/bin/env bash
# Verificação de segurança da stack. Rode após qualquer mudança:
#   sudo ./scripts/verify.sh            (testes básicos)
#   sudo ./scripts/verify.sh --killswitch   (também derruba a VPN para testar o killswitch)
set -uo pipefail
cd "$(dirname "$0")/.."
[ "$(id -u)" -eq 0 ] || { echo "Rode com sudo." >&2; exit 1; }
fail=0
ok()  { echo "  OK    $*"; }
bad() { echo "  FALHA $*"; fail=1; }

echo "== IP de saída"
vpn_ip=$(docker exec qbittorrent curl -s --max-time 10 ifconfig.me || true)
real_ip=$(curl -s --max-time 10 ifconfig.me || true)
if [ -n "$vpn_ip" ] && [ "$vpn_ip" != "$real_ip" ]; then ok "qBittorrent sai por $vpn_ip (IP real: $real_ip)"; else bad "qBittorrent: '$vpn_ip' / real: '$real_ip'"; fi

echo "== Porta encaminhada sincronizada"
fp=$(docker exec gluetun cat /tmp/gluetun/forwarded_port 2>/dev/null || true)
qp=$(docker exec qbittorrent curl -s http://127.0.0.1:8080/api/v2/app/preferences | python3 -I -c 'import json,sys;print(json.load(sys.stdin)["listen_port"])' 2>/dev/null || true)
[ -n "$fp" ] && [ "$fp" = "$qp" ] && ok "porta $fp" || bad "gluetun=$fp qbittorrent=$qp"

echo "== Interfaces de administração só no loopback"
for p in 8080 9696 8989 7878 6767; do
  addrs=$(ss -tlnH "sport = :$p" | awk '{print $4}' | sort -u | tr '\n' ' ')
  case "$addrs" in "127.0.0.1:$p ") ok "$p -> $addrs";; *) bad "$p -> $addrs";; esac
done

echo "== Firewall"
ufw status | grep -q '^Status: active' && ok "ufw ativo" || bad "ufw inativo"

if [ "${1:-}" = "--killswitch" ]; then
  echo "== Killswitch (derrubando o gluetun)"
  docker compose stop gluetun >/dev/null 2>&1
  out=$(docker exec qbittorrent curl -s --max-time 5 ifconfig.me 2>&1); rc=$?
  if [ $rc -ne 0 ] && ! grep -qE '^[0-9.]+$' <<<"$out"; then ok "sem rede com a VPN parada"; else bad "qBittorrent respondeu '$out' sem VPN"; fi
  docker compose up -d gluetun >/dev/null 2>&1
  until [ "$(docker inspect -f '{{.State.Health.Status}}' gluetun)" = healthy ]; do sleep 3; done
  docker compose up -d --force-recreate qbittorrent >/dev/null 2>&1   # obrigatório: perde a rede quando o gluetun reinicia
  ok "gluetun e qbittorrent restaurados"
fi

echo; [ $fail -eq 0 ] && echo "Tudo certo." || { echo "Há falhas: não use a stack até corrigir."; exit 1; }
