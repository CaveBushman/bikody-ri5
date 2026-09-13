#!/usr/bin/env bash
# Z čerstvého Raspberry Pi OS udělá krabičku u trati.
#
# Jeden příkaz, žádné otázky. Co udělá:
#
#   1. stáhne aktuální verzi z gitu (`git pull`) — displej i agent se mění
#      spolu se skriptem, takže deploy ze staré kopie nasadí staré věci,
#   2. doinstaluje, co chybí (cage, chromium, curl, avahi),
#   3. nastaví jméno stroje a časové pásmo,
#   4. nakopíruje agenta do /opt a zapne ho jako službu,
#   5. zapne displej (kiosk) jako službu — bez plochy a bez přihlašování,
#   6. zapne hardwarový watchdog a vypne zhasínání konzole,
#   7. volitelně nastaví pevnou IP,
#   8. na konci zkontroluje, že obojí běží, a napíše, co zbývá.
#
# Použití:
#
#   sudo bash deploy.sh
#   sudo bash deploy.sh --hostname krabicka-brno \
#                       --static-ip 192.168.9.10/24 --gateway 192.168.9.1
#   sudo bash deploy.sh --no-pull  # nesahat na git (offline, vlastní úpravy)
#   bash deploy.sh --dry-run       # jen vypíše, co by udělal
#
# Přes `bash`, ne `./deploy.sh`: kopírování na Pi umí souboru sebrat právo
# ke spuštění a `sudo ./deploy.sh` pak hlásí „command not found".
#
# Skript se dá pouštět opakovaně — je to nastavení, ne instalace. Token
# krabičky se **nikdy nepřepisuje**: obsluha ho má opsaný v aplikaci.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR=/opt/event-control-agent
AGENT_SERVICE=event-control-agent
KIOSK_SERVICE=event-control-kiosk
TIMEZONE_DEFAULT="Europe/Prague"

# Aplikace běží na jednom místě, takže se adresa nikde nevypisuje. `--server`
# zůstává kvůli zkouškám a vlastnímu serveru.
SERVER="https://bikody.com"
HOSTNAME_NEW=""
TIMEZONE="$TIMEZONE_DEFAULT"
STATIC_IP=""
GATEWAY=""
DNS="1.1.1.1"
WITH_KIOSK=1
WITH_PULL=1
DRY_RUN=0
KIOSK_REZIM="zadny"      # systemd | zadny — podle toho, zda se instaluje displej
KIOSK_CEKA_NA_REBOOT=0    # 1 při přechodu z běžící plochy na samostatný kiosk

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server)     SERVER="${2:-}"; shift 2 ;;
        --hostname)   HOSTNAME_NEW="${2:-}"; shift 2 ;;
        --timezone)   TIMEZONE="${2:-}"; shift 2 ;;
        --static-ip)  STATIC_IP="${2:-}"; shift 2 ;;
        --gateway)    GATEWAY="${2:-}"; shift 2 ;;
        --dns)        DNS="${2:-}"; shift 2 ;;
        --no-kiosk)   WITH_KIOSK=0; shift ;;
        --no-pull)    WITH_PULL=0; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    usage 0 ;;
        *) echo "Neznámý přepínač: $1" >&2; usage 1 ;;
    esac
done

# --- pomůcky ---------------------------------------------------------------

krok()  { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
varuj() { printf '  \033[33m! %s\033[0m\n' "$*"; }

# Každý zásah do systému jde přes `spust`, takže `--dry-run` je opravdu
# nanečisto — a zároveň je z výpisu vidět, co přesně se s Pi stalo.
spust() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m$ %s\033[0m\n' "$*"
        return 0
    fi
    "$@"
}

zapis() {
    local cesta="$1" obsah="$2"
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  \033[2m$ zapsat %s (%d B)\033[0m\n' "$cesta" "${#obsah}"
        return 0
    fi
    printf '%s' "$obsah" > "$cesta"
}

if [[ $DRY_RUN -eq 0 && $EUID -ne 0 ]]; then
    echo "Spusťte přes sudo: sudo $0 $*" >&2
    exit 1
fi

if [[ $DRY_RUN -eq 0 && ! -f /etc/rpi-issue && ! -f /boot/firmware/config.txt ]]; then
    varuj "Tohle nevypadá na Raspberry Pi OS — pokračuji, ale watchdog a"
    varuj "vypnutí zhasínání konzole se nejspíš nepovedou."
fi

# Uživatel, pod kterým poběží displej. Kiosk potřebuje mít kde mít svůj
# runtime adresář, takže běží jako konkrétní člověk, ne jako root.
DESKTOP_USER="${SUDO_USER:-${USER:-pi}}"
[[ "$DESKTOP_USER" == "root" ]] && DESKTOP_USER="pi"

echo "Event Control — nastavení krabičky u trati"
[[ $DRY_RUN -eq 1 ]] && echo "(nanečisto — nic se nemění)"

# --- 1. čerstvá kopie repozitáře -------------------------------------------
#
# Displej, agent i tenhle skript se mění spolu, takže deploy ze staré kopie
# nasadí staré věci — a na trati to nikdo nepozná (Davidovo zadání
# 20. 8. 2026: „do deploy krabičky u trati dodělej git pull").
#
# Tři opatrnosti:
#
#   * `safe.directory` — repozitář patří `pi`, ale skript běží pod sudo
#     a git by jinak odmítl „dubious ownership",
#   * `--ff-only` — na krabičce se nevětví; kdyby se strom rozešel, je lepší
#     to říct než vyrobit merge commit v terénu,
#   * **restart sebe sama** — bash čte skript po částech, takže přepsat
#     běžící `deploy.sh` uprostřed běhu je past. Když se něco přitáhlo,
#     skript se spustí znovu z nové verze (a jen jednou, hlídá to proměnná).

krok "Git"
if [[ $WITH_PULL -eq 0 ]]; then
    info "přeskočeno (--no-pull)"
elif [[ ! -d "$ROOT/.git" ]]; then
    info "tohle není git repozitář — není odkud táhnout"
elif ! command -v git >/dev/null; then
    varuj "git není nainstalovaný — pokračuji s tím, co je v adresáři"
elif [[ $DRY_RUN -eq 1 ]]; then
    printf '  \033[2m$ git -C %s pull --ff-only\033[0m\n' "$ROOT"
else
    GIT=(git -C "$ROOT" -c safe.directory="$ROOT")
    PRED="$("${GIT[@]}" rev-parse HEAD 2>/dev/null || echo "")"
    if [[ -n "$("${GIT[@]}" status --porcelain 2>/dev/null)" ]]; then
        varuj "v adresáři jsou místní změny — git pull přeskočen"
    elif "${GIT[@]}" pull --ff-only --quiet; then
        PO="$("${GIT[@]}" rev-parse HEAD 2>/dev/null || echo "")"
        if [[ -n "$PRED" && -n "$PO" && "$PRED" != "$PO" ]]; then
            info "stáhnuto: ${PRED:0:7} → ${PO:0:7}"
            if [[ "${EC_DEPLOY_RESTART:-0}" != "1" ]]; then
                info "skript se změnil — pokračuji jeho novou verzí"
                EC_DEPLOY_RESTART=1 exec bash "$ROOT/deploy.sh" "$@"
            fi
        else
            info "už bylo aktuální"
        fi
    else
        varuj "git pull se nepovedl (offline nebo rozejitý strom) — pokračuji"
    fi
fi

# --- 2. balíčky ------------------------------------------------------------

krok "Balíčky"
CHYBI=()
command -v curl >/dev/null || CHYBI+=(curl)
command -v python3 >/dev/null || CHYBI+=(python3)
if [[ $WITH_KIOSK -eq 1 ]]; then
    command -v chromium-browser >/dev/null || command -v chromium >/dev/null || CHYBI+=(chromium-browser)
    # Podle grafiky: s KMS (/dev/dri) kreslí `cage`. Malé SPI displeje
    # (MHS35 a spol.) mají jen framebuffer — tam Wayland nemá GPU a je
    # potřeba X server s ovladačem fbdev.
    if [[ -d /dev/dri ]]; then
        command -v cage >/dev/null || CHYBI+=(cage)
    elif [[ -e /dev/fb0 ]]; then
        command -v xinit >/dev/null || CHYBI+=(xinit xserver-xorg xserver-xorg-video-fbdev xserver-xorg-legacy)
    else
        command -v cage >/dev/null || CHYBI+=(cage)
    fi
fi
command -v avahi-daemon >/dev/null || CHYBI+=(avahi-daemon)

if [[ ${#CHYBI[@]} -gt 0 ]]; then
    info "doinstaluji: ${CHYBI[*]}"
    spust apt-get update -qq
    spust apt-get install -y -qq "${CHYBI[@]}"
else
    info "všechno je nainstalované"
fi

# --- 3. jméno stroje a čas -------------------------------------------------

krok "Jméno stroje a časové pásmo"
if [[ -n "$HOSTNAME_NEW" ]]; then
    spust hostnamectl set-hostname "$HOSTNAME_NEW"
    info "jméno: $HOSTNAME_NEW (dostupná jako $HOSTNAME_NEW.local)"
else
    info "jméno ponecháno: $(hostname)"
fi
spust timedatectl set-timezone "$TIMEZONE"
info "časové pásmo: $TIMEZONE"

# --- 3. ovladač displeje ----------------------------------------------------
#
# 3,5" SPI displej (MHS35) potřebuje overlay v /boot a dva řádky v config.txt.
# Bez nich po zapnutí svítí bíle a systém o něm neví. Krabička s HDMI nebo
# oficiálním DSI displejem tenhle krok přeskočí — pozná se to podle toho, že
# překryv není potřeba: overlay se instaluje, jen když v configu žádný
# displej nastavený není.

REBOOT_KVULI_DISPLEJI=0
if [[ $WITH_KIOSK -eq 1 && -f /boot/firmware/config.txt ]]; then
    krok "Ovladač displeje"
    if grep -q "^dtoverlay=mhs35" /boot/firmware/config.txt; then
        info "MHS35 už je v config.txt"
    elif [[ -d /dev/dri || -e /dev/fb0 ]]; then
        info "displej už funguje — ovladač neměním"
    else
        spust install -m 755 "$ROOT/display/mhs35.dtbo" /boot/firmware/overlays/mhs35.dtbo
        grep -q "^dtparam=spi=on" /boot/firmware/config.txt \
            || spust bash -c 'echo "dtparam=spi=on" >> /boot/firmware/config.txt'
        spust bash -c 'echo "dtoverlay=mhs35:rotate=90" >> /boot/firmware/config.txt'
        REBOOT_KVULI_DISPLEJI=1
        info "MHS35 nainstalován (projeví se po restartu)"
    fi
fi

# --- 4. agent ---------------------------------------------------------------
#
# Zdroj agenta má pořadí: **server → kopie v repu**. Server vydává přesně tu
# verzi, se kterou aplikace počítá (`/bmx/api/agent/download/` servíruje
# `tools/track_agent.py` z nasazeného kódu), zatímco kopie na SD kartě je
# stará jak poslední klonování. Bez sítě se jede z kopie — krabička se dá
# postavit i offline a aktualizuje se, jakmile na server dosáhne
# (`scripts/update.sh` nebo prostě druhé spuštění tohohle skriptu).

krok "Agent"
spust install -d -m 755 "$INSTALL_DIR"

AGENT_REPO="$ROOT/agent/track_agent.py"
AGENT_ZDROJ="$AGENT_REPO"
AGENT_REPO_VERZE="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$AGENT_REPO" | head -1)"
AGENT_TMP="$(mktemp)"
trap 'rm -f "$AGENT_TMP"' EXIT
if [[ -n "$SERVER" ]] && curl -fsSL --max-time 20 "$SERVER/bmx/api/agent/download/" -o "$AGENT_TMP" 2>/dev/null         && python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$AGENT_TMP" 2>/dev/null; then
    AGENT_SERVER_VERZE="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$AGENT_TMP" | head -1)"
    # Server je obvykle zdroj pravdy, ale při postupném nasazení může dočasně
    # nabízet staršího agenta než čerstvý instalační repozitář. Krabičku při
    # opakovaném deployi nesmíme downgradovat. Číselné porovnání zvládá i
    # víceciferné části (1.10 > 1.9), na rozdíl od prostého řazení řetězců.
    if python3 - "$AGENT_SERVER_VERZE" "$AGENT_REPO_VERZE" <<'PY'
import re, sys

def version_key(value):
    return tuple(int(part) for part in re.findall(r"\d+", value or ""))

server, repository = sys.argv[1:3]
raise SystemExit(0 if version_key(server) >= version_key(repository) else 1)
PY
    then
        AGENT_ZDROJ="$AGENT_TMP"
        info "agent stažen ze serveru $SERVER (verze ${AGENT_SERVER_VERZE:-neznámá})"
    else
        info "server nabízí staršího agenta ${AGENT_SERVER_VERZE:-neznámá}; používám přibalenou verzi ${AGENT_REPO_VERZE:-neznámá}"
    fi
else
    info "server nedostupný — agent z kopie v repozitáři"
fi
AGENT_VERZE="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$AGENT_ZDROJ" | head -1)"
info "verze agenta: ${AGENT_VERZE:-neznámá}"
spust install -m 755 "$AGENT_ZDROJ" "$INSTALL_DIR/track_agent.py"
spust install -m 644 "$ROOT/systemd/$AGENT_SERVICE.service" "/etc/systemd/system/$AGENT_SERVICE.service"

# Adresa aplikace se zapíše rovnou, takže krabička po zapnutí ukáže token
# a nemusí se na ní nic nastavovat. Token se nikdy nepřepisuje — obsluha ho
# má opsaný v aplikaci a tichá výměna by krabičku odpojila.
if [[ -n "$SERVER" ]]; then
    if [[ -f "$INSTALL_DIR/config.json" ]]; then
        info "adresa aplikace: $SERVER (token zůstává)"
        spust python3 - "$INSTALL_DIR/config.json" "$SERVER" <<'PY'
import json, sys
cesta, server = sys.argv[1], sys.argv[2]
data = json.load(open(cesta))
data["server"] = server
json.dump(data, open(cesta, "w"), indent=2)
PY
    else
        info "adresa aplikace: $SERVER (token si krabička vyrobí sama)"
        zapis "$INSTALL_DIR/config.json" "$(printf '{\n  "server": "%s",\n  "token": "",\n  "autostart": false\n}\n' "$SERVER")"
        spust chmod 600 "$INSTALL_DIR/config.json"
    fi
fi

spust systemctl daemon-reload
spust systemctl enable "$AGENT_SERVICE"
# Restart, ne `enable --now`: běžící službu by `--now` nechal být a nová
# verze agenta by se načetla až po ručním restartu — přitom právě proto se
# skript pouští podruhé.
spust systemctl restart "$AGENT_SERVICE"
info "služba $AGENT_SERVICE zapnuta"

# --- 5. displej (kiosk) -----------------------------------------------------

if [[ $WITH_KIOSK -eq 1 ]]; then
    krok "Displej (kiosk)"
    spust install -m 755 "$ROOT/kiosk/start-kiosk.sh" "$INSTALL_DIR/start-kiosk.sh"
    spust install -m 755 "$ROOT/kiosk/kiosk-x-session.sh" "$INSTALL_DIR/kiosk-x-session.sh"

    if [[ ! -d /dev/dri && -e /dev/fb0 ]]; then
        info "SPI displej bez KMS — kiosk pojede přes X na /dev/fb0"
        spust install -d -m 755 /etc/X11/xorg.conf.d
        spust install -m 644 "$ROOT/kiosk/99-event-control-fbdev.conf" \
            /etc/X11/xorg.conf.d/99-event-control-fbdev.conf
        # X spouští služba, ne přihlášený člověk u konzole — bez tohohle by
        # wrapper start odmítl („Only console users are allowed").
        zapis /etc/X11/Xwrapper.config \
"allowed_users=anybody
needs_root_rights=yes
"
    fi

    # Krabička je jednoúčelová: výchozí cesta je vždy systémová služba bez
    # plochy a bez přihlášení. Dřívější větev pro graphical.target ukládala
    # jen desktopový autostart; bez autologinu pak po zapnutí zůstal displej
    # na přihlašovací obrazovce a agent vypadal jako něco, co se musí spustit
    # ručně. multi-user.target pustí agenta i kiosk rovnou po zapnutí zdroje.
    KIOSK_REZIM="systemd"
    PUVODNI_TARGET="$(systemctl get-default 2>/dev/null || echo multi-user.target)"
    if [[ "$PUVODNI_TARGET" == "graphical.target" ]]; then
        spust systemctl set-default multi-user.target
        KIOSK_CEKA_NA_REBOOT=1
        info "výchozí start přepnut na multi-user.target — bez přihlašování"
    fi

    # Starý desktopový autostart by po případném ručním spuštění plochy otevřel
    # druhý Chromium nad systémovým kioskem. Odstraňuje se jen náš vlastní
    # přesně pojmenovaný soubor; ostatního nastavení uživatele se nedotýkáme.
    DESKTOP_HOME="$(getent passwd "$DESKTOP_USER" 2>/dev/null | cut -d: -f6 || true)"
    DESKTOP_HOME="${DESKTOP_HOME:-/home/$DESKTOP_USER}"
    spust rm -f "$DESKTOP_HOME/.config/autostart/event-control-kiosk.desktop"

    spust install -m 644 "$ROOT/systemd/$KIOSK_SERVICE.service" \
        "/etc/systemd/system/$KIOSK_SERVICE@.service"
    spust systemctl daemon-reload
    spust systemctl enable "$KIOSK_SERVICE@$DESKTOP_USER"
    # Po smyčce restartů zůstane služba „failed" a systemd ji odmítne
    # spustit, dokud se počítadlo nesmaže.
    spust systemctl reset-failed "$KIOSK_SERVICE@$DESKTOP_USER" 2>/dev/null || true
    if [[ $KIOSK_CEKA_NA_REBOOT -eq 1 ]]; then
        info "kiosk je zapnutý pro příští start; nynější plochu ukončí restart"
    else
        spust systemctl restart "$KIOSK_SERVICE@$DESKTOP_USER"
    fi
    spust loginctl enable-linger "$DESKTOP_USER"
    info "poběží pod uživatelem $DESKTOP_USER, bez plochy a bez přihlašování"
else
    krok "Displej přeskočen (--no-kiosk)"
fi

# --- 6. krabička, která se o sebe stará ------------------------------------

krok "Odolnost"
if [[ ! -f /boot/firmware/cmdline.txt ]]; then
    varuj "/boot/firmware/cmdline.txt neexistuje — zhasínání konzole neřeším"
elif grep -q "consoleblank=0" /boot/firmware/cmdline.txt; then
    info "zhasínání konzole už je vypnuté"
else
    spust sed -i '1 s/$/ consoleblank=0/' /boot/firmware/cmdline.txt
    info "vypnuto zhasínání konzole (po restartu)"
fi

if [[ ! -f /boot/firmware/config.txt ]]; then
    varuj "/boot/firmware/config.txt neexistuje — watchdog neřeším"
elif grep -q "^dtparam=watchdog=on" /boot/firmware/config.txt; then
    info "watchdog už je zapnutý"
else
    spust bash -c 'echo "dtparam=watchdog=on" >> /boot/firmware/config.txt'
    info "zapnut hardwarový watchdog (po restartu)"
fi

spust install -d -m 755 /etc/systemd/system.conf.d
zapis /etc/systemd/system.conf.d/event-control-watchdog.conf \
"# Krabička u trati běží bez obsluhy: když zamrzne, restartuje se sama.
[Manager]
RuntimeWatchdogSec=30
RebootWatchdogSec=2min
"

# Log na SD kartě neroste donekonečna — karta se u trati vytahuje ze zásuvky
# a plný disk by krabičku položil dřív než cokoli jiného.
# Raspberry Pi Connect na krabičce nemá co dělat — a bez přihlášení se točí
# v havarijní smyčce („Sign in failed") stovkykrát za minutu: journald mele
# naprázdno, CPU topí a ventilátor jede naplno, i když „nic neběží".
if command -v rpi-connect >/dev/null; then
    info "vypínám Raspberry Pi Connect (krabička ho nepoužívá)"
    spust runuser -u "$DESKTOP_USER" -- env XDG_RUNTIME_DIR="/run/user/$(id -u "$DESKTOP_USER")" \
        rpi-connect off || true
fi

spust install -d -m 755 /etc/systemd/journald.conf.d
zapis /etc/systemd/journald.conf.d/event-control.conf \
"[Journal]
SystemMaxUse=50M
"
info "log omezen na 50 MB, watchdog na 30 s"

# --- 7. síť ----------------------------------------------------------------

if [[ -n "$STATIC_IP" ]]; then
    krok "Pevná adresa"
    if [[ -z "$GATEWAY" ]]; then
        echo "K --static-ip patří i --gateway." >&2
        exit 1
    fi
    # Přes `bash`, ne přímo: kopírování na Pi (scp, rozbalený archiv, klon
    # s vypnutým core.fileMode) umí sebrat právo ke spuštění a skript by
    # spadl na „Permission denied" až tady, uprostřed nastavování.
    spust bash "$ROOT/scripts/set-static-ip.sh" "$STATIC_IP" "$GATEWAY" "$DNS"
fi

# --- 8. kontrola -----------------------------------------------------------

krok "Kontrola"
SLUZBY=("$AGENT_SERVICE")
[[ "$KIOSK_REZIM" == "systemd" ]] && SLUZBY+=("$KIOSK_SERVICE@$DESKTOP_USER")

if [[ $DRY_RUN -eq 1 ]]; then
    info "nanečisto — služby se nespouštěly"
else
    for SLUZBA in "${SLUZBY[@]}"; do
        if ! systemctl is-enabled --quiet "$SLUZBA"; then
            varuj "$SLUZBA není zapnutá pro start systému"
        elif [[ $KIOSK_CEKA_NA_REBOOT -eq 1 && "$SLUZBA" == "$KIOSK_SERVICE@$DESKTOP_USER" ]]; then
            info "$SLUZBA je zapnutá a naběhne po restartu"
        elif systemctl is-active --quiet "$SLUZBA"; then
            info "$SLUZBA běží"
        else
            varuj "$SLUZBA neběží — podívejte se: journalctl -u $SLUZBA -n 30"
        fi
    done
fi

# `|| true`, protože `hostname -I` zná Linux, ale ne každý systém — a se
# zapnutým `pipefail` by na tom celý skript skončil těsně před tím, co má
# obsluha přečíst.
IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
NAZEV="$(hostname)"
NAZEV="${NAZEV%.local}"      # mDNS jméno se skládá níž, ať tam není dvakrát
cat <<INFO

Hotovo.

  Displej krabičky:  http://${IP:-<ip-krabicky>}:8088/   (nebo http://$NAZEV.local:8088/)
  Nastavení:         http://${IP:-<ip-krabicky>}:8088/nastaveni

Po zapnutí zdroje najede agent i displej samy, bez přihlašování. Hlásí se na
$SERVER, takže zbývá jediný krok:

  token z displeje opsat v aplikaci: Nastavení aplikace → Přihlásit krabičku.
INFO
echo
for SLUZBA in "${SLUZBY[@]}"; do
    echo "  stav:  systemctl status $SLUZBA"
done
cat <<INFO
  log:   journalctl -u $AGENT_SERVICE -f

Agent i displej jsou nastavené jako výchozí služby. Po každém zapnutí napájení
naběhnou samy. Watchdog, vypnuté zhasínání a případná změna režimu plochy se
projeví po restartu: sudo reboot
INFO
if [[ $REBOOT_KVULI_DISPLEJI -eq 1 ]]; then
    echo
    varuj "Nainstaloval se ovladač displeje — displej se rozsvítí až po: sudo reboot"
fi
