"""Chargement minimal d'un fichier ``.env``, sans dépendance externe.

Le fichier ``.env`` n'est jamais commité. Les variables déjà présentes dans
l'environnement ont priorité : cela permet d'injecter les secrets par un
gestionnaire de secrets, un shell ou un CI sans être écrasé par le fichier.

Format supporté : ``KEY=VALUE``, ``export KEY=VALUE``, commentaires ``#``,
valeurs entre guillemets simples ou doubles. Aucune valeur n'est journalisée.
"""
from __future__ import annotations

import os
from pathlib import Path

from .config import ROOT

ENV_PATH = os.path.join(ROOT, ".env")


def parse_env(text: str) -> dict[str, str]:
    """Parse le contenu d'un fichier ``.env`` en dictionnaire."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not all(ch.isalnum() or ch == "_" for ch in key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (
                    value.replace("\\n", "\n")
                    .replace('\\"', '"')
                    .replace("\\\\", "\\")
                )
        else:
            # Un commentaire inline n'est reconnu que précédé d'un espace,
            # pour ne pas casser une valeur contenant un #.
            for marker in (" #", "\t#"):
                index = value.find(marker)
                if index >= 0:
                    value = value[:index].rstrip()
                    break
        values[key] = value
    return values


def load_env(path: str | None = None, override: bool = False) -> dict[str, str]:
    """Charge le fichier ``.env`` et retourne les valeurs trouvées.

    ``override=False`` par défaut : une variable déjà exportée gagne. Le
    chemin peut être forcé par ``MERIDIAN_ENV_FILE``.
    """
    path = path or os.environ.get("MERIDIAN_ENV_FILE") or ENV_PATH
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        values = parse_env(fh.read())
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def env(name: str, default: str = "") -> str:
    """Lit une variable d'environnement nettoyée."""
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def env_float(name: str, default: float) -> float:
    raw = env(name, "")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un nombre, reçu: {raw!r}") from exc


def require_env(*names: str) -> dict[str, str]:
    """Exige la présence de variables non vides, sans exposer leur valeur."""
    missing = [name for name in names if not env(name)]
    if missing:
        raise RuntimeError(
            "variables d'environnement manquantes : "
            + ", ".join(missing)
            + " (voir .env.example)"
        )
    return {name: env(name) for name in names}


def mask_secret(value: str, visible: int = 4) -> str:
    """Retourne une version masquée d'un secret pour les logs."""
    if not value:
        return "(vide)"
    if len(value) <= visible:
        return "*" * len(value)
    return value[:visible] + "…" + "*" * max(0, len(value) - visible - 1)
