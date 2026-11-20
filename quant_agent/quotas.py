"""Quotas quotidiens mock, stockés dans ``runs/agent/usage.json``.

Ce n'est pas un système anti-abus de production : c'est le même contrat
d'usage que la v1, avec un backend local. Les limites sont configurables par
variables d'environnement pour les tests et les démos.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from meridian.config import ROOT

DEFAULT_LIMITS: dict[str, int] = {
    "messages": 20,
    "backtests": 5,
    "tokens": 100_000,
}

_ENV_LIMITS = {
    "messages": "QUANT_AGENT_DAILY_MESSAGES",
    "backtests": "QUANT_AGENT_DAILY_BACKTESTS",
    "tokens": "QUANT_AGENT_DAILY_TOKENS",
}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _reset_at() -> str:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(
        tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc
    ).isoformat(timespec="seconds")


def limits_from_env() -> dict[str, int]:
    limits = dict(DEFAULT_LIMITS)
    for key, env_name in _ENV_LIMITS.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value >= 0:
            limits[key] = value
    return limits


class QuotaExceeded(RuntimeError):
    """Quota journalier dépassé."""

    def __init__(self, kind: str, limit: int, reset_at: str):
        self.kind = kind
        self.limit = limit
        self.reset_at = reset_at
        labels = {
            "messages": "messages",
            "backtests": "backtests",
            "tokens": "tokens",
        }
        super().__init__(
            f"quota {labels.get(kind, kind)} atteint ({limit}/jour). "
            f"Réinitialisation : {reset_at}."
        )


class QuotaManager:
    """Compteurs journaliers simples, atomiques et lisibles."""

    def __init__(
        self,
        root: str | None = None,
        limits: dict[str, int] | None = None,
        enabled: bool = True,
    ):
        self.root = root or ROOT
        self.path = os.path.join(self.root, "runs", "agent", "usage.json")
        self.limits = dict(limits or limits_from_env())
        self.enabled = bool(enabled)

    # ------------------------------------------------------------------ state
    def _empty(self) -> dict[str, Any]:
        return {
            "version": 1,
            "date": _today(),
            "messages": 0,
            "backtests": 0,
            "tokens": 0,
            "updated_at": None,
        }

    def _load(self) -> dict[str, Any]:
        data = self._empty()
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict) and raw.get("date") == data["date"]:
                    for key in ("messages", "backtests", "tokens"):
                        data[key] = max(0, int(raw.get(key, 0)))
                    data["updated_at"] = raw.get("updated_at")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
        return data

    def _save(self, data: dict[str, Any]) -> None:
        if not self.enabled:
            return
        data["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ usage
    def check(self, kind: str, amount: int = 1) -> None:
        if not self.enabled or kind not in self.limits:
            return
        data = self._load()
        limit = int(self.limits[kind])
        if int(data.get(kind, 0)) + max(0, int(amount)) > limit:
            raise QuotaExceeded(kind, limit, _reset_at())

    def consume(self, kind: str, amount: int = 1) -> None:
        if not self.enabled or kind not in self.limits or amount <= 0:
            return
        self.check(kind, amount)
        data = self._load()
        data[kind] = int(data.get(kind, 0)) + int(amount)
        self._save(data)

    def add_tokens(self, amount: int) -> None:
        """Enregistre les tokens réellement consommés, même au-delà du quota."""
        if not self.enabled or amount <= 0:
            return
        data = self._load()
        data["tokens"] = int(data.get("tokens", 0)) + int(amount)
        self._save(data)

    def reset(self) -> None:
        data = self._empty()
        self._save(data)

    def snapshot(self) -> dict[str, Any]:
        data = self._load()
        used = {key: int(data.get(key, 0)) for key in self.limits}
        remaining = {
            key: max(0, int(self.limits[key]) - used[key]) for key in self.limits
        }
        return {
            "date": data["date"],
            "enabled": self.enabled,
            "limits": dict(self.limits),
            "used": used,
            "remaining": remaining,
            "reset_at": _reset_at(),
            "updated_at": data.get("updated_at"),
            "path": self.path,
        }

    def format_text(self) -> str:
        snap = self.snapshot()
        lines = [
            f"Usage local · {snap['date']}",
            f"Messages  : {snap['used']['messages']:>4} / {snap['limits']['messages']:<6} "
            f"(reste {snap['remaining']['messages']})",
            f"Backtests : {snap['used']['backtests']:>4} / {snap['limits']['backtests']:<6} "
            f"(reste {snap['remaining']['backtests']})",
            f"Tokens    : {snap['used']['tokens']:>6} / {snap['limits']['tokens']:<8} "
            f"(reste {snap['remaining']['tokens']})",
            f"Reset     : {snap['reset_at']}",
        ]
        if not snap["enabled"]:
            lines.append("Quotas désactivés pour cette session (--no-quota).")
        return "\n".join(lines)
