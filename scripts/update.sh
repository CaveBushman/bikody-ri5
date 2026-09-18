#!/usr/bin/env bash
# Stáhne aktuální verzi agenta ze serveru, na který je krabička nastavená.
#
# Agent je hloupý drát: protokoly zná server, takže se tenhle soubor mění jen
# tehdy, když se mění samotný způsob spojení. Aktualizace aplikace ho nevyžaduje.
set -euo pipefail

INSTALL_DIR=/opt/bikody-agent
SERVICE=bikody-agent
CONFIG="$INSTALL_DIR/config.json"

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
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TMP"   # ať se nenainstaluje chybová stránka

if cmp -s "$TMP" "$INSTALL_DIR/track_agent.py"; then
    echo "▶ Verze je stejná, není co měnit."
    exit 0
fi

install -m 755 "$TMP" "$INSTALL_DIR/track_agent.py"
systemctl restart "$SERVICE"
echo "▶ Hotovo, agent restartován."
