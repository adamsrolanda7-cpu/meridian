#!/usr/bin/env bash
# Meridian — observation du paper trading dans le terminal.
#
# Lecture seule : aucune clé API, aucun ordre réel, aucune écriture du paper.
# Par défaut, rafraîchit l'écran en continu. Utiliser --plain pour une seule
# trame, ou --window pour tenter une nouvelle fenêtre de terminal.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERREUR : python3 est introuvable." >&2
  exit 1
fi

# Sans argument : mode continu texte, sans curses et sans exécuter le paper.
if [ "$#" -eq 0 ]; then
  set -- --follow
fi

exec python3 -m meridian live \
  --offline \
  --no-auto-run \
  --no-write-monitor \
  "$@"
