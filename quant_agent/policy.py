"""Politique Kraken : budgets, outils autorisés et prompt système.

« Kraken » n'est pas un modèle à entraîner. C'est la politique
d'orchestration par défaut : planifier, appeler des outils, vérifier les
chiffres, répondre avec les run_id et les sources.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tools import TOOL_DEFS

ALLOWED_TOOLS = frozenset(item["name"] for item in TOOL_DEFS)


@dataclass(frozen=True)
class Mode:
    name: str
    max_tool_calls: int
    max_steps: int
    max_seconds: float
    description: str


MODES: dict[str, Mode] = {
    "fast": Mode(
        name="fast",
        max_tool_calls=2,
        max_steps=6,
        max_seconds=75.0,
        description="Réponse courte, un ou deux outils.",
    ),
    "kraken": Mode(
        name="kraken",
        max_tool_calls=8,
        max_steps=12,
        max_seconds=180.0,
        description="Défaut : plan, outils, vérification, réponse auditée.",
    ),
    "deep": Mode(
        name="deep",
        max_tool_calls=12,
        max_steps=20,
        max_seconds=300.0,
        description="Plus d'étapes et de vérification, pour une analyse longue.",
    ),
}


class Policy:
    def __init__(self, mode: str = "kraken"):
        key = (mode or "kraken").strip().lower()
        if key not in MODES:
            raise ValueError(
                f"mode inconnu « {mode} » ; disponibles : {', '.join(MODES)}"
            )
        self.mode = MODES[key]

    @property
    def name(self) -> str:
        return self.mode.name

    @property
    def max_tool_calls(self) -> int:
        return self.mode.max_tool_calls

    @property
    def max_steps(self) -> int:
        return self.mode.max_steps

    @property
    def max_seconds(self) -> float:
        return self.mode.max_seconds

    def can_call_tool(self, name: str) -> bool:
        return name in ALLOWED_TOOLS

    def system_prompt(self, usage: dict[str, Any] | None = None) -> str:
        tools = ", ".join(sorted(ALLOWED_TOOLS))
        lines = [
            "Tu es Meridian Research, copilote de recherche quant pour des "
            "stratégies crypto daily, long-only spot, sans levier et sans short.",
            "",
            "Règles non négociables :",
            "1. Tout chiffre de performance, de risque ou de portefeuille doit "
            "venir d'un outil. Tu n'inventes jamais un chiffre, un run_id ou une date.",
            "2. Tu ne passes aucun ordre réel et tu ne promets aucun rendement. "
            "Le mode est paper only. Tu n'es pas un conseiller financier.",
            "3. Si un outil renvoie une erreur, tu le dis clairement et tu proposes "
            "une action de remédiation. Tu ne masques pas l'erreur.",
            "4. Quand tu utilises un backtest, cite son run_id. Quand tu compares, "
            "lance les deux backtests puis utilise compare_backtests.",
            "5. Si la demande sort du périmètre (trading réel, levier, short, "
            "prévision de prix, conseil d'investissement personnalisé), refuse "
            "brièvement et propose une alternative de recherche.",
            "6. Réponds dans la langue de l'utilisateur, en français par défaut. "
            "Sois concis : 5 à 12 lignes, sauf demande de détail.",
            "",
            f"Outils autorisés : {tools}.",
            f"Budget : {self.max_tool_calls} appels d'outils, "
            f"{self.max_steps} étapes, {int(self.max_seconds)} secondes.",
            "Le cache de backtests est prioritaire : un run identique ne coûte "
            "pas de quota.",
            "",
            "Stratégies disponibles : trend_momentum (défaut) et mean_reversion. "
            "La fenêtre par défaut est d'environ 2 ans ; l'utilisateur peut "
            "demander une autre période.",
        ]
        if usage:
            used = usage.get("used") or {}
            limits = usage.get("limits") or {}
            lines.extend(
                [
                    "",
                    "Quotas locaux du jour : "
                    f"messages {used.get('messages', 0)}/{limits.get('messages', '?')}, "
                    f"backtests {used.get('backtests', 0)}/{limits.get('backtests', '?')}, "
                    f"tokens {used.get('tokens', 0)}/{limits.get('tokens', '?')}.",
                ]
            )
        return "\n".join(lines)
