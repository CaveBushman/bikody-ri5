#!/usr/bin/env bash
# Stáhne aktuální verzi agenta ze serveru, na který je krabička nastavená.
#
# Agent je hloupý drát: protokoly zná server, takže se tenhle soubor mění jen
# tehdy, když se mění samotný způsob spojení. Aktualizace aplikace ho nevyžaduje.
set -euo pipefail

# **Jména se nehádají, hledají.** Krabička v terénu může být ještě pod starými
# jmény (přejmenování na BIKODY se dělá po částech). Skript s natvrdo psaným
# `/opt/bikody-agent` na takové krabičce 18. 9. 2026 spadl na
# „cannot create regular file" a nešlo ji aktualizovat vůbec.
if [[ -d /opt/bikody-agent ]]; then
    INSTALL_DIR=/opt/bikody-agent
    SERVICE=bikody-agent
elif [[ -d /opt/event-control-agent ]]; then
    INSTALL_DIR=/opt/event-control-agent
    SERVICE=event-control-agent
else
    echo "Nenašel jsem instalaci agenta v /opt — je krabička vůbec nastavená?" >&2
    exit 1
fi

# **Config je o patro níž, než se dlouho myslelo.** Agent ho ukládá podle
# zvyklostí systému do `$HOME/.config/bikody-agent/`, a `HOME` je v jednotce
# nastavený na instalační adresář. Soubor `$INSTALL_DIR/config.json` psal
# `deploy.sh` a **nikdo ho nečetl** — proto tudy adresa serveru nikdy
# nedorazila a skript se jí marně doptával.
CONFIG="$INSTALL_DIR/.config/bikody-agent/config.json"
[[ -f "$CONFIG" ]] || CONFIG="$INSTALL_DIR/.config/event-control-agent/config.json"

if [[ $EUID -ne 0 ]]; then
    echo "Spusťte přes sudo: sudo $0" >&2
    exit 1
fi

SERVER="${1:-}"
if [[ -z "$SERVER" && -f "$CONFIG" ]]; then
    SERVER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("server",""))' "$CONFIG")"
fi
if [[ -z "$SERVER" ]]; then
    echo "Není známá adresa serveru. Zadejte ji: sudo $0 https://vas-server.cz" >&2
    exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

echo "▶ Stahuji agenta z $SERVER"
curl -fsSL "$SERVER/bmx/api/agent/download/" -o "$TMP"

# **Že se to dá přeložit, neznamená, že je to celé.** Stahování přerušené na
# hranici řádku nechá syntakticky platný Python, kterému chybí `main()` —
# spustí se, nemá co dělat a skončí **s nulou a bez jediného výpisu**. Systemd
# ho restartuje dokola a na displeji je jen „127.0.0.1 refused to connect".
# Přesně takhle 18. 9. 2026 zůstala krabička večer před závodem mrtvá a
# `ast.parse`, který tu do té doby stál sám, hlásil „syntaxe OK".
#
# Proto se neptáme, jestli to jde přeložit, ale jestli to **doběhlo**:
# spouštěcí blok je až na konci souboru.
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TMP"
if ! grep -q '__main__' "$TMP" || ! grep -q 'def main(' "$TMP"; then
    echo "Stažený agent není celý (chybí spouštěcí blok) — neinstaluji." >&2
    exit 1
fi

if cmp -s "$TMP" "$INSTALL_DIR/track_agent.py"; then
    echo "▶ Verze je stejná, není co měnit."
    exit 0
fi

# Záloha, než se sáhne na běžící krabičku: vrátit se je pak jedno kopírování
# místo shánění internetu u trati.
[[ -f "$INSTALL_DIR/track_agent.py" ]] && cp "$INSTALL_DIR/track_agent.py" "$INSTALL_DIR/track_agent.py.bak"
install -m 755 "$TMP" "$INSTALL_DIR/track_agent.py"
systemctl restart "$SERVICE"

# **Restart sám o sobě není důkaz.** Služba se restartuje i tehdy, když agent
# hned skončí; poznalo by se to až u trati. Pár vteřin počkat a zeptat se.
sleep 3
if systemctl is-active --quiet "$SERVICE"; then
    echo "▶ Hotovo, agent běží."
else
    echo "▶ Agent po restartu neběží — vracím předchozí verzi." >&2
    [[ -f "$INSTALL_DIR/track_agent.py.bak" ]] && install -m 755 "$INSTALL_DIR/track_agent.py.bak" "$INSTALL_DIR/track_agent.py"
    systemctl restart "$SERVICE"
    journalctl -u "$SERVICE" -n 20 --no-pager >&2
    exit 1
fi
