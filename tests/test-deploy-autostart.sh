#!/usr/bin/env bash
# Regrese: i Raspberry Pi OS nainstalovaný s desktopem musí po příštím bootu
# spustit agenta a kiosk bez loginu. Test běží pouze nanečisto a všechny
# dotazy na systemd zachytí lokální maketa.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/bin"
cat > "$TMP/bin/systemctl" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "get-default" ]]; then
    echo graphical.target
fi
exit 0
SH
chmod +x "$TMP/bin/systemctl"

OUTPUT="$(PATH="$TMP/bin:$PATH" bash "$ROOT/deploy.sh" \
    --dry-run --no-pull --server "")"

grep -Fq '$ systemctl enable bikody-agent' <<<"$OUTPUT"
grep -Fq '$ systemctl set-default multi-user.target' <<<"$OUTPUT"
grep -Fq '$ systemctl enable bikody-kiosk@' <<<"$OUTPUT"
grep -Fq 'kiosk je zapnutý pro příští start; nynější plochu ukončí restart' <<<"$OUTPUT"
grep -Fq 'Agent i displej jsou nastavené jako výchozí služby.' <<<"$OUTPUT"
# Verze se **nepíše natvrdo**: test měl `1.6` a agent byl mezitím na 1.12,
# takže sada padala na řádku, který o autostartu nic neříká. Čte se z téhož
# souboru, ze kterého ji čte `deploy.sh`.
AGENT_VERZE="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$ROOT/agent/track_agent.py" | head -1)"
grep -Fq "verze agenta: $AGENT_VERZE" <<<"$OUTPUT"

echo "OK: agent i kiosk jsou výchozí služby také při přechodu z desktopu"
