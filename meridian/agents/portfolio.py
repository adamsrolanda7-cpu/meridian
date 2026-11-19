"""Allocation de portefeuille : du signal au poids cible.

Séquence :
1. mélange des sleeves selon le régime ;
2. score composite par actif, ajusté par la confiance ;
3. pondération inverse-vol ;
4. contrainte de concentration, de liquidité et de gross ;
5. ciblage de volatilité (plafond, pas de levier) ;
6. bande morte de rééquilibrage pour limiter le turnover.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get
from ..types import Regime, Target
from .base import Agent

REGIME_BLEND: dict[str, dict[str, float]] = {
    "TREND_UP": {"trend": 0.55, "xsmom": 0.35, "meanrev": 0.10},
    "RANGE": {"trend": 0.20, "xsmom": 0.15, "meanrev": 0.65},
    "TREND_DOWN": {"trend": 0.0, "xsmom": 0.0, "meanrev": 0.0},
    "CRISIS": {"trend": 0.0, "xsmom": 0.0, "meanrev": 0.0},
    "WARMUP": {"trend": 0.0, "xsmom": 0.0, "meanrev": 0.0},
}


@dataclass
class Allocation:
    targets: list[Target]
    gross: float
    portfolio_vol: float
    sleeve_weights: dict[str, float]
    composite: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    decision_scores: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    sleeves: dict[str, str] = field(default_factory=dict)


class PortfolioAgent(Agent):
    name = "portfolio"
    role = "Combiner les sleeves, dimensionner en inverse-vol et plafonner le risque agrégé."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.target_vol = float(get(cfg, "strategy.target_vol", 0.18))
        self.max_gross = float(get(cfg, "strategy.max_gross", 0.95))
        self.min_cash = float(get(cfg, "strategy.min_cash", 0.05))
        self.max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))
        self.threshold = float(get(cfg, "strategy.rebalance_threshold", 0.025))
        self.vol_floor = float(get(cfg, "risk.vol_floor", 0.20))
        self.pos_adv = float(get(cfg, "risk.position_adv_pct", 0.01))
        self.use_confidence = bool(get(cfg, "strategy.use_confidence", True))
        self.cov_shrink = float(get(cfg, "strategy.cov_shrink", 0.30))
        self.entry_score = float(get(cfg, "strategy.entry_score", 0.25))
        self.exit_score = float(get(cfg, "strategy.exit_score", 0.12))
        self.asset_caps = dict(get(cfg, "strategy.asset_caps", {}) or {})
        self.core_btc = float(get(cfg, "strategy.core_btc", 0.0))

    def build(
        self,
        date: pd.Timestamp,
        regime: Regime,
        scores: dict[str, dict[str, tuple[float, float]]],
        vols: dict[str, float],
        cov: np.ndarray,
        assets: list[str],
        current: dict[str, float],
        equity: float,
        adv: dict[str, float],
        mr_exit: set[str] | None = None,
        blocked: set[str] | None = None,
    ) -> Allocation:
        mr_exit = mr_exit or set()
        blocked = blocked or set()
        blend = REGIME_BLEND.get(regime.label, REGIME_BLEND["RANGE"])
        gross_cap = min(self.max_gross, 1.0 - self.min_cash)
        notes: list[str] = []

        if regime.label in ("TREND_DOWN", "CRISIS", "WARMUP"):
            targets = [Target(a, 0.0, 0.0, f"régime {regime.label} → cash") for a in assets]
            notes.append(f"{regime.label}: exposition forcée à 0 (préservation du capital)")
            return Allocation(targets, 0.0, 0.0, dict(blend), {a: 0.0 for a in assets}, notes)

        composite: dict[str, float] = {}
        dominant: dict[str, str] = {}
        for asset in assets:
            if asset in blocked:
                composite[asset] = 0.0
                dominant[asset] = "blocked"
                continue
            total = 0.0
            conviction_num = 0.0
            best_sleeve = ""
            best_contrib = 0.0
            for sleeve, sleeve_w in blend.items():
                score, conf = scores.get(asset, {}).get(sleeve, (0.0, 0.0))
                if sleeve == "meanrev" and asset in mr_exit:
                    score = 0.0
                contrib = sleeve_w * max(0.0, score)
                total += contrib
                conviction_num += contrib * max(0.0, min(1.0, conf))
                if contrib > best_contrib:
                    best_contrib = contrib
                    best_sleeve = sleeve
            conviction = conviction_num / total if total > 0 else 0.0
            if self.use_confidence and total > 0:
                total *= 0.60 + 0.40 * conviction
            composite[asset] = float(np.clip(total, 0.0, 1.0))
            dominant[asset] = best_sleeve or "none"

        # Bandes d'entrée/sortie : on n'entre que sur un score suffisant, on
        # ne sort que si le signal se dégrade vraiment. C'est le principal
        # rempart contre le turnover et les allers-retours autour d'un seuil.
        for asset in assets:
            current_w = max(0.0, float(current.get(asset, 0.0)))
            if current_w > 1e-4:
                if composite[asset] < self.exit_score:
                    composite[asset] = 0.0
                    dominant[asset] = "exit"
            elif composite[asset] < self.entry_score:
                composite[asset] = 0.0
                dominant[asset] = "none"

        raw = {
            a: composite[a] / max(float(vols.get(a, np.nan)) if np.isfinite(vols.get(a, np.nan)) else 1.0, self.vol_floor)
            for a in assets
            if composite[a] > 0.05
        }
        if not raw:
            targets = [Target(a, 0.0, composite[a], "aucun signal exploitable") for a in assets]
            notes.append("aucun score au-dessus du seuil → cash")
            return Allocation(targets, 0.0, 0.0, dict(blend), composite, notes)

        total_raw = sum(raw.values())
        weights = {a: raw.get(a, 0.0) / total_raw * gross_cap for a in assets}

        # Plafonds individuels + liquidité (position <= x% de l'ADV 20j).
        caps = {}
        for a in assets:
            cap = self.max_asset
            if a in self.asset_caps:
                cap = min(cap, float(self.asset_caps[a]))
            adv_a = float(adv.get(a, np.nan))
            if np.isfinite(adv_a) and adv_a > 0 and equity > 0:
                cap = min(cap, self.pos_adv * adv_a / equity)
            caps[a] = max(0.0, cap)

        for _ in range(8):
            excess = 0.0
            for a in assets:
                if weights[a] > caps[a] + 1e-12:
                    excess += weights[a] - caps[a]
                    weights[a] = caps[a]
            if excess <= 1e-12:
                break
            free = [a for a in assets if weights[a] < caps[a] - 1e-12 and raw.get(a, 0) > 0]
            if not free:
                break
            denom = sum(raw[a] for a in free)
            if denom <= 0:
                break
            for a in free:
                weights[a] += excess * raw[a] / denom

        # Ciblage de volatilité : on réduit si le portefeuille est trop volatil.
        # On n'augmente pas au-delà du gross autorisé (pas de levier caché).
        cov_ann = np.asarray(cov, dtype=float) * 365.0
        cov_ann = np.nan_to_num(cov_ann, nan=0.0, posinf=0.0, neginf=0.0)
        cov_ann = 0.5 * (cov_ann + cov_ann.T)
        diag = np.diag(np.diag(cov_ann))
        cov_ann = (1.0 - self.cov_shrink) * cov_ann + self.cov_shrink * diag
        w_vec = np.array([weights[a] for a in assets])
        variance = float(w_vec @ cov_ann @ w_vec)
        portfolio_vol = float(np.sqrt(max(variance, 0.0)))
        if portfolio_vol > self.target_vol > 0:
            scale = self.target_vol / portfolio_vol
            weights = {a: weights[a] * scale for a in assets}
            portfolio_vol = self.target_vol
            notes.append(f"vol cible: réduction ×{scale:.2f}")

        # Core BTC stratégique : uniquement en régime haussier confirmé. Il
        # assure une exposition au beta crypto quand les signaux satellites
        # sont trop prudents, sans jamais s'activer en risk-off.
        if self.core_btc > 0 and "BTCUSDT" in weights and regime.label == "TREND_UP":
            btc_weight = min(gross_cap, max(weights.get("BTCUSDT", 0.0), self.core_btc))
            others = [a for a in assets if a != "BTCUSDT"]
            other_sum = sum(weights[a] for a in others)
            available = max(0.0, gross_cap - btc_weight)
            if other_sum > available > 0:
                factor = available / other_sum
                for a in others:
                    weights[a] *= factor
            elif other_sum > 0 and available <= 0:
                for a in others:
                    weights[a] = 0.0
            weights["BTCUSDT"] = btc_weight
            if btc_weight > 0:
                w_vec = np.array([weights[a] for a in assets])
                portfolio_vol = float(np.sqrt(max(w_vec @ cov_ann @ w_vec, 0.0)))
                notes.append(f"core BTC {btc_weight:.0%} (régime haussier)")

        # Bande morte : on ne paie pas des frais pour 50 bp de dérive.
        final: dict[str, float] = {}
        for a in assets:
            cur = max(0.0, float(current.get(a, 0.0)))
            tgt = weights[a]
            if tgt <= 1e-9:
                final[a] = 0.0
            elif abs(tgt - cur) < self.threshold:
                final[a] = cur
            else:
                final[a] = tgt
        gross = sum(final.values())
        if gross > gross_cap + 1e-12:
            factor = gross_cap / gross
            final = {a: w * factor for a in assets}
            gross = gross_cap
            notes.append("gross plafonné après rééquilibrage")

        targets = []
        for a in assets:
            if final[a] <= 1e-9:
                reason = "sortie / hors portefeuille"
            else:
                reason = (
                    f"{dominant[a]} score {composite[a]:.2f}, "
                    f"vol {vols.get(a, float('nan')):.0%}"
                )
            targets.append(Target(a, float(final[a]), float(composite[a]), reason))

        if regime.label == "RANGE":
            notes.append("régime RANGE: mean-reversion surpondérée, tendance réduite")
        if gross < 0.10:
            notes.append("exposition faible: signaux dispersés ou volatilité élevée")

        return Allocation(
            targets=targets,
            gross=float(gross),
            portfolio_vol=float(portfolio_vol),
            sleeve_weights=dict(blend),
            composite=composite,
            notes=notes,
            decision_scores=scores,
            sleeves=dominant,
        )
