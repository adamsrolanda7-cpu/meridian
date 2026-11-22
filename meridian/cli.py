"""Interface en ligne de commande du desk Meridian.

Commandes :
    download   télécharge et contrôle les données Binance
    backtest   rejoue la stratégie et produit un rapport
    research   IS/OOS, walk-forward, Monte Carlo, stress de coûts
    paper      initialise, exécute ou consulte le portefeuille papier
    monitor    suivi opérationnel, rolling Sharpe et kill criteria
    live       tableau de bord terminal temps réel, sans web
    report     reconstruit le rapport à partir de la configuration courante
    status     état des données et du paper trading
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from .backtest import Backtest
from .config import ROOT, deep_merge, dump_config, load_config
from .data import DataError, download_all, ensure_data, load_panel, quality_report
from .features import FeatureStore
from .metrics import format_metrics
from .paper import (
    PaperError,
    format_paper_summary,
    format_status,
    format_verification,
    initialize,
    run_paper,
    state_exists,
    status as paper_status,
    verify as paper_verify,
)
from .report import build_report
from .research import run_research, summarize
from . import monitor as monitor_mod
from . import live as live_mod

RESEARCH_JSON = os.path.join(ROOT, "runs", "research.json")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _load_cfg(args) -> dict:
    overrides = None
    if getattr(args, "config", None):
        with open(args.config, "r", encoding="utf-8") as fh:
            overrides = json.load(fh)
    return load_config(overrides=overrides)


def _read_research() -> dict | None:
    if not os.path.exists(RESEARCH_JSON):
        return None
    try:
        with open(RESEARCH_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _print_quality(quality: dict) -> None:
    print(f"{'Actif':<10}{'Bougies':>8}  {'Période':<25}{'Statut':<8}{'ADV20':>12}")
    for symbol, row in quality.items():
        adv = row.get("adv20_usd", 0) / 1e6
        print(
            f"{symbol:<10}{row.get('rows', 0):>8}  "
            f"{row.get('first', '')} → {row.get('last', ''):<12}"
            f"{row.get('status', ''):<8}{adv:>9,.0f} M$".replace(",", " ")
        )


# --------------------------------------------------------------------- commands
def cmd_download(args) -> int:
    cfg = _load_cfg(args)
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    panel = download_all(cfg, symbols=symbols, start=args.start, force=args.force)
    quality = quality_report(cfg, panel)
    _print_quality(quality)
    bad = [s for s, q in quality.items() if q.get("status") != "OK"]
    if bad:
        print(f"\nATTENTION: qualité à vérifier pour {', '.join(bad)}")
    return 0


def _run_backtest(cfg: dict, args, research: dict | None = None) -> tuple:
    panel, source = ensure_data(cfg, quiet=False)
    quality = quality_report(cfg, panel)
    print(f"[data] source={source} · {len(panel)} actifs")
    store = FeatureStore.build(panel, cfg)
    result = Backtest(store, cfg).run(
        start=getattr(args, "start", None),
        end=getattr(args, "end", None),
        initial_equity=getattr(args, "initial_equity", None),
        cost_multiplier=getattr(args, "cost_multiplier", 1.0),
    )
    print(format_metrics(result.metrics))
    tag = getattr(args, "tag", None) or _stamp()
    if not getattr(args, "no_report", False):
        path = build_report(result, cfg, quality=quality, research=research, tag=tag)
        print(f"\nRapport  : {path}")
        print(f"Dernier  : {os.path.join(ROOT, 'reports', 'latest.html')}")
        print(f"Memo     : {os.path.join(ROOT, 'runs', tag, 'memo.md')}")
    return result, quality


def cmd_backtest(args) -> int:
    cfg = _load_cfg(args)
    _run_backtest(cfg, args, research=_read_research())
    return 0


def cmd_report(args) -> int:
    cfg = _load_cfg(args)
    research = _read_research()
    _run_backtest(cfg, args, research=research)
    return 0


def cmd_research(args) -> int:
    cfg = _load_cfg(args)
    report = run_research(cfg, quick=args.quick, paths=args.paths)
    print(summarize(report))

    payload = report.to_dict()
    selected = deep_merge(
        cfg,
        {
            "strategy": {
                "target_vol": payload["selected"]["params"]["strategy.target_vol"],
                "rebalance_days": payload["selected"]["params"]["strategy.rebalance_days"],
            },
            "risk": {
                "stop_atr": payload["selected"]["params"]["risk.stop_atr"],
                "trail_atr": payload["selected"]["params"]["risk.stop_atr"],
            },
        },
    )
    selected_path = os.path.join(ROOT, "runs", "selected_config.json")
    dump_config(selected, selected_path)
    print(f"\nConfig sélectionnée (IS) : {selected_path}")
    if payload["selected"].get("mode") == "frozen":
        print("Mandat figé : ce fichier reprend le mandat publié, la grille n'a pas sélectionné à sa place.")
    print("config/default.json n'est PAS modifié automatiquement : adopter un mandat est une décision humaine.")

    if not args.no_report:
        panel, source = ensure_data(cfg, quiet=True)
        quality = quality_report(cfg, panel)
        store = FeatureStore.build(panel, selected)
        result = Backtest(store, selected).run()
        tag = args.tag or f"research-{_stamp()}"
        path = build_report(result, selected, quality=quality, research=payload, tag=tag)
        print(f"Rapport recherche : {path}")
        print(f"Dernier rapport   : {os.path.join(ROOT, 'reports', 'latest.html')}")
    return 0


def cmd_paper(args) -> int:
    cfg = _load_cfg(args)
    try:
        if args.init:
            state = initialize(
                cfg,
                start=args.start,
                initial_equity=args.initial_equity,
                force=args.force,
            )
            if not args.json:
                print(
                    f"Paper initialisé : {state.start} · capital {state.initial_equity:,.2f} $ "
                    f"· état {os.path.join(ROOT, 'runs', 'paper', 'state.json')}".replace(",", " ")
                )
            # Première séance : produit immédiatement les intentions pour la
            # prochaine ouverture, sans exécuter de position rétroactive.
            summary = run_paper(cfg=cfg, quiet=args.json, refresh=not args.no_refresh)
            if args.json:
                print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
            return 0
        if args.status:
            payload = paper_status()
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(format_status(payload))
            return 0
        if getattr(args, "verify", False):
            payload = paper_verify(cfg, refresh=not args.no_refresh)
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
            else:
                print(format_verification(payload))
            return 0 if payload["ok"] else 2
        if not state_exists():
            raise PaperError(
                "aucun état papier : lancez `python -m meridian paper --init` "
                "(ou `--init --start AAAA-MM-JJ --initial-equity 100000`)."
            )
        summary = run_paper(cfg=cfg, quiet=args.json, refresh=not args.no_refresh)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        return 0
    except PaperError as exc:
        print(f"ERREUR paper : {exc}", file=sys.stderr)
        return 2


def cmd_monitor(args) -> int:
    cfg = _load_cfg(args)
    payload = monitor_mod.evaluate(cfg, refresh=not args.no_refresh)
    if not args.no_write:
        json_path, md_path = monitor_mod.write(payload)
        if not args.json:
            print(f"Suivi écrit : {json_path}")
            print(f"             {md_path}")
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(monitor_mod.format_monitor(payload))
    return 0 if payload.get("status") != "ALERT" else 2


def cmd_status(args) -> int:
    payload = paper_status()
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_status(payload))
        if payload.get("state_exists") and not payload.get("config_match"):
            print("\n! La configuration a changé depuis l'initialisation du paper.")
    return 0


def cmd_live(args) -> int:
    cfg = _load_cfg(args)
    return live_mod.run(
        cfg,
        interval=args.interval,
        snapshot_interval=args.snapshot_interval,
        monitor_interval=args.monitor_interval,
        auto_run=not args.no_auto_run,
        fetch=args.fetch,
        fetch_interval=args.fetch_interval,
        write_monitor=not args.no_write_monitor,
        plain=args.plain,
        follow=args.follow,
        window=args.window,
        config_path=args.config,
        offline=args.offline,
    )


# ------------------------------------------------------------------------ parser
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meridian",
        description="Meridian — desk crypto multi-actifs, long-only spot, paper trading.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, with_window: bool = True) -> None:
        p.add_argument("--config", help="fichier JSON de surcharge de la configuration")
        p.add_argument("--tag", help="nom du dossier de sortie dans runs/")
        if with_window:
            p.add_argument("--start", help="date de début (AAAA-MM-JJ)")
            p.add_argument("--end", help="date de fin (AAAA-MM-JJ)")
            p.add_argument("--initial-equity", type=float, help="capital initial")
            p.add_argument("--cost-multiplier", type=float, default=1.0, help="stress de coûts")
            p.add_argument("--no-report", action="store_true", help="ne pas générer le rapport HTML")

    p_dl = sub.add_parser("download", help="télécharger les données Binance")
    add_common(p_dl, with_window=False)
    p_dl.add_argument("--start", default=None, help="date de départ (défaut: config)")
    p_dl.add_argument("--symbols", help="liste séparée par des virgules")
    p_dl.add_argument("--force", action="store_true", help="forcer le téléchargement complet")
    p_dl.set_defaults(func=cmd_download)

    p_bt = sub.add_parser("backtest", help="backtest complet + rapport")
    add_common(p_bt)
    p_bt.set_defaults(func=cmd_backtest)

    p_rs = sub.add_parser("research", help="validation IS/OOS et robustesse")
    add_common(p_rs, with_window=False)
    p_rs.add_argument("--paths", type=int, default=800, help="tirages Monte Carlo")
    p_rs.add_argument("--quick", action="store_true", help="réduire les chemins Monte Carlo")
    p_rs.add_argument("--no-report", action="store_true", help="ne pas générer le rapport HTML")
    p_rs.set_defaults(func=cmd_research)

    p_pa = sub.add_parser("paper", help="paper trading persistant")
    p_pa.add_argument("--config", help="fichier JSON de surcharge")
    p_pa.add_argument("--init", action="store_true", help="initialiser le portefeuille papier")
    p_pa.add_argument("--force", action="store_true", help="écraser l'état existant à l'init")
    p_pa.add_argument("--start", help="date de départ du paper (défaut: dernière séance)")
    p_pa.add_argument("--initial-equity", type=float, help="capital papier")
    p_pa.add_argument("--status", action="store_true", help="afficher l'état sans exécuter")
    p_pa.add_argument("--verify", action="store_true", help="auditer l'état papier contre le replay")
    p_pa.add_argument("--no-refresh", action="store_true", help="ne pas tenter de télécharger")
    p_pa.add_argument("--json", action="store_true", help="sortie JSON")
    p_pa.set_defaults(func=cmd_paper)

    p_mon = sub.add_parser("monitor", help="suivi opérationnel et kill criteria")
    p_mon.add_argument("--config", help="fichier JSON de surcharge")
    p_mon.add_argument("--no-refresh", action="store_true", help="ne pas tenter de télécharger")
    p_mon.add_argument("--no-write", action="store_true", help="ne pas écrire runs/monitor.*")
    p_mon.add_argument("--json", action="store_true", help="sortie JSON")
    p_mon.set_defaults(func=cmd_monitor)

    p_live = sub.add_parser("live", help="tableau de bord terminal temps réel")
    p_live.add_argument("--config", help="fichier JSON de surcharge")
    p_live.add_argument("--interval", type=float, default=5.0, help="rafraîchissement de l'écran (s)")
    p_live.add_argument("--snapshot-interval", type=float, default=30.0, help="recalcul du snapshot (s)")
    p_live.add_argument("--monitor-interval", type=float, default=300.0, help="recalcul du monitor (s)")
    p_live.add_argument("--fetch", action="store_true", help="télécharger aussi les données en boucle")
    p_live.add_argument("--fetch-interval", type=float, default=300.0, help="intervalle de téléchargement (s)")
    p_live.add_argument("--no-auto-run", action="store_true", help="ne pas exécuter le paper sur nouvelle bougie")
    p_live.add_argument("--no-write-monitor", action="store_true", help="ne pas écrire runs/monitor.*")
    p_live.add_argument("--plain", action="store_true", help="afficher une trame texte et quitter")
    p_live.add_argument("--follow", action="store_true", help="mode texte continu, sans curses")
    p_live.add_argument("--window", action="store_true", help="ouvrir le dashboard dans une nouvelle fenêtre")
    p_live.add_argument("--offline", action="store_true", help="ne rien télécharger ; lire uniquement le cache local")
    p_live.set_defaults(func=cmd_live)

    p_rep = sub.add_parser("report", help="reconstruire le rapport HTML")
    add_common(p_rep)
    p_rep.set_defaults(func=cmd_report)

    p_st = sub.add_parser("status", help="état des données et du paper")
    p_st.add_argument("--json", action="store_true", help="sortie JSON")
    p_st.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except (DataError, PaperError, ValueError, FileNotFoundError) as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
