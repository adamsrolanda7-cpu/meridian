"""Agent de risque : le seul composant qui peut réduire ou bloquer une décision.

Trois couches :
1. stops par position (ATR, trailing) et cooldown après sortie ;
2. gouverneur de portefeuille (drawdown, perte journalière) ;
3. contrôle pré-exécution (concentration, gross, liquidité, mode sans levier).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..config import get
from ..types import Position, Target
from .base import Agent


@dataclass
class Governor:
    scale: float
    no_new_risk: bool
    drawdown: float
    daily_return: float
    halted: bool
    notes: list[str] = field(default_factory=list)


class RiskAgent(Agent):
    name = "risk"
    role = "Veto, dimensionnement, stops et circuit breakers. Ne cherche pas le rendement."

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.stop_atr = float(get(cfg, "risk.stop_atr", 3.0))
        self.mr_stop_atr = float(get(cfg, "risk.mr_stop_atr", 2.0))
        self.trail_atr = float(get(cfg, "risk.trail_atr", 3.0))
        self.hard_stop_pct = float(get(cfg, "risk.hard_stop_pct", 0.0))
        self.cooldown_bars = int(get(cfg, "risk.cooldown_bars", 3))
        self.daily_limit = float(get(cfg, "risk.daily_loss_limit", 0.03))
        self.dd_derisk = float(get(cfg, "risk.dd_derisk", 0.08))
        self.dd_halt = float(get(cfg, "risk.dd_halt", 0.12))
        self.halt_bars = int(get(cfg, "risk.halt_bars", 20))
        self.probation_bars = int(get(cfg, "risk.probation_bars", 30))
        self.probation_scale = float(get(cfg, "risk.probation_scale", 0.25))
        self.halt_step = float(get(cfg, "risk.halt_step", 0.05))
        self.max_asset = float(get(cfg, "strategy.max_asset_weight", 0.35))
        self.max_gross = float(get(cfg, "strategy.max_gross", 0.95))
        self.vol_floor = float(get(cfg, "risk.vol_floor", 0.20))

        self.peak: float | None = None
        self.prev_equity: float | None = None
        self.halted = False
        self.halt_until = -1
        self.probation_until = -1
        self.last_halt_dd = 0.0
        self.cooldowns: dict[str, int] = {}
        self.history: list[dict] = []

    # ------------------------------------------------------------- gouverneur
    def update(self, equity: float, index: int) -> Governor:
        notes: list[str] = []
        if self.peak is None:
            self.peak = equity
            self.prev_equity = equity
        assert self.prev_equity is not None and self.peak is not None
        drawdown = equity / self.peak - 1.0 if self.peak > 0 else 0.0
        daily_return = equity / self.prev_equity - 1.0 if self.prev_equity > 0 else 0.0

        if self.halted and index >= self.halt_until:
            self.halted = False
            self.probation_until = index + self.probation_bars
            notes.append(
                f"fin du gel: reprise à {self.probation_scale:.0%} pendant {self.probation_bars} séances"
            )

        # Nouveau gel uniquement si le drawdown s'aggrave sensiblement depuis
        # le dernier gel. Sinon le système resterait bloqué à vie sous -12%.
        if (
            not self.halted
            and drawdown <= -self.dd_halt
            and drawdown <= self.last_halt_dd - self.halt_step
        ):
            self.halted = True
            self.halt_until = index + self.halt_bars
            self.last_halt_dd = drawdown
            notes.append(
                f"drawdown {drawdown:.1%} ≤ -{self.dd_halt:.0%}: gel du risque pendant {self.halt_bars} séances"
            )

        if drawdown > -self.dd_derisk:
            self.last_halt_dd = 0.0  # réarmement après guérison

        if self.halted:
            scale = 0.0
            notes.append("gouverneur: exposition 0 (halt)")
        elif index < self.probation_until:
            scale = self.probation_scale
            notes.append(
                f"gouverneur: probation post-halt, exposition {self.probation_scale:.0%}"
            )
        elif drawdown <= -self.dd_derisk:
            scale = 0.5
            notes.append(f"drawdown {drawdown:.1%}: exposition réduite de moitié")
        else:
            scale = 1.0

        no_new_risk = daily_return <= -self.daily_limit
        if no_new_risk:
            notes.append(f"perte journalière {daily_return:.1%}: aucun ajout de risque")

        governor = Governor(
            scale=scale,
            no_new_risk=no_new_risk,
            drawdown=drawdown,
            daily_return=daily_return,
            halted=self.halted,
            notes=notes,
        )
        self.history.append(
            {
                "equity": float(equity),
                "drawdown": float(drawdown),
                "daily_return": float(daily_return),
                "scale": float(scale),
                "halted": bool(self.halted),
            }
        )
        self.peak = max(self.peak, equity)
        self.prev_equity = equity
        return governor

    def apply(
        self,
        targets: list[Target],
        governor: Governor,
        current_weights: dict[str, float],
        index: int,
    ) -> tuple[list[Target], list[str]]:
        notes = list(governor.notes)
        if governor.scale <= 0:
            out = [Target(t.asset, 0.0, t.score, "risk-off: liquidation") for t in targets]
            notes.append("application: portefeuille liquidé")
            return out, notes

        blocked = {a for a, until in self.cooldowns.items() if until > index}
        out: list[Target] = []
        for target in targets:
            weight = max(0.0, target.weight) * governor.scale
            current = max(0.0, current_weights.get(target.asset, 0.0))
            if target.asset in blocked:
                weight = 0.0
                notes.append(f"{target.asset}: cooldown après stop")
            if governor.no_new_risk:
                weight = min(weight, current)
            if weight > self.max_asset:
                weight = self.max_asset
                notes.append(f"{target.asset}: plafond actif {self.max_asset:.0%}")
            out.append(Target(target.asset, weight, target.score, target.reason))

        gross = sum(t.weight for t in out)
        if gross > self.max_gross > 0:
            factor = self.max_gross / gross
            out = [Target(t.asset, t.weight * factor, t.score, t.reason) for t in out]
            notes.append("gross plafonné par le RiskAgent")
        return out, notes

    # ------------------------------------------------------------------ stops
    def entry_stop(self, price: float, atr: float, sleeve: str) -> float:
        if not np.isfinite(atr) or atr <= 0:
            atr = price * 0.03
        mult = self.mr_stop_atr if sleeve == "meanrev" else self.stop_atr
        return max(0.0, price - mult * atr)

    def update_trailing(self, pos: Position, close: float, atr: float) -> None:
        if pos.sleeve not in ("trend", "xsmom"):
            return
        if self.trail_atr <= 0 or not np.isfinite(atr) or atr <= 0:
            return
        pos.stop = max(pos.stop, close - self.trail_atr * atr)

    def set_cooldown(self, asset: str, index: int) -> None:
        self.cooldowns[asset] = index + self.cooldown_bars

    def blocked(self, index: int) -> set[str]:
        return {a for a, until in self.cooldowns.items() if until > index}

    # --------------------------------------------------------------- contrôles
    def pretrade_check(
        self,
        asset: str,
        weight: float,
        equity: float,
        adv: float,
        prices: dict[str, float],
    ) -> tuple[bool, str]:
        if weight <= 0:
            return True, ""
        if not asset.endswith("USDT"):
            return False, "actif hors mandat"
        if weight > self.max_asset + 1e-9:
            return False, f"poids {weight:.1%} > plafond {self.max_asset:.0%}"
        if equity <= 0:
            return False, "equity invalide"
        if adv and np.isfinite(adv) and adv > 0:
            max_position = float(get(self.cfg, "risk.position_adv_pct", 0.01)) * adv
            if weight * equity > max_position:
                return False, "position > limite de liquidité (ADV)"
        price = prices.get(asset)
        if price is None or not np.isfinite(price) or price <= 0:
            return False, "prix indisponible"
        return True, ""
