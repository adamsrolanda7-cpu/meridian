"""Superviseur : contrôle de cohérence, journal et memo de fin de séance.

C'est le garde-fou humain du système : il ne produit pas d'alpha, il vérifie
que la décision respecte le mandat et la rend lisible. Le memo est généré par
template à partir des objets structurés — pas d'appel LLM dans la chaîne
d'exécution, pour garantir reproductibilité et auditabilité.
"""
from __future__ import annotations

import json
import math
import os

import numpy as np
import pandas as pd

from ..config import get
from ..portfolio import Book
from ..types import Decision
from .base import Agent


def _pct(value: float, digits: int = 1) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _money(value: float) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:,.0f} $".replace(",", " ")


class SupervisorAgent(Agent):
    name = "supervisor"
    role = "Vérifier les invariants, journaliser et produire le memo de séance."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.max_gross = float(get(cfg, "strategy.max_gross", 0.95))
        self.max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))

    def validate(self, decision: Decision, book: Book) -> list[str]:
        problems: list[str] = []
        gross = sum(max(0.0, t.weight) for t in decision.targets)
        if gross > self.max_gross + 1e-6:
            problems.append(f"gross cible {gross:.4f} > plafond {self.max_gross:.4f}")
        for target in decision.targets:
            if not math.isfinite(target.weight):
                problems.append(f"poids non fini pour {target.asset}")
            elif target.weight > self.max_asset + 1e-6:
                problems.append(f"{target.asset}: poids {target.weight:.3f} > {self.max_asset:.3f}")
        if book.cash < -1e-6:
            problems.append(f"cash négatif: {book.cash:.2f}")
        if book.equity() <= 0:
            problems.append("equity <= 0")
        for asset, pos in book.positions.items():
            if pos.qty < 0:
                problems.append(f"{asset}: quantité négative")
            if pos.stop <= 0:
                problems.append(f"{asset}: stop absent")
        return problems

    def memo(
        self,
        decision: Decision,
        book: Book,
        governor,
        execution=None,
        quality: dict | None = None,
    ) -> str:
        regime = decision.regime
        equity = book.equity()
        gross = sum(max(0.0, t.weight) for t in decision.targets)
        lines: list[str] = []
        lines.append(f"# Meridian — memo de séance · {pd.Timestamp(decision.ts).date()}")
        lines.append("")
        lines.append(f"**Régime** : `{regime.label}` (confiance {_pct(regime.confidence, 0)})  ")
        lines.append(f"**Lecture** : {regime.reason}  ")
        lines.append(
            f"**Equity** : {_money(equity)} · **exposition cible** : {_pct(gross)} · "
            f"**vol estimée** : {_pct(decision.portfolio_vol)} · **drawdown** : {_pct(governor.drawdown)}"
        )
        lines.append("")

        lines.append("## 1. Signaux")
        lines.append("| Actif | Tendance | Mean-rev | XS momentum | Composite |")
        lines.append("|---|---:|---:|---:|---:|")
        by_asset: dict[str, dict[str, float]] = {}
        for signal in decision.signals:
            by_asset.setdefault(signal.asset, {})[signal.sleeve] = signal.score
        composite = {t.asset: t.score for t in decision.targets}
        for asset in sorted(by_asset):
            row = by_asset[asset]
            lines.append(
                f"| {asset} | {row.get('trend', 0):.2f} | {row.get('meanrev', 0):.2f} | "
                f"{row.get('xsmom', 0):.2f} | {composite.get(asset, 0):.2f} |"
            )
        lines.append("")

        lines.append("## 2. Portefeuille cible")
        lines.append("| Actif | Poids | Notionnel | Stop | Score |")
        lines.append("|---|---:|---:|---:|---:|")
        signals_by_asset = {s.asset: s for s in decision.signals if s.sleeve == "trend"}
        for target in sorted(decision.targets, key=lambda t: t.weight, reverse=True):
            if target.weight <= 1e-9:
                continue
            pos = book.positions.get(target.asset)
            stop_text = f"{pos.stop:,.2f}" if pos else "—"
            lines.append(
                f"| {target.asset} | {_pct(target.weight)} | {_money(target.weight * equity)} | "
                f"{stop_text} | {target.score:.2f} |"
            )
        if all(t.weight <= 1e-9 for t in decision.targets):
            lines.append("| — | 0.0% | cash | — | — |")
        lines.append("")

        lines.append("## 3. Ordres à l'ouverture suivante")
        if decision.orders:
            lines.append("| Actif | Poids cible | Motif |")
            lines.append("|---|---:|---|")
            for order in decision.orders:
                lines.append(f"| {order.asset} | {_pct(order.target_weight)} | {order.reason[:90]} |")
        else:
            lines.append("Aucun ordre : portefeuille dans la bande morte de rééquilibrage.")
        lines.append("")

        lines.append("## 4. Risque et exécution")
        for note in decision.risk_notes:
            lines.append(f"- {note}")
        if execution is not None:
            for note in execution.notes:
                lines.append(f"- exécution : {note}")
            if execution.fills:
                lines.append(
                    f"- {len(execution.fills)} fill(s), frais "
                    f"{_money(sum(f.fee for f in execution.fills))}"
                )
        if quality:
            stale = max((q.get("stale_days", 0) for q in quality.values()), default=0)
            lines.append(f"- qualité données : {len(quality)} actifs, staleness max {stale} j")
        lines.append("")
        lines.append("## 5. Positions ouvertes")
        if book.positions:
            lines.append("| Actif | Qté | PRU | Dernier | Stop | PnL latent | Sleeve |")
            lines.append("|---|---:|---:|---:|---:|---:|---|")
            for asset, pos in sorted(book.positions.items()):
                pnl = (pos.last_price - pos.avg_price) * pos.qty
                lines.append(
                    f"| {asset} | {pos.qty:.6f} | {pos.avg_price:,.2f} | {pos.last_price:,.2f} | "
                    f"{pos.stop:,.2f} | {_money(pnl)} | {pos.sleeve or '—'} |"
                )
        else:
            lines.append("Aucune position.")
        lines.append("")
        lines.append("---")
        lines.append(
            "Document de recherche. Aucun ordre réel n'est envoyé par ce module. "
            "Les performances passées ne préjugent pas des performances futures."
        )
        return "\n".join(lines)

    def write(self, path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)

    def write_json(self, path: str, payload) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, path)
