# arr-stack

Stack doméstica de mídia em Docker: **qBittorrent atrás de VPN com killswitch** e automação com
Radarr, Sonarr, Prowlarr e Bazarr, mais **Jellyfin** para assistir e **Seerr** para pedir filmes e séries.

Este repositório tem tudo o que é preciso para **reconstruir o servidor do zero**. Segredos
(chave da VPN, senhas, chaves de API, bancos dos apps) **não** ficam aqui: veja [Backup](#backup).

## Arquitetura

```
                Internet
                   │
   ┌───────────────┴───────────────────────────────────────────┐
   │ gluetun  (ProtonVPN WireGuard + port forwarding)           │
   │   └─ qbittorrent  network_mode: service:gluetun            │
   │        sem VPN = sem rede (killswitch real)                │
   └───────────────┬───────────────────────────────────────────┘
                   │ rede docker "arrnet"
   prowlarr ── sonarr ── radarr ── bazarr ── jellyfin ── seerr
```

| Serviço | Função | Acesso |
|---|---|---|
| qBittorrent | download (só pela VPN) | `127.0.0.1:8080`, via túnel SSH |
| Prowlarr | indexadores, sincroniza com Sonarr/Radarr | `127.0.0.1:9696`, via túnel SSH |
| Sonarr | séries | `127.0.0.1:8989`, via túnel SSH |
| Radarr | filmes | `127.0.0.1:7878`, via túnel SSH |
| Bazarr | legendas | `127.0.0.1:6767`, via túnel SSH |
| Jellyfin | streaming | `http://IP_DO_SERVIDOR:8096` (rede local) |
| Seerr | pedidos | `http://IP_DO_SERVIDOR:5055` (rede local) |

Fluxo: pedido no **Seerr**, que repassa ao **Radarr/Sonarr**. Eles buscam nos indexadores do
**Prowlarr** e mandam o torrent ao **qBittorrent** (pela VPN), que baixa em
`downloads/<categoria>`. O Radarr/Sonarr importa o arquivo com hardlink para `movies/` ou `shows/`
e avisa o **Jellyfin**; o **Bazarr** baixa as legendas.

Todos os containers montam a mesma pasta de mídia no mesmo caminho (`/data`). Assim os caminhos
batem entre os apps e os hardlinks funcionam: o arquivo não é copiado e o seed continua.

```
$MEDIA_ROOT/
├── downloads/{movies,tv}   área do qBittorrent
├── movies/                 biblioteca de filmes
└── shows/                  biblioteca de séries
```

## Requisitos

- Debian 13 (trixie), usuário com sudo e acesso SSH por chave.
- Assinatura **ProtonVPN** paga (necessária para port forwarding). Outros provedores funcionam
  ajustando o `.env` conforme a [wiki do Gluetun](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers).
- IP fixo para o servidor (reserva de DHCP no roteador).
- Opcional: GPU Intel para transcodificação. O compose usa `/dev/dri/renderD128`; remova o
  bloco `devices` do Jellyfin se não houver.

## Reconstrução do zero

### 1. Clonar e preparar o `.env`

```bash
git clone https://github.com/jp-seplag/arr-stack.git ~/arr-stack
cd ~/arr-stack
cp .env.example .env && chmod 600 .env
nano .env    # PUID/PGID (id -u / id -g), MEDIA_ROOT e a chave WireGuard
```

**Chave WireGuard da Proton:** em account.protonvpn.com, vá em *Downloads → WireGuard
configuration*. Escolha *Platform: GNU/Linux*, ligue **NAT-PMP (Port Forwarding)**, pegue um
servidor **P2P** e clique em *Create*. Copie `PrivateKey` para `WIREGUARD_PRIVATE_KEY`. A chave
funciona em qualquer servidor; o país sai de `SERVER_COUNTRIES`.

### 2. Preparar a máquina

```bash
sudo LAN_CIDR=192.168.0.0/24 ./scripts/host-setup.sh
```

O script desliga a suspensão por tampa ou ociosidade, configura o firewall (só SSH da rede
local), instala o Docker oficial, endurece o SSH, liga as atualizações automáticas de segurança e
cria as pastas de mídia. Revise `host/sshd-90-hardening.conf` antes de rodar. **Abra uma nova
sessão SSH antes de fechar a atual.**

### 2b. Disco de mídia USB (opcional, recomendado)

A mídia fica num HD USB montado em `/mnt/media1` (ext4, rótulo `media1`). Para preparar um
disco **vazio** (o `mkfs` apaga tudo):

```bash
lsblk -o NAME,SIZE,TRAN,FSTYPE,LABEL      # identifique o disco USB (ex.: /dev/sdc)
sudo mkfs.ext4 -L media1 -m 0 /dev/sdX1   # só se precisar formatar
sudo blkid -s UUID -o value /dev/sdX1
sudo LAN_CIDR=192.168.0.0/24 MEDIA_DISK_UUID=<uuid> ./scripts/host-setup.sh
```

O script põe o disco no `/etc/fstab` com `nofail` (o boot não trava sem ele) e espera até 90 s
por ele (HD USB leva ~20 s para aparecer no boot; com 15 s a montagem desistia), deixa o ponto de
montagem **imutável** (sem o disco, nada grava no SSD por engano) e faz o Docker **depender do
disco**: sem ele montado, a stack não sobe. No `.env`, use `MEDIA_ROOT=/mnt/media1/media`.

Para desconectar o disco: `sudo docker compose stop && sudo umount /mnt/media1`.

Prefira a porta **USB 3.0** e um cabo USB 3.0 de verdade: com cabo comum o disco conecta a
480 Mbps. Dois HDs de 2,5" costumam precisar de um **hub USB com fonte**.

### 3. Subir a stack

```bash
sudo docker compose up -d
sudo docker compose logs -f gluetun       # espere "port forwarded is NNNNN"
sudo docker compose logs qbittorrent | grep -i password   # senha temporária do qBittorrent
```

### 4. Configurar os apps

```bash
QBIT_TEMP_PASSWORD=xxxxx LAN_IP=192.168.0.5 python3 scripts/configure-apps.py
```

O script gera uma senha única (usuário `admin`) em `credenciais.txt` e configura tudo pelas
APIs; os detalhes estão no cabeçalho do script. Ele é idempotente e pode ser rodado de novo.

Ainda é preciso fazer na interface:
- **Prowlarr**: adicionar os indexadores.
- **Bazarr**: idiomas (*Settings → Languages*) e provedores de legenda.
- **Jellyfin**: criar um usuário comum para assistir na TV.

### 5. Verificar

```bash
sudo ./scripts/verify.sh --killswitch
```

O script confere o IP de saída da VPN, a porta encaminhada, as portas restritas ao loopback, o
firewall e o killswitch.

## Uso diário

Abra o túnel no seu PC para acessar as interfaces de administração:

```bash
ssh -N -L 8080:127.0.0.1:8080 -L 9696:127.0.0.1:9696 -L 8989:127.0.0.1:8989 -L 7878:127.0.0.1:7878 -L 6767:127.0.0.1:6767 usuario@IP_DO_SERVIDOR
```

Depois use `http://localhost:8080`, `:9696`, `:8989`, `:7878` e `:6767`.

| Ação | Comando (na pasta da stack) |
|---|---|
| Estado | `sudo docker compose ps` |
| Logs | `sudo docker compose logs -f <serviço>` |
| Atualizar | `sudo docker compose pull && sudo docker compose up -d` e depois `sudo ./scripts/verify.sh` |
| Parar (mantém os dados) | `sudo docker compose down` |

## Armadilhas conhecidas (aprendidas na prática)

- **Reiniciar o Gluetun derruba a rede do qBittorrent.** Recrie-o depois:
  `sudo docker compose up -d --force-recreate qbittorrent`.
- **A porta encaminhada da Proton muda a cada reconexão.** O `VPN_PORT_FORWARDING_UP_COMMAND` no
  compose atualiza o qBittorrent sozinho. Isso depende do *bypass de autenticação em localhost*
  do qBittorrent, que o `configure-apps.py` liga.
- **Sonarr/Radarr acessam o qBittorrent pelo host `gluetun`**, não `qbittorrent`.
- **O Docker ignora o ufw nas portas publicadas.** Por isso as interfaces de administração ficam
  em `127.0.0.1`. Jellyfin (8096) e Seerr (5055) ficam abertos na rede local: **nunca** crie
  redirecionamento dessas portas no roteador.
- **Bibliotecas do Jellyfin criadas pela API vêm sem fontes de metadados** (sem capa, sem
  sinopse) e sem monitoramento em tempo real. O `configure-apps.py` corrige isso.
- **O qBittorrent ignora a pasta da categoria no modo manual.** O gerenciamento automático
  (*Automatic Torrent Management*) precisa estar ligado.
- **O Radarr avisa que `/data/downloads/movies` não existe** até o primeiro download na categoria.
  Basta criar a pasta.
- **O Jellyseerr foi descontinuado.** Use o Seerr (`ghcr.io/seerr-team/seerr`), que roda como
  usuário comum (uid 1000) e precisa de `init: true`.
- **Sem seed por padrão:** o qBittorrent para o torrent ao concluir (ratio 0) e o Radarr/Sonarr o
  remove, apagando a cópia em `downloads/`; a biblioteca não é afetada (hardlink). Em **trackers
  privados** isso causa banimento: aumente `max_ratio`/`max_seeding_time` no qBittorrent.
- **Os servidores "Brasil" da Proton ficam fisicamente em Miami.** O IP aparece como Flórida.
- **Um disco USB que “acende mas não aparece”** geralmente está com cabo só de carga ou com mau
  contato; acompanhe com `sudo dmesg -wT` ao conectar.
- **Intel Haswell (4ª geração) só tem VA-API.** Ela não decodifica HEVC por hardware, então dê
  preferência a H.264 1080p ou a aparelhos que reproduzam HEVC direto.

## Backup

O repositório não guarda estado. Para não reconfigurar tudo do zero, guarde em local
**privado**, fora do GitHub:

```bash
cd ~ && sudo tar czf arr-backup-$(date +%F).tgz --exclude='arr-stack/jellyfin/cache' \
  arr-stack/.env arr-stack/credenciais.txt \
  arr-stack/{gluetun,qbittorrent,prowlarr,sonarr,radarr,bazarr,jellyfin,seerr}
```

Para restaurar, extraia o arquivo sobre um clone do repositório e rode `sudo docker compose up -d`.
