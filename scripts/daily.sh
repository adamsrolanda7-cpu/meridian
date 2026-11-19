#!/usr/bin/env bash
# Séance quotidienne Meridian : données, paper, monitor.
# À planifier une fois par jour après la clôture UTC (00:00).
#
# Usage :
#   scripts/daily.sh                 # télécharge puis exécute
#   scripts/daily.sh --no-download   # réutilise les données déjà présentes
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p runs

DOWNLOAD=1
if [[ "${1:-}" == "--no-download" ]]; then
  DOWNLOAD=0
fi

# Un seul passage à la fois : deux téléchargements/paper concurrents
# pourraient se disputer l'état persistant.
exec 9>runs/daily.lock
if ! flock -n 9; then
  echo "une séance est déjà en cours"
  exit 0
fi

echo "=== $(date -u +%FT%TZ) séance quotidienne ==="

if [[ "$DOWNLOAD" == 1 ]]; then
  # --force garantit la récupération de la dernière bougie clôturée, même si
  # le cache local date de la veille.
  python3 -m meridian download --force
else
  echo "téléchargement ignoré (--no-download)"
fi

LATEST=$(
  python3 - <<'PY'
from meridian.config import load_config
from meridian.data import ensure_data

cfg = load_config()
panel, source = ensure_data(cfg, quiet=True, allow_synthetic=False)
if source == "synthetic":
    raise SystemExit("source synthétique refusée pour le paper")
print(max(df.index[-1] for df in panel.values()).date())
PY
)
STATE=$(
  python3 - <<'PY'
import json

with open("runs/paper/state.json", "r", encoding="utf-8") as fh:
    print(json.load(fh)["last_bar"])
PY
)

if [[ "$LATEST" > "$STATE" ]]; then
  python3 -m meridian paper
else
  echo "paper déjà à jour : $STATE (données $LATEST)"
fi

python3 -m meridian monitor
echo "=== fin $(date -u +%FT%TZ) ==="
