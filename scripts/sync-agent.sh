#!/usr/bin/env bash
# Aktualizuje kopii agenta v tomhle projektu z hlavního repozitáře.
#
# Zdrojem pravdy je BIKODY Cloud (`tools/track_agent.py`) — tam se agent vyvíjí
# a tam je i pokrytý testy. Kopie tady je proto, aby se krabička dala postavit
# bez přístupu k němu; nesmí se ale rozejít.
set -euo pipefail

SOURCE="${1:-$HOME/Development/12 BIKODY SYSTEMS/BIKODY Cloud/tools/track_agent.py}"
TARGET="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/agent/track_agent.py"

if [[ ! -f "$SOURCE" ]]; then
    echo "Nenašel jsem zdroj: $SOURCE" >&2
    echo "Zadejte cestu: $0 /cesta/k/Event\\ Control/tools/track_agent.py" >&2
    exit 1
fi

if cmp -s "$SOURCE" "$TARGET"; then
    echo "Kopie je aktuální."
    exit 0
fi

cp "$SOURCE" "$TARGET"
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$TARGET"
echo "Zkopírováno: $SOURCE → agent/track_agent.py"
