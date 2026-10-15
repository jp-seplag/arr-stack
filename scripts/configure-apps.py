#!/usr/bin/env python3
"""Configura todos os apps da stack pelas APIs, depois do primeiro `docker compose up -d`.

Idempotente: o que já está configurado é mantido. Rode na pasta da stack, sem sudo:

    QBIT_TEMP_PASSWORD=xxxx python3 scripts/configure-apps.py

QBIT_TEMP_PASSWORD só é necessária na primeira vez; ela aparece em
`sudo docker compose logs qbittorrent`. A senha única de todos os apps (usuário
`admin`) é gerada na primeira execução e salva em credenciais.txt (modo 600).

O que configura:
  qBittorrent  senha, /data/downloads, categorias tv/movies, gerenciamento automático, sem seed,
               bypass de auth em localhost (usado pelo Gluetun para atualizar a porta)
  Sonarr/Radarr  login, pasta raiz, qBittorrent (host gluetun), aviso ao Jellyfin
  Prowlarr     login, sincronização com Sonarr/Radarr
  Bazarr       login, conexão com Sonarr/Radarr
  Jellyfin     assistente inicial, bibliotecas Filmes/Séries com metadados, VA-API, chave de API
  Seerr        login via Jellyfin, bibliotecas, Radarr/Sonarr, pt-BR
"""
import http.cookiejar
import json
import os
import re
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRED = os.path.join(ROOT, "credenciais.txt")
LAN_IP = os.environ.get("LAN_IP", "")  # opcional: endereço que o Seerr usa nos links para o Jellyfin
RENDER_DEVICE = os.environ.get("RENDER_DEVICE", "/dev/dri/renderD128")
QUALITY_PROFILE = os.environ.get("QUALITY_PROFILE", "HD-1080p")
USER = "admin"


def log(msg):
    print(f"  {msg}", flush=True)


def senha():
    if os.path.exists(CRED):
        return re.search(r"^Senha: (.+)$", open(CRED).read(), re.M).group(1)
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(20))
    old = os.umask(0o077)
    with open(CRED, "w") as fh:
        fh.write(f"Usuario (todos os apps): {USER}\nSenha: {pw}\n")
    os.umask(old)
    log("senha nova gerada em credenciais.txt")
    return pw


class Api:
    """Cliente HTTP mínimo com cookies, cabeçalhos fixos e corpo JSON ou formulário."""

    def __init__(self, base, headers=None):
        self.base, self.headers = base, headers or {}
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, method, path, body=None, form=None, ok=(200, 201, 202, 204)):
        hdr = dict(self.headers)
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdr["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            hdr["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdr)
        try:
            with self.opener.open(req, timeout=60) as r:
                txt = r.read()
        except urllib.error.HTTPError as e:
            if e.code in ok:
                return None
            sys.exit(f"ERRO {method} {self.base}{path} -> {e.code}: {e.read()[:400]!r}")
        try:
            return json.loads(txt) if txt else None
        except ValueError:
            return txt.decode()


def wait(url, tries=60):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=5)
            return
        except urllib.error.HTTPError:
            return  # respondeu (mesmo que 401/404): está no ar
        except Exception:
            time.sleep(3)
    sys.exit(f"ERRO: {url} não respondeu")


def arr_key(app):
    return re.search(r"<ApiKey>([^<]+)", open(os.path.join(ROOT, app, "config.xml")).read()).group(1)


def from_schema(api, kind, impl, name, values):
    s = next(x for x in api("GET", f"/{kind}/schema") if x["implementation"] == impl)
    s["name"], s["enable"] = name, True
    for f in s["fields"]:
        if f["name"] in values:
            f["value"] = values[f["name"]]
    return s


# ---------------------------------------------------------------- qBittorrent
def qbittorrent(pw):
    print("qBittorrent")
    wait("http://127.0.0.1:8080")
    q = Api("http://127.0.0.1:8080/api/v2", {"Referer": "http://127.0.0.1:8080"})

    def login(password):
        # qBittorrent 5.x: 204 = ok, 401 = senha errada (versões antigas: 200 "Ok." / 200 "Fails.")
        req = urllib.request.Request(q.base + "/auth/login", headers=q.headers, method="POST",
                                     data=urllib.parse.urlencode({"username": USER, "password": password}).encode())
        try:
            with q.opener.open(req, timeout=30) as r:
                return r.status == 204 or r.read() == b"Ok."
        except urllib.error.HTTPError:
            return False

    if not login(pw):
        tmp = os.environ.get("QBIT_TEMP_PASSWORD")
        if not tmp or not login(tmp):
            sys.exit("ERRO: login no qBittorrent falhou; defina QBIT_TEMP_PASSWORD (veja os logs do container)")
    prefs = {"web_ui_password": pw, "save_path": "/data/downloads", "temp_path_enabled": False,
             "bypass_local_auth": True, "upnp": False, "random_port": False,
             "auto_tmm_enabled": True, "torrent_changed_tmm_enabled": True,
             "save_path_changed_tmm_enabled": True, "category_changed_tmm_enabled": True,
             # sem seed: para ao concluir; o Radarr/Sonarr então remove o torrent e a cópia em downloads/
             # (a biblioteca fica intacta, é um hardlink). Não use em trackers privados, que exigem ratio.
             "max_ratio_enabled": True, "max_ratio": 0, "max_seeding_time_enabled": True,
             "max_seeding_time": 0, "max_ratio_act": 0}
    q("POST", "/app/setPreferences", form={"json": json.dumps(prefs)})
    cats = q("GET", "/torrents/categories") or {}
    for cat in ("tv", "movies"):
        if cat not in cats:
            q("POST", "/torrents/createCategory", form={"category": cat, "savePath": f"/data/downloads/{cat}"})
    log("senha, pastas, categorias tv/movies, gerenciamento automático, para ao concluir (sem seed)")


# ---------------------------------------------------------------- *arr
ARR = {"sonarr": ("http://127.0.0.1:8989/api/v3", "http://sonarr:8989"),
       "radarr": ("http://127.0.0.1:7878/api/v3", "http://radarr:7878"),
       "prowlarr": ("http://127.0.0.1:9696/api/v1", "http://prowlarr:9696")}


def arrs(pw, jellyfin_key):
    apis = {}
    for app, (base, _) in ARR.items():
        print(app.capitalize())
        wait(base.rsplit("/api", 1)[0])
        api = apis[app] = Api(base, {"X-Api-Key": arr_key(app)})
        host = api("GET", "/config/host")
        if host.get("authenticationMethod") != "forms" or host.get("username") != USER:
            host.update(authenticationMethod="forms", authenticationRequired="enabled",
                        username=USER, password=pw, passwordConfirmation=pw)
            api("PUT", f"/config/host/{host['id']}", host)
        log("login por formulário")

    for app, root, cat_field, cat in (("sonarr", "/data/shows", "tvCategory", "tv"),
                                      ("radarr", "/data/movies", "movieCategory", "movies")):
        api = apis[app]
        if not any(r["path"].rstrip("/") == root for r in api("GET", "/rootfolder")):
            api("POST", "/rootfolder", {"path": root})
        if not any(d["implementation"] == "QBittorrent" for d in api("GET", "/downloadclient")):
            api("POST", "/downloadclient", from_schema(api, "downloadclient", "QBittorrent", "qBittorrent", {
                "host": "gluetun", "port": 8080, "username": USER, "password": pw, cat_field: cat}))
        # avisa o Jellyfin a cada importação/renomeação/exclusão
        if not any(n["implementation"] == "MediaBrowser" for n in api("GET", "/notification")):
            n = from_schema(api, "notification", "MediaBrowser", "Jellyfin", {
                "host": "jellyfin", "port": 8096, "useSsl": False, "apiKey": jellyfin_key,
                "notify": False, "updateLibrary": True})
            want = {"onDownload", "onUpgrade", "onImportComplete", "onRename", "onMovieDelete", "onMovieFileDelete",
                    "onMovieFileDeleteForUpgrade", "onSeriesAdd", "onSeriesDelete", "onEpisodeFileDelete",
                    "onEpisodeFileDeleteForUpgrade"}
            for k in [k for k in n if k.startswith("on") and isinstance(n[k], bool)]:
                n[k] = k in want and n.get("supportsO" + k[1:], False)
            api("POST", "/notification", n)
        print(app.capitalize())
        log(f"pasta {root}, qBittorrent (host gluetun, categoria {cat}), aviso ao Jellyfin")

    existing = {a["implementation"] for a in apis["prowlarr"]("GET", "/applications")}
    for app, impl in (("sonarr", "Sonarr"), ("radarr", "Radarr")):
        if impl not in existing:
            a = from_schema(apis["prowlarr"], "applications", impl, impl, {
                "prowlarrUrl": ARR["prowlarr"][1], "baseUrl": ARR[app][1], "apiKey": arr_key(app)})
            a["syncLevel"] = "fullSync"
            apis["prowlarr"]("POST", "/applications", a)
    print("Prowlarr")
    log("sincronizando indexadores com Sonarr e Radarr")


# ---------------------------------------------------------------- Bazarr
def bazarr(pw):
    print("Bazarr")
    wait("http://127.0.0.1:6767")
    cfg = open(os.path.join(ROOT, "bazarr", "config", "config.yaml")).read()
    key = re.search(r"^auth:\n(?:  .*\n)*?  apikey: (\S+)", cfg, re.M).group(1)
    Api("http://127.0.0.1:6767/api", {"X-API-KEY": key})("POST", "/system/settings", form={
        "settings-general-use_sonarr": "true", "settings-general-use_radarr": "true",
        "settings-sonarr-ip": "sonarr", "settings-sonarr-port": "8989", "settings-sonarr-apikey": arr_key("sonarr"),
        "settings-radarr-ip": "radarr", "settings-radarr-port": "7878", "settings-radarr-apikey": arr_key("radarr"),
        "settings-auth-type": "form", "settings-auth-username": USER, "settings-auth-password": pw})
    log("login, Sonarr e Radarr conectados (idiomas/provedores de legenda: escolher na interface)")


# ---------------------------------------------------------------- Jellyfin
JF_AUTH = 'MediaBrowser Client="arr-stack", Device="configure-apps", DeviceId="arr-stack-setup", Version="1.0"'


def jellyfin(pw):
    print("Jellyfin")
    wait("http://127.0.0.1:8096/System/Info/Public")
    jf = Api("http://127.0.0.1:8096", {"Authorization": JF_AUTH})
    if not jf("GET", "/System/Info/Public")["StartupWizardCompleted"]:
        cfg = jf("GET", "/Startup/Configuration")
        cfg.update(UICulture="pt-BR", MetadataCountryCode="BR", PreferredMetadataLanguage="pt")
        jf("POST", "/Startup/Configuration", cfg)
        jf("GET", "/Startup/User")
        jf("POST", "/Startup/User", {"Name": USER, "Password": pw})
        jf("POST", "/Startup/RemoteAccess", {"EnableRemoteAccess": True, "EnableAutomaticPortMapping": False})
        jf("POST", "/Startup/Complete")
        log("assistente inicial (pt-BR, sem UPnP)")
    token = jf("POST", "/Users/AuthenticateByName", {"Username": USER, "Pw": pw})["AccessToken"]
    jf.headers["Authorization"] = JF_AUTH + f', Token="{token}"'

    # bibliotecas: criar e aplicar TODAS as fontes de metadados/imagens disponíveis.
    # (Criar só com {"LibraryOptions": {...}} deixa as fontes vazias: sem capa, sem sinopse.)
    for name, ctype, path in (("Filmes", "movies", "/data/movies"), ("Séries", "tvshows", "/data/shows")):
        if name not in {f["Name"] for f in jf("GET", "/Library/VirtualFolders")}:
            q = urllib.parse.urlencode({"name": name, "collectionType": ctype, "paths": path, "refreshLibrary": "false"})
            jf("POST", f"/Library/VirtualFolders?{q}", {"LibraryOptions": {}})
        f = next(f for f in jf("GET", "/Library/VirtualFolders") if f["Name"] == name)
        avail = jf("GET", f"/Libraries/AvailableOptions?LibraryContentType={ctype}&IsNewLibrary=true")
        o = f["LibraryOptions"]
        o.update(EnableRealtimeMonitor=True, EnableInternetProviders=True,
                 PreferredMetadataLanguage="pt", MetadataCountryCode="BR",
                 LocalMetadataReaderOrder=[x["Name"] for x in avail["MetadataReaders"]],
                 TypeOptions=[{"Type": t["Type"],
                               "MetadataFetchers": [x["Name"] for x in t["MetadataFetchers"]],
                               "MetadataFetcherOrder": [x["Name"] for x in t["MetadataFetchers"]],
                               "ImageFetchers": [x["Name"] for x in t["ImageFetchers"]],
                               "ImageFetcherOrder": [x["Name"] for x in t["ImageFetchers"]],
                               "ImageOptions": t.get("DefaultImageOptions", [])} for t in avail["TypeOptions"]])
        jf("POST", "/Library/VirtualFolders/LibraryOptions", {"Id": f["ItemId"], "LibraryOptions": o})
        log(f"biblioteca {name} -> {path} (TMDB, tempo real)")

    # transcodificação: Intel Haswell e anteriores só têm VA-API (QSV exige Broadwell+)
    enc = jf("GET", "/System/Configuration/encoding")
    enc.update(HardwareAccelerationType="vaapi", VaapiDevice=RENDER_DEVICE, EnableHardwareEncoding=True,
               HardwareDecodingCodecs=["h264", "mpeg2video", "vc1"], EnableDecodingColorDepth10Hevc=False,
               EnableDecodingColorDepth10Vp9=False, EnableTonemapping=False, AllowHevcEncoding=False)
    jf("POST", "/System/Configuration/encoding", enc)
    log(f"transcodificação VA-API em {RENDER_DEVICE}")

    keys = jf("GET", "/Auth/Keys")["Items"]
    if not any(k["AppName"] == "arr-stack" for k in keys):
        jf("POST", "/Auth/Keys?app=arr-stack")
        keys = jf("GET", "/Auth/Keys")["Items"]
    jf("POST", "/Library/Refresh")
    return next(k["AccessToken"] for k in keys if k["AppName"] == "arr-stack")


# ---------------------------------------------------------------- Seerr
def seerr(pw):
    print("Seerr")
    wait("http://127.0.0.1:5055/api/v1/status")
    s = Api("http://127.0.0.1:5055/api/v1")
    if s("GET", "/settings/public")["mediaServerType"] == 4:  # 4 = nenhum servidor configurado ainda
        s("POST", "/auth/jellyfin", {"username": USER, "password": pw, "hostname": "jellyfin", "port": 8096,
                                     "useSsl": False, "urlBase": "", "email": "admin@localhost", "serverType": 2})
    else:
        s("POST", "/auth/jellyfin", {"username": USER, "password": pw})
    s("POST", "/settings/jellyfin/library/sync")
    for lib in s("GET", "/settings/jellyfin/library"):
        s("PUT", f"/settings/jellyfin/library/{lib['id']}", {"enabled": True})
    if LAN_IP:
        s("POST", "/settings/jellyfin", {"externalHostname": f"http://{LAN_IP}:8096"})

    for app, port, root, extra in (("radarr", 7878, "/data/movies", {"minimumAvailability": "released"}),
                                   ("sonarr", 8989, "/data/shows", {"enableSeasonFolders": True,
                                                                    "activeAnimeDirectory": "/data/shows"})):
        if s("GET", f"/settings/{app}"):
            continue
        prof = next(p for p in Api(f"http://127.0.0.1:{port}/api/v3", {"X-Api-Key": arr_key(app)})(
            "GET", "/qualityprofile") if p["name"] == QUALITY_PROFILE)
        body = {"name": app.capitalize(), "hostname": app, "port": port, "apiKey": arr_key(app), "useSsl": False,
                "baseUrl": "", "activeProfileId": prof["id"], "activeProfileName": prof["name"],
                "activeDirectory": root, "is4k": False, "isDefault": True, "externalUrl": "",
                "syncEnabled": True, "preventSearch": False, **extra}
        if app == "sonarr":
            body.update(activeAnimeProfileId=prof["id"], activeAnimeProfileName=prof["name"])
        s("POST", f"/settings/{app}", body)

    s("POST", "/settings/main", {"locale": "pt-BR", "discoverRegion": "BR", "streamingRegion": "BR"})
    if not s("GET", "/settings/public")["initialized"]:
        s("POST", "/settings/initialize")
    log(f"Jellyfin, bibliotecas, Radarr/Sonarr ({QUALITY_PROFILE}), pt-BR")


if __name__ == "__main__":
    pw = senha()
    qbittorrent(pw)
    key = jellyfin(pw)
    arrs(pw, key)
    bazarr(pw)
    seerr(pw)
    print("\nConcluído. Falta na interface: indexadores no Prowlarr e idiomas/provedores no Bazarr.")
