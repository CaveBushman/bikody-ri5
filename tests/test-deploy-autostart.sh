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

grep -Fq '$ systemctl enable event-control-agent' <<<"$OUTPUT"
grep -Fq '$ systemctl set-default multi-user.target' <<<"$OUTPUT"
grep -Fq '$ systemctl enable event-control-kiosk@' <<<"$OUTPUT"
grep -Fq 'kiosk je zapnutý pro příští start; nynější plochu ukončí restart' <<<"$OUTPUT"
grep -Fq 'Agent i displej jsou nastavené jako výchozí služby.' <<<"$OUTPUT"
grep -Fq 'verze agenta: 1.6' <<<"$OUTPUT"

echo "OK: agent i kiosk jsou výchozí služby také při přechodu z desktopu"
