"""Paper trading ombragé, déterministe et sans ordre réel.

Principe : le portefeuille papier est reconstruit par *replay* du moteur sur
les données réelles depuis la date d'initialisation. Le state JSON persiste le
capital de départ, la dernière séance traitée, le snapshot courant et les
intentions en attente. À chaque exécution, les intentions de la veille sont
exécutées à l'ouverture de la nouvelle bougie par le même agent d'exécution
que le backtest, puis les nouvelles intentions sont produites pour l'ouverture
suivante.

Pourquoi un replay plutôt qu'un état incrémental fragile ?
- une seule source de vérité : le couple (config, données, date de départ) ;
- aucune dérive possible entre le backtest et le paper ;
- les intentions en attente sont vérifiées par réconciliation avec les fills ;
- en cas de crash, l'état se reconstruit sans ambiguïté.

Aucun connecteur d'exchange n'est présent. Un mode différent de ``paper`` est
refusé explicitement : l'ajout du live devra passer par un adaptateur dédié,
une gestion de clés hors dépôt et une revue de risque séparée.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .agents.supervisor import SupervisorAgent
from .backtest import Backtest, BacktestResult
from .config import ROOT, get, load_config
from .data import ensure_data, quality_report
from .features import FeatureStore

PAPER_DIR = os.path.join(ROOT, "runs", "paper")
STATE_PATH = os.path.join(PAPER_DIR, "state.json")
JOURNAL_PATH = os.path.join(PAPER_DIR, "journal.jsonl")
MEMO_PATH = os.path.join(PAPER_DIR, "memo.md")
FILLS_PATH = os.path.join(PAPER_DIR, "fills.csv")
SNAPSHOT_PATH = os.path.join(PAPER_DIR, "snapshot.json")


class PaperError(RuntimeError):
    pass


class LiveTradingDisabled(PaperError):
    """Le desk refuse toute exécution réelle."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def config_hash(cfg: dict) -> str:
    payload = json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _write_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def assert_paper_only(cfg: dict) -> None:
    mode = str(get(cfg, "execution.mode", "paper")).lower()
    if mode != "paper":
        raise LiveTradingDisabled(
            f"mode d'exécution '{mode}' refusé : ce module ne sait pas envoyer d'ordre réel. "
            "Le live exige un adaptateur broker, des clés hors dépôt et une validation humaine."
        )
    if get(cfg, "execution.broker"):
        raise LiveTradingDisabled("un broker est configuré alors que le mode paper est attendu")


@dataclass
class PaperState:
    version: int
    created_at: str
    updated_at: str
    config_hash: str
    start: str
    initial_equity: float
    last_bar: str
    equity: float
    cash: float
    positions: dict[str, dict] = field(default_factory=dict)
    pending_orders: list[dict] = field(default_factory=list)
    last_regime: str = ""
    gross_target: float = 0.0
    drawdown: float = 0.0
    data_source: str = ""
    runs: list[dict] = field(default_factory=list)

    @classmethod
    def new(cls, cfg: dict, start: str, initial_equity: float) -> "PaperState":
        now = _now()
        return cls(
            version=1,
            created_at=now,
            updated_at=now,
            config_hash=config_hash(cfg),
            start=start,
            initial_equity=float(initial_equity),
            last_bar=start,
            equity=float(initial_equity),
            cash=float(initial_equity),
            positions={},
            pending_orders=[],
            data_source="",
        )

    @classmethod
    def load(cls, path: str | None = None) -> "PaperState":
        path = path or STATE_PATH
        raw = _read_json(path)
        return cls(
            version=int(raw.get("version", 1)),
            created_at=raw["created_at"],
            updated_at=raw["updated_at"],
            config_hash=raw["config_hash"],
            start=raw["start"],
            initial_equity=float(raw["initial_equity"]),
            last_bar=raw["last_bar"],
            equity=float(raw["equity"]),
            cash=float(raw["cash"]),
            positions=dict(raw.get("positions", {})),
            pending_orders=list(raw.get("pending_orders", [])),
            last_regime=raw.get("last_regime", ""),
            gross_target=float(raw.get("gross_target", 0.0)),
            drawdown=float(raw.get("drawdown", 0.0)),
            data_source=raw.get("data_source", ""),
            runs=list(raw.get("runs", [])),
        )

    def save(self, path: str | None = None) -> None:
        path = path or STATE_PATH
        self.updated_at = _now()
        _write_json(path, self.__dict__)

    def pending_map(self) -> dict[str, float]:
        return {str(o["asset"]): float(o["target"]) for o in self.pending_orders}


def state_exists(path: str | None = None) -> bool:
    return os.path.exists(path or STATE_PATH)


def initialize(
    cfg: dict | None = None,
    start: str | None = None,
    initial_equity: float | None = None,
    force: bool = False,
) -> PaperState:
    """Crée (ou écrase avec ``force``) le portefeuille papier.

    Par défaut, le paper démarre à la dernière séance clôturée disponible :
    capital 100 % cash, aucune position historique inventée.
    """
    cfg = cfg or load_config()
    assert_paper_only(cfg)
    if state_exists() and not force:
        raise PaperError(
            f"un état papier existe déjà ({STATE_PATH}). Utilisez --force pour l'écraser "
            "ou --status pour le consulter."
        )
    panel, source = ensure_data(cfg, quiet=True)
    last = max(df.index[-1] for df in panel.values())
    start_ts = pd.Timestamp(start, tz="UTC") if start else last.normalize()
    if start_ts > last:
        raise PaperError(f"date de départ {start_ts.date()} postérieure à la dernière bougie {last.date()}")
    state = PaperState.new(cfg, str(start_ts.date()), initial_equity or cfg.get("initial_equity", 100_000.0))
    state.data_source = source
    state.save()
    return state


def _new_fills(result: BacktestResult, after: pd.Timestamp) -> list[dict]:
    out = []
    for trade in result.trades:
        ts = pd.Timestamp(trade["ts"])
        if ts > after:
            out.append(trade)
    return out


def _reconcile(previous: PaperState, fills: list[dict]) -> list[str]:
    """Compare les intentions de la séance précédente aux fills observés.

    Les stops et liquidations risk-off peuvent produire des fills hors
    intentions de rééquilibrage : ce ne sont pas des anomalies. En revanche,
    un fill de rééquilibrage sur un actif qui n'était pas dans les intentions
    est signalé.
    """
    notes: list[str] = []
    intended = previous.pending_map()
    rebalance_fills = [
        f
        for f in fills
        if "stop" not in str(f.get("reason", "")).lower()
        and "liquidation" not in str(f.get("reason", "")).lower()
        and "cash" not in str(f.get("reason", "")).lower()
    ]
    for fill in rebalance_fills:
        asset = fill["asset"]
        if asset not in intended:
            notes.append(f"fill hors intention sur {asset}: {fill.get('reason', '')}")
    for asset, target in intended.items():
        if target <= 1e-9:
            sold = [f for f in rebalance_fills if f["asset"] == asset and f["side"] == "SELL"]
            if not sold:
                notes.append(f"{asset}: sortie demandée mais aucun fill de vente observé")
        else:
            bought = [f for f in rebalance_fills if f["asset"] == asset and f["side"] == "BUY"]
            if not bought:
                notes.append(f"{asset}: entrée/renfort demandé mais aucun achat observé")
    return notes


def run_paper(
    cfg: dict | None = None,
    quiet: bool = False,
    refresh: bool = True,
) -> dict:
    """Exécute une séance de paper trading et met à jour l'état persistant."""
    cfg = cfg or load_config()
    assert_paper_only(cfg)
    if not state_exists():
        raise PaperError("aucun état papier : lancez d'abord `python -m meridian paper --init`.")
    state = PaperState.load()
    if state.config_hash != config_hash(cfg):
        raise PaperError(
            "la configuration a changé depuis l'initialisation du paper. "
            "Le mandat ne doit pas dériver en silence : archivez l'état puis "
            "réinitialisez avec --init --force, ou revenez à la configuration d'origine."
        )

    if refresh:
        panel, source = ensure_data(cfg, quiet=True)
    else:
        from .data import load_panel

        panel, source = load_panel(cfg), "cache"
    quality = quality_report(cfg, panel)
    store = FeatureStore.build(panel, cfg)
    engine = Backtest(store, cfg)
    result = engine.run(start=state.start, initial_equity=state.initial_equity)
    if result.end < state.last_bar:
        raise PaperError(
            f"les données régressent ({result.end} < {state.last_bar}) : cache incohérent."
        )

    previous_last = pd.Timestamp(state.last_bar, tz="UTC")
    new_fills = _new_fills(result, previous_last)
    first_run = not state.runs
    # La réconciliation ne concerne que la première séance nouvelle : les
    # rééquilibrages des séances suivantes ont leurs propres intentions.
    if new_fills:
        first_new_day = pd.Timestamp(new_fills[0]["ts"]).normalize()
        recon_fills = [
            f for f in new_fills if pd.Timestamp(f["ts"]).normalize() == first_new_day
        ]
    else:
        recon_fills = []
    reconciliation = _reconcile(state, recon_fills) if (recon_fills and not first_run) else []

    decision = result.latest_decision
    governor = result.governor
    supervisor = SupervisorAgent(cfg)
    memo = ""
    if decision is not None and governor is not None:
        memo = supervisor.memo(decision, result.book, governor, result.latest_execution, quality)
        supervisor.write(MEMO_PATH, memo)

    pending = (
        [
            {"asset": order.asset, "target": float(order.target_weight), "reason": order.reason}
            for order in decision.orders
        ]
        if decision is not None
        else []
    )
    snapshot = result.book.to_dict()
    entry = {
        "run_at": _now(),
        "data_source": source,
        "start": state.start,
        "last_bar": result.end,
        "equity": round(result.equity.iloc[-1], 2),
        "cash": round(snapshot["cash"], 2),
        "gross": round(result.gross.iloc[-1], 4),
        "regime": decision.regime.label if decision else "",
        "new_fills": len(new_fills),
        "pending_orders": len(pending),
        "reconciliation": reconciliation,
    }

    state.updated_at = _now()
    state.last_bar = result.end
    state.equity = float(result.equity.iloc[-1])
    state.cash = float(snapshot["cash"])
    state.positions = snapshot["positions"]
    state.pending_orders = pending
    state.last_regime = decision.regime.label if decision else ""
    state.gross_target = float(sum(p["target"] for p in pending)) if pending else 0.0
    state.drawdown = float(governor.drawdown) if governor else 0.0
    state.data_source = source
    state.runs = (state.runs + [entry])[-120:]
    state.save()

    snapshot = {
        "version": 1,
        "run_at": entry["run_at"],
        "config_hash": state.config_hash,
        "start": state.start,
        "last_bar": result.end,
        "equity": state.equity,
        "regime": decision.regime.as_dict() if decision else {},
        "signals": [s.as_dict() for s in decision.signals] if decision else [],
        "targets": [t.as_dict() for t in decision.targets] if decision else [],
        "orders": [o.__dict__ for o in decision.orders] if decision else [],
        "portfolio_vol": float(decision.portfolio_vol) if decision else 0.0,
    }
    _write_json(SNAPSHOT_PATH, snapshot)

    os.makedirs(PAPER_DIR, exist_ok=True)
    with open(JOURNAL_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    if result.trades:
        pd.DataFrame(result.trades).to_csv(FILLS_PATH, index=False)

    summary = {
        **entry,
        "initial_equity": state.initial_equity,
        "total_return": state.equity / state.initial_equity - 1.0,
        "drawdown": state.drawdown,
        "positions": state.positions,
        "pending": state.pending_orders,
        "reconciliation": reconciliation,
        "memo_path": MEMO_PATH if memo else None,
        "fills": new_fills,
        "total_fills": len(result.trades),
    }
    if not quiet:
        print(format_paper_summary(summary))
    return summary


def format_paper_summary(summary: dict) -> str:
    def pct(value: float) -> str:
        return f"{value * 100:+.2f}%"

    lines = [
        "=" * 74,
        f"MERIDIAN PAPER — séance {summary['last_bar']} · régime {summary.get('regime', 'n/a')}",
        "=" * 74,
        f"Equity            : {summary['equity']:,.2f} $".replace(",", " "),
        f"Rendement total   : {pct(summary['total_return'])}",
        f"Drawdown          : {pct(summary['drawdown'])}",
        f"Cash              : {summary['cash']:,.2f} $".replace(",", " "),
        f"Exposition brute  : {summary['gross'] * 100:.1f}%",
        f"Source données    : {summary['data_source']}",
        f"Nouveaux fills    : {summary['new_fills']} (total {summary['total_fills']})",
    ]
    if summary["positions"]:
        lines.append("")
        lines.append("Positions :")
        for asset, pos in sorted(summary["positions"].items()):
            lines.append(
                f"  {asset:<9} qty {pos['qty']:.6f} · PRU {pos['avg_price']:,.2f} · "
                f"stop {pos['stop']:,.2f} · {pos.get('sleeve', '—')}"
            )
    if summary["pending"]:
        lines.append("")
        lines.append("Intentions pour la prochaine ouverture :")
        for order in summary["pending"]:
            lines.append(f"  {order['asset']:<9} cible {order['target'] * 100:5.1f}% · {order['reason'][:70]}")
    if summary.get("reconciliation"):
        lines.append("")
        lines.append("Réconciliation :")
        for note in summary["reconciliation"]:
            lines.append(f"  ! {note}")
    if summary.get("memo_path"):
        lines.append("")
        lines.append(f"Memo : {summary['memo_path']}")
    lines.append("")
    lines.append("Aucun ordre réel n'a été envoyé.")
    return "\n".join(lines)


def verify(cfg: dict | None = None, refresh: bool = True) -> dict:
    """Audite l'état papier contre le replay déterministe, sans le modifier.

    Retourne ``ok=False`` si l'equity, le cash, les positions ou les intentions
    divergent au-delà des tolérances. Une nouvelle bougie disponible n'est pas
    une divergence : c'est une séance à traiter, signalée séparément.
    """
    cfg = cfg or load_config()
    assert_paper_only(cfg)
    if not state_exists():
        raise PaperError("aucun état papier : lancez d'abord `python -m meridian paper --init`.")
    state = PaperState.load()
    if refresh:
        panel, source = ensure_data(cfg, quiet=True)
    else:
        from .data import load_panel

        panel, source = load_panel(cfg), "cache"
    store = FeatureStore.build(panel, cfg)
    result = Backtest(store, cfg).run(start=state.start, initial_equity=state.initial_equity)
    replay_last = str(result.equity.index[-1].date())
    replay_equity = float(result.equity.iloc[-1])
    snapshot = result.book.to_dict()
    issues: list[str] = []
    warnings: list[str] = []

    if state.config_hash != config_hash(cfg):
        issues.append("la configuration a changé depuis l'initialisation du paper")
    if result.end < state.last_bar:
        issues.append(f"les données régressent ({result.end} < {state.last_bar})")
    if abs(state.equity - replay_equity) > 0.01:
        issues.append(
            f"equity divergente : état {state.equity:,.2f} $ vs replay {replay_equity:,.2f} $".replace(",", " ")
        )
    if abs(state.cash - float(snapshot["cash"])) > 0.01:
        issues.append(
            f"cash divergent : état {state.cash:,.2f} $ vs replay {snapshot['cash']:,.2f} $".replace(",", " ")
        )

    state_pos = state.positions
    replay_pos = snapshot["positions"]
    for asset in sorted(set(state_pos) | set(replay_pos)):
        left = state_pos.get(asset)
        right = replay_pos.get(asset)
        if left is None or right is None:
            issues.append(f"position {asset} présente d'un seul côté (état/replay)")
            continue
        if abs(float(left.get("qty", 0.0)) - float(right.get("qty", 0.0))) > 1e-9:
            issues.append(f"quantité {asset} divergente : état vs replay")
        if abs(float(left.get("avg_price", 0.0)) - float(right.get("avg_price", 0.0))) > 0.01:
            issues.append(f"prix moyen {asset} divergent : état vs replay")
        if abs(float(left.get("stop", 0.0)) - float(right.get("stop", 0.0))) > 0.01:
            issues.append(f"stop {asset} divergent : état vs replay")

    replay_pending = {
        str(order.asset): float(order.target_weight)
        for order in (result.latest_decision.orders if result.latest_decision else [])
    }
    stored_pending = state.pending_map()
    if result.end > state.last_bar:
        warnings.append(
            f"nouvelle bougie disponible ({state.last_bar} → {result.end}) : "
            "les intentions ont avancé, relancez `paper` pour les adopter."
        )
    else:
        for asset in sorted(set(stored_pending) | set(replay_pending)):
            left = stored_pending.get(asset, 0.0)
            right = replay_pending.get(asset, 0.0)
            if abs(left - right) > 1e-6:
                issues.append(
                    f"intention {asset} divergente : état {left:.2%} vs replay {right:.2%}"
                )

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "state": {
            "start": state.start,
            "last_bar": state.last_bar,
            "equity": state.equity,
            "cash": state.cash,
            "positions": len(state_pos),
            "pending_orders": len(state.pending_orders),
        },
        "replay": {
            "start": state.start,
            "last_bar": replay_last,
            "equity": replay_equity,
            "cash": float(snapshot["cash"]),
            "positions": len(replay_pos),
            "pending_orders": len(replay_pending),
            "trades": len(result.trades),
        },
        "data_source": source,
    }


def format_verification(payload: dict) -> str:
    verdict = "OK" if payload["ok"] else "DIVERGENCE"
    lines = [
        "=" * 66,
        f"MERIDIAN PAPER — vérification · {verdict}",
        "=" * 66,
        f"État   : {payload['state']['start']} → {payload['state']['last_bar']} · "
        f"equity {payload['state']['equity']:,.2f} $ · {payload['state']['positions']} position(s)".replace(",", " "),
        f"Replay : {payload['replay']['start']} → "
        f"{payload['replay']['last_bar']} · equity {payload['replay']['equity']:,.2f} $ · "
        f"{payload['replay']['positions']} position(s) · {payload['replay']['trades']} fill(s)".replace(",", " "),
    ]
    if payload["issues"]:
        lines.append("")
        lines.append("Divergences :")
        for issue in payload["issues"]:
            lines.append(f"  ! {issue}")
    if payload["warnings"]:
        lines.append("")
        lines.append("Avertissements :")
        for warning in payload["warnings"]:
            lines.append(f"  ~ {warning}")
    if payload["ok"]:
        lines.append("")
        lines.append("État papier et replay cohérents.")
    lines.append("Aucun ordre réel n'a été envoyé.")
    return "\n".join(lines)


def status() -> dict:
    """État papier + cohérence des données, sans exécuter de séance."""
    cfg = load_config()
    out: dict = {"state_exists": state_exists(), "config_hash": config_hash(cfg)}
    if state_exists():
        state = PaperState.load()
        out.update(
            {
                "start": state.start,
                "last_bar": state.last_bar,
                "equity": state.equity,
                "cash": state.cash,
                "drawdown": state.drawdown,
                "regime": state.last_regime,
                "positions": state.positions,
                "pending_orders": state.pending_orders,
                "config_match": state.config_hash == out["config_hash"],
                "updated_at": state.updated_at,
                "runs": len(state.runs),
            }
        )
    try:
        panel, source = ensure_data(cfg, quiet=True)
        quality = quality_report(cfg, panel)
        last = max(df.index[-1] for df in panel.values())
        out["data"] = {
            "source": source,
            "last_bar": str(last.date()),
            "stale_days": int((pd.Timestamp.now(tz="UTC") - last).days),
            "quality": quality,
        }
    except Exception as exc:  # état dégradé mais lisible
        out["data"] = {"error": str(exc)}
    return out


def format_status(payload: dict) -> str:
    lines = ["MERIDIAN — statut du desk", "=" * 50]
    data = payload.get("data", {})
    if "error" in data:
        lines.append(f"Données : ERREUR — {data['error']}")
    else:
        lines.append(
            f"Données : {data.get('source')} · dernière bougie {data.get('last_bar')} "
            f"(retard {data.get('stale_days')} j)"
        )
        for symbol, row in data.get("quality", {}).items():
            lines.append(
                f"  {symbol:<9} {row.get('rows', 0):>5} bougies · {row.get('status')} · "
                f"ADV20 {row.get('adv20_usd', 0) / 1e6:,.0f} M$".replace(",", " ")
            )
    lines.append("")
    if not payload.get("state_exists"):
        lines.append("Paper : non initialisé (`python -m meridian paper --init`).")
        return "\n".join(lines)
    lines.append(f"Paper : {payload['start']} → {payload['last_bar']} · régime {payload.get('regime')}")
    equity_txt = f"{payload['equity']:,.0f}".replace(",", " ")
    cash_txt = f"{payload['cash']:,.0f}".replace(",", " ")
    dd_txt = f"{payload['drawdown'] * 100:+.1f}".replace(".", ",")
    lines.append(
        f"  Equity {equity_txt} $ · cash {cash_txt} $ · "
        f"drawdown {dd_txt}%"
    )
    lines.append(f"  Config inchangée : {'oui' if payload.get('config_match') else 'NON'}")
    if payload.get("positions"):
        lines.append("  Positions :")
        for asset, pos in sorted(payload["positions"].items()):
            lines.append(f"    {asset:<9} {pos['qty']:.6f} @ {pos['avg_price']:,.2f} stop {pos['stop']:,.2f}")
    if payload.get("pending_orders"):
        lines.append("  Intentions en attente :")
        for order in payload["pending_orders"]:
            lines.append(f"    {order['asset']:<9} {order['target'] * 100:5.1f}%")
    return "\n".join(lines)
