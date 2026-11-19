"""Rapport HTML autonome + exports CSV/JSON + memo de séance.

Aucune dépendance de plotting : les courbes sont générées en SVG inline.
Le rapport est lisible hors ligne et peut être envoyé à un comité d'investissement.
"""
from __future__ import annotations

import html
import json
import math
import os
import shutil
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .agents.supervisor import SupervisorAgent
from .config import ROOT, get
from .metrics import monthly_returns
from .types import Decision

RUNS = os.path.join(ROOT, "runs")
REPORTS = os.path.join(ROOT, "reports")


def _pct(value, digits: int = 1, signed: bool = False) -> str:
    if value is None or not isinstance(value, (int, float, np.floating)) or not np.isfinite(value):
        return "n/a"
    pattern = f"{{:+.{digits}f}}%" if signed else f"{{:.{digits}f}}%"
    return pattern.format(value * 100).replace(".", ",")


def _money(value, digits: int = 0) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:,.{digits}f} $".replace(",", " ").replace(".", ",")


def _num(value, digits: int = 2) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value:.{digits}f}".replace(".", ",")


def _md_to_html(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    in_table = False
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":"} for c in cells):
                continue
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append(
                    "<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr></thead><tbody>"
                )
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            out.append("</tbody></table>")
            in_table = False
        if stripped.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(stripped[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if stripped.startswith("### "):
            out.append(f"<h4>{_inline(stripped[4:])}</h4>")
        elif stripped.startswith("## "):
            out.append(f"<h3>{_inline(stripped[3:])}</h3>")
        elif stripped.startswith("# "):
            out.append(f"<h2>{_inline(stripped[2:])}</h2>")
        elif stripped == "---":
            out.append("<hr>")
        elif stripped:
            out.append(f"<p>{_inline(stripped)}</p>")
    if in_table:
        out.append("</tbody></table>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(text: str) -> str:
    escaped = html.escape(text)
    parts = escaped.split("`")
    for i in range(1, len(parts), 2):
        parts[i] = f"<code>{parts[i]}</code>"
    escaped = "".join(parts)
    escaped = escaped.replace("**", "§")  # bascule simple
    chunks = escaped.split("§")
    for i in range(1, len(chunks), 2):
        chunks[i] = f"<strong>{chunks[i]}</strong>"
    return "".join(chunks)


def _svg_equity(equity: pd.Series, benchmark: pd.Series) -> str:
    width, height = 1080, 340
    left, right, top, bottom = 74, 24, 22, 38
    plot_w, plot_h = width - left - right, height - top - bottom
    eq = equity.dropna()
    bench = benchmark.reindex(eq.index).dropna()
    values = np.concatenate([eq.values, bench.values]) if len(bench) else eq.values
    if len(eq) < 2:
        return "<p>Pas assez de données.</p>"
    log = values.min() > 0
    transformed = np.log10(values) if log else values
    vmin, vmax = float(transformed.min()), float(transformed.max())
    if vmax - vmin < 1e-9:
        vmax = vmin + 1
    pad = (vmax - vmin) * 0.08
    vmin -= pad
    vmax += pad

    def x(i: int, n: int) -> float:
        return left + (i / max(1, n - 1)) * plot_w

    def y(v: float) -> float:
        t = np.log10(v) if log else v
        return top + plot_h - ((t - vmin) / (vmax - vmin)) * plot_h

    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x(i, len(eq)):.1f},{y(v):.1f}" for i, v in enumerate(eq.values)
    )
    bpath = (
        " ".join(
            f"{'M' if i == 0 else 'L'}{x(i, len(bench)):.1f},{y(v):.1f}" for i, v in enumerate(bench.values)
        )
        if len(bench)
        else ""
    )
    grid = []
    for k in range(5):
        value = vmin + (vmax - vmin) * k / 4
        real = 10**value if log else value
        yy = top + plot_h - (value - vmin) / (vmax - vmin) * plot_h
        grid.append(
            f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#22304a" stroke-width="1"/>'
            f'<text x="{left-10}" y="{yy+4:.1f}" fill="#7f8ea8" font-size="11" text-anchor="end">{_money(real)}</text>'
        )
    xticks = []
    n = len(eq)
    for k in range(6):
        i = min(n - 1, int(k * (n - 1) / 5))
        xx = x(i, n)
        xticks.append(
            f'<line x1="{xx:.1f}" y1="{top}" x2="{xx:.1f}" y2="{top+plot_h}" stroke="#1b273d" stroke-width="1"/>'
            f'<text x="{xx:.1f}" y="{height-14}" fill="#7f8ea8" font-size="11" text-anchor="middle">{eq.index[i].strftime("%Y-%m")}</text>'
        )
    return f"""
<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Courbe d'equity">
  <defs>
    <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#d4af37" stop-opacity="0.28"/>
      <stop offset="100%" stop-color="#d4af37" stop-opacity="0.01"/>
    </linearGradient>
  </defs>
  {''.join(grid)}{''.join(xticks)}
  <path d="{path} L{left+plot_w:.1f},{top+plot_h:.1f} L{left:.1f},{top+plot_h:.1f} Z" fill="url(#eqfill)" stroke="none"/>
  <path d="{bpath}" fill="none" stroke="#5d6b82" stroke-width="1.4" stroke-dasharray="5 5"/>
  <path d="{path}" fill="none" stroke="#d4af37" stroke-width="2.2"/>
  <circle cx="{x(len(eq)-1,len(eq)):.1f}" cy="{y(eq.iloc[-1]):.1f}" r="4" fill="#d4af37"/>
  <g font-size="12" fill="#c9d4e5">
    <rect x="{left+12}" y="{top+10}" width="12" height="3" fill="#d4af37"/>
    <text x="{left+30}" y="{top+15}">Meridian</text>
    <rect x="{left+110}" y="{top+10}" width="12" height="3" fill="#5d6b82"/>
    <text x="{left+128}" y="{top+15}">BTC buy &amp; hold</text>
  </g>
</svg>"""


def _svg_drawdown(equity: pd.Series) -> str:
    width, height = 1080, 190
    left, right, top, bottom = 74, 24, 16, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    dd = equity / equity.cummax() - 1.0
    if len(dd) < 2:
        return ""
    ymin = float(min(dd.min(), -0.01))
    ymin = math.floor(ymin * 20) / 20

    def x(i: int) -> float:
        return left + i / max(1, len(dd) - 1) * plot_w

    def y(v: float) -> float:
        return top + plot_h - ((v - ymin) / (0 - ymin)) * plot_h

    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(dd.values))
    ticks = []
    for frac in (0.0, 0.5, 1.0):
        value = ymin * frac
        yy = y(value)
        ticks.append(
            f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="#22304a"/>'
            f'<text x="{left-10}" y="{yy+4:.1f}" fill="#7f8ea8" font-size="11" text-anchor="end">{value*100:.0f}%</text>'
        )
    return f"""
<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Drawdown">
  {''.join(ticks)}
  <path d="{path} L{left+plot_w:.1f},{y(0):.1f} L{left:.1f},{y(0):.1f} Z" fill="#8f2f38" fill-opacity="0.35" stroke="#d05a64" stroke-width="1.5"/>
</svg>"""


def _monthly_table(equity: pd.Series) -> str:
    monthly = monthly_returns(equity)
    if monthly.empty:
        return ""
    monthly["key"] = monthly["year"].astype(str) + "-" + monthly["month"].astype(str).str.zfill(2)
    pivot = monthly.pivot_table(index="year", columns="month", values="return", aggfunc="last")
    years = sorted(pivot.index, reverse=True)
    months = ["Jan", "Fév", "Mar", "Avr", "Mai", "Juin", "Juil", "Août", "Sep", "Oct", "Nov", "Déc"]
    rows = []
    for year in years:
        cells = []
        for month in range(1, 13):
            value = pivot.loc[year, month] if month in pivot.columns else np.nan
            if pd.isna(value):
                cells.append('<td class="muted">·</td>')
                continue
            intensity = min(0.65, abs(float(value)) / 0.15 * 0.5)
            color = f"rgba(38,128,90,{intensity:.2f})" if value > 0 else f"rgba(150,48,58,{intensity:.2f})"
            cells.append(f'<td style="background:{color}">{value*100:+.1f}</td>')
        total = equity[equity.index.year == year]
        year_return = total.iloc[-1] / equity[equity.index.year < year].iloc[-1] - 1.0 if (equity.index.year < year).any() else np.nan
        rows.append(
            f"<tr><th>{year}</th>{''.join(cells)}<td class='year'>{_pct(year_return, signed=True)}</td></tr>"
        )
    header = "".join(f"<th>{m}</th>" for m in months)
    return f"<table class='heatmap'><thead><tr><th>Année</th>{header}<th>Total</th></tr></thead><tbody>{''.join(rows)}</tbody></table>"


def _allocation_rows(result, cfg: dict) -> str:
    decision = result.latest_decision
    if decision is None:
        return ""
    equity = result.equity.iloc[-1]
    rows = []
    positions = result.book.positions
    for target in sorted(decision.targets, key=lambda t: t.weight, reverse=True):
        if target.weight <= 1e-9 and target.asset not in positions:
            continue
        pos = positions.get(target.asset)
        stop = _money(pos.stop, 2) if pos else "—"
        sleeve = pos.sleeve if pos else "—"
        bar = f'<span class="bar" style="width:{min(100, target.weight*100):.1f}%"></span>'
        rows.append(
            f"<tr><td><strong>{target.asset}</strong></td><td>{_pct(target.weight)}</td>"
            f"<td>{_money(target.weight*equity)}</td><td>{stop}</td>"
            f"<td>{html.escape(sleeve)}</td><td class='barcell'>{bar}</td></tr>"
        )
    cash = result.book.cash
    rows.append(
        f"<tr class='cashrow'><td><strong>Cash</strong></td><td>{_pct(cash/equity if equity else 0)}</td>"
        f"<td>{_money(cash)}</td><td>—</td><td>—</td><td></td></tr>"
    )
    return "".join(rows)


def _trades_table(trades: list[dict], limit: int = 25) -> str:
    rows = []
    for trade in trades[-limit:][::-1]:
        ts = str(trade.get("ts", ""))[:10]
        pnl = trade.get("pnl")
        pnl_html = f"{pnl:+,.0f}".replace(",", " ") if trade.get("side") == "SELL" else "—"
        cls = "pos" if isinstance(pnl, (int, float)) and pnl > 0 else "neg" if isinstance(pnl, (int, float)) and pnl < 0 else ""
        rows.append(
            f"<tr><td>{ts}</td><td>{trade.get('asset','')}</td>"
            f"<td class='{ 'buy' if trade.get('side')=='BUY' else 'sell'}'>{trade.get('side','')}</td>"
            f"<td>{_money(trade.get('price', 0), 2)}</td><td>{_money(trade.get('notional', 0))}</td>"
            f"<td class='{cls}'>{pnl_html}</td><td>{html.escape(str(trade.get('reason',''))[:60])}</td></tr>"
        )
    return "".join(rows)


def _research_section(research: dict | None) -> str:
    if not research:
        return ""
    sel = research.get("selected", {})
    wf = research.get("walkforward", {})
    mc = research.get("montecarlo", {})
    rows = []
    for key, label in (
        ("is", "In-sample"),
        ("oos", "OOS nouveau compte"),
        ("oos_continuous", "OOS continu"),
        ("full", "Période complète"),
    ):
        m = sel.get(key, {})
        if not m:
            continue
        turnover = m.get("turnover_x")
        turnover_txt = f"{_num(turnover, 1)}x" if turnover is not None else "n/a"
        rows.append(
            f"<tr><td>{label}</td><td>{_pct(m.get('cagr'))}</td><td>{_pct(m.get('volatility'))}</td>"
            f"<td>{_num(m.get('sharpe'))}</td><td>{_pct(m.get('max_drawdown'))}</td>"
            f"<td>{turnover_txt}</td></tr>"
        )
    cost_rows = "".join(
        f"<tr><td>×{c.get('multiplier',1):.0f}</td><td>{_pct(c.get('cagr'))}</td>"
        f"<td>{_num(c.get('sharpe'))}</td><td>{_pct(c.get('max_drawdown'))}</td></tr>"
        for c in research.get("cost_stress", [])
    )
    start_rows = "".join(
        f"<tr><td>{s.get('start')}</td><td>{_pct(s.get('cagr'))}</td>"
        f"<td>{_num(s.get('sharpe'))}</td><td>{_pct(s.get('max_drawdown'))}</td></tr>"
        for s in research.get("start_dates", [])
    )
    wf_test = wf.get("stitched", {})
    fixed = wf.get("fixed", {})
    fixed_test = fixed.get("stitched", {})
    fixed_by_start = {
        f.get("test_start"): f for f in fixed.get("folds", []) if f.get("test_start")
    }
    wf_rows = []
    for fold in wf.get("folds", []):
        fixed_fold = fixed_by_start.get(fold.get("test_start"), {})
        fixed_metrics = fixed_fold.get("test", {})
        wf_rows.append(
            f"<tr><td>{fold.get('test_start')} → {fold.get('test_end')}</td>"
            f"<td>{_pct(fold.get('test', {}).get('cagr'))}</td>"
            f"<td>{_num(fold.get('test', {}).get('sharpe'))}</td>"
            f"<td>{_pct(fixed_metrics.get('cagr'))}</td>"
            f"<td>{_num(fixed_metrics.get('sharpe'))}</td></tr>"
        )
    fixed_line = (
        f"<br><strong>Mandat fixe, sans ré-optimisation :</strong> "
        f"{fixed.get('positive_folds', 0)}/{fixed.get('total_folds', 0)} folds positifs · "
        f"CAGR {_pct(fixed_test.get('cagr'))} · Sharpe {_num(fixed_test.get('sharpe'))} · "
        f"DD {_pct(fixed_test.get('max_drawdown'))}"
        if fixed
        else ""
    )
    return f"""
<section class="card">
  <h2>Validation hors échantillon</h2>
  <p class="note">Sélection sur l'IS uniquement. L'OOS n'a pas servi à choisir les paramètres :
  il est publié tel quel. « OOS nouveau compte » suppose un capital frais au début de l'OOS ;
  « OOS continu » enchaîne les positions et mesure le drawdown depuis le plus haut historique.
  Les deux diffèrent, et aucune des deux n'est une prévision.</p>
  <table class="data">
    <thead><tr><th>Phase</th><th>CAGR</th><th>Vol</th><th>Sharpe</th><th>Max DD</th><th>Turnover</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div class="cols">
    <div>
      <h3>Walk-forward · {wf.get('total_folds',0)} fenêtres de {wf.get('test_days',365)} j</h3>
      <p>Ré-optimisé sur chaque train : {wf.get('positive_folds',0)}/{wf.get('total_folds',0)} folds positifs ·
      CAGR {_pct(wf_test.get('cagr'))} · Sharpe {_num(wf_test.get('sharpe'))} ·
      DD {_pct(wf_test.get('max_drawdown'))}{fixed_line}</p>
      <table class="data compact">
        <thead><tr><th>Test</th><th>Optim. CAGR</th><th>Optim. Sharpe</th><th>Fixe CAGR</th><th>Fixe Sharpe</th></tr></thead>
        <tbody>{''.join(wf_rows)}</tbody>
      </table>
    </div>
    <div>
      <h3>Monte Carlo · {mc.get('paths',0)} tirages</h3>
      <p>CAGR p5 / p50 / p95 : <strong>{_pct(mc.get('cagr_p5'))} / {_pct(mc.get('cagr_p50'))} / {_pct(mc.get('cagr_p95'))}</strong><br>
      Max DD p5 : {_pct(mc.get('maxdd_p5'))} · P(perte) : {_pct(mc.get('prob_negative'),0)}<br>
      P(DD &lt; -20%) : {_pct(mc.get('prob_dd_worse_20'),0)}</p>
      <h3>Stress de coûts</h3>
      <table class="data compact"><thead><tr><th>Multiplicateur</th><th>CAGR</th><th>Sharpe</th><th>DD</th></tr></thead>
      <tbody>{cost_rows}</tbody></table>
    </div>
  </div>
  <h3>Robustesse au point de départ</h3>
  <table class="data compact"><thead><tr><th>Départ</th><th>CAGR</th><th>Sharpe</th><th>DD</th></tr></thead>
  <tbody>{start_rows}</tbody></table>
</section>"""


def build_report(
    result,
    cfg: dict,
    quality: dict | None = None,
    research: dict | None = None,
    output_dir: str | None = None,
    tag: str | None = None,
) -> str:
    tag = tag or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = output_dir or os.path.join(RUNS, tag)
    os.makedirs(out_dir, exist_ok=True)
    metrics = result.metrics
    benchmark = metrics.get("benchmark", {})
    supervisor = SupervisorAgent(cfg)
    memo = (
        supervisor.memo(
            result.latest_decision,
            result.book,
            result.governor,
            result.latest_execution,
            quality,
        )
        if result.latest_decision and result.governor
        else "# Memo indisponible"
    )
    with open(os.path.join(out_dir, "memo.md"), "w", encoding="utf-8") as fh:
        fh.write(memo)
    with open(os.path.join(out_dir, "metrics.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2, ensure_ascii=False)
    result.equity.to_csv(os.path.join(out_dir, "equity.csv"), header=True)
    pd.DataFrame(result.trades).to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    with open(os.path.join(out_dir, "decisions.json"), "w", encoding="utf-8") as fh:
        json.dump(result.decisions, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "latest_decision.json"), "w", encoding="utf-8") as fh:
        json.dump(result.latest_decision.as_dict() if result.latest_decision else {}, fh, indent=2, ensure_ascii=False)
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)

    macro = get(cfg, "strategy.target_vol", 0.30)
    bench_name = "BTC buy & hold"
    quality_rows = "".join(
        f"<tr><td>{s}</td><td>{q.get('rows',0)}</td><td>{q.get('first','')} → {q.get('last','')}</td>"
        f"<td class='{ 'ok' if q.get('status')=='OK' else 'warn'}'>{q.get('status','')}</td>"
        f"<td>{_money(q.get('adv20_usd',0))}</td></tr>"
        for s, q in (quality or {}).items()
    )
    kpis = [
        ("CAGR", _pct(metrics.get("cagr"), signed=True), f"BTC {_pct(benchmark.get('cagr'), signed=True)}"),
        ("Volatilité", _pct(metrics.get("volatility")), f"BTC {_pct(benchmark.get('volatility'))}"),
        ("Sharpe", _num(metrics.get("sharpe")), f"BTC {_num(benchmark.get('sharpe'))}"),
        ("Max drawdown", _pct(metrics.get("max_drawdown")), f"BTC {_pct(benchmark.get('max_drawdown'))}"),
        ("Exposition moyenne", _pct(metrics.get("avg_gross_exposure")), f"Cible vol {_pct(macro)}"),
        ("Hit rate", _pct(metrics.get("hit_rate")), f"{metrics.get('round_trips',0)} trades clos"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><span>{label}</span><strong>{value}</strong><small>{sub}</small></div>'
        for label, value, sub in kpis
    )
    report_html = f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meridian — {result.start} → {result.end}</title>
<style>
:root {{ color-scheme: dark; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:#0b1220; color:#dbe4f0; font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; padding:32px 22px 64px; }}
header {{ border-bottom:1px solid #24314a; padding-bottom:20px; margin-bottom:24px; display:flex; justify-content:space-between; gap:20px; align-items:flex-end; flex-wrap:wrap; }}
.brand {{ font-size:13px; letter-spacing:.22em; color:#d4af37; text-transform:uppercase; }}
h1 {{ font-size:30px; margin:6px 0 4px; letter-spacing:-.02em; }}
h2 {{ font-size:19px; margin:0 0 12px; color:#f0d98a; }}
h3 {{ font-size:15px; margin:20px 0 8px; color:#c9d4e5; }}
h4 {{ font-size:14px; color:#c9d4e5; }}
p {{ margin:8px 0; }}
.sub {{ color:#8b9ab3; }}
.kpis {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin:0 0 22px; }}
.kpi {{ background:#111c30; border:1px solid #22304a; border-radius:12px; padding:13px 14px; }}
.kpi span {{ display:block; font-size:11px; color:#8b9ab3; text-transform:uppercase; letter-spacing:.08em; }}
.kpi strong {{ display:block; font-size:21px; margin:4px 0 2px; }}
.kpi small {{ color:#7f8ea8; }}
.card {{ background:#0f192b; border:1px solid #22304a; border-radius:16px; padding:20px; margin:18px 0; }}
table {{ width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }}
.data th, .data td {{ padding:7px 9px; border-bottom:1px solid #1d2a41; text-align:right; }}
.data th:first-child, .data td:first-child {{ text-align:left; }}
.data th {{ color:#8b9ab3; font-weight:500; font-size:12px; }}
.data.compact th, .data.compact td {{ padding:5px 7px; font-size:12px; }}
.heatmap {{ font-size:11px; margin-top:6px; }}
.heatmap th, .heatmap td {{ padding:4px 3px; text-align:center; border:1px solid #17233a; }}
.heatmap th:first-child {{ text-align:left; }}
.heatmap .year {{ font-weight:600; color:#f0d98a; }}
.heatmap .muted {{ color:#42526b; }}
.barcell {{ width:140px; }}
.bar {{ display:inline-block; height:8px; border-radius:4px; background:linear-gradient(90deg,#d4af37,#7c5f18); }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; }}
.pos {{ color:#5fd3a2; }} .neg {{ color:#e47b86; }}
.buy {{ color:#5fd3a2; }} .sell {{ color:#e47b86; }}
.ok {{ color:#5fd3a2; }} .warn {{ color:#e6b45e; }}
.note {{ color:#8b9ab3; font-size:13px; }}
code {{ background:#17233a; padding:1px 5px; border-radius:4px; font-size:13px; }}
details {{ margin-top:8px; }}
summary {{ cursor:pointer; color:#d4af37; margin-bottom:8px; }}
table.data code {{ background:transparent; }}
footer {{ color:#63728a; font-size:12px; border-top:1px solid #1d2a41; margin-top:26px; padding-top:16px; }}
@media (max-width:900px) {{ .kpis {{ grid-template-columns:repeat(2,1fr); }} .cols {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body><div class="wrap">
<header>
  <div>
    <div class="brand">Meridian · Multi-Asset Crypto Desk</div>
    <h1>Rapport de gestion systématique</h1>
    <div class="sub">{result.start} → {result.end} · {", ".join(result.assets)} · long-only spot</div>
  </div>
  <div class="sub" style="text-align:right">
    Capital initial {_money(metrics.get('initial_equity'))}<br>
    Equity finale <strong style="color:#f0d98a">{_money(metrics.get('final_equity'))}</strong><br>
    Généré le {pd.Timestamp.now(tz='UTC').strftime('%Y-%m-%d %H:%M UTC')}
  </div>
</header>

<section class="kpis">{kpi_html}</section>

<section class="card">
  <h2>Courbe d'equity</h2>
  {_svg_equity(result.equity, result.benchmark)}
  <p class="note">Échelle logarithmique. La ligne pointillée est BTC buy &amp; hold sur la même période ;
  le benchmark ne paie pas de frais (hypothèse favorable au benchmark).</p>
</section>

<section class="card">
  <h2>Drawdown</h2>
  {_svg_drawdown(result.equity)}
</section>

<section class="card">
  <h2>Performance mensuelle</h2>
  {_monthly_table(result.equity)}
</section>

<section class="card">
  <h2>Portefeuille cible</h2>
  <div class="cols">
    <div>
      <table class="data">
        <thead><tr><th>Actif</th><th>Poids</th><th>Notionnel</th><th>Stop</th><th>Sleeve</th><th></th></tr></thead>
        <tbody>{_allocation_rows(result, cfg)}</tbody>
      </table>
      <p class="note">Poids cible après veto risque. Stop ATR sur clôture pour la tendance,
      stop intrabar pour le mean-reversion.</p>
    </div>
    <div>
      <h3>Mandat et limites</h3>
      <table class="data compact">
        <tbody>
          <tr><td>Volatilité cible</td><td>{_pct(get(cfg,'strategy.target_vol'))}</td></tr>
          <tr><td>Gross maximum</td><td>{_pct(get(cfg,'strategy.max_gross'))}</td></tr>
          <tr><td>Poids max par actif</td><td>{_pct(get(cfg,'strategy.max_asset_weight'))}</td></tr>
          <tr><td>Stop tendance</td><td>{_num(get(cfg,'risk.stop_atr'),1)} ATR</td></tr>
          <tr><td>De-risk drawdown</td><td>{_pct(get(cfg,'risk.dd_derisk'))}</td></tr>
          <tr><td>Gel drawdown</td><td>{_pct(get(cfg,'risk.dd_halt'))}</td></tr>
          <tr><td>Perte journalière max</td><td>{_pct(get(cfg,'risk.daily_loss_limit'))}</td></tr>
          <tr><td>Frais + slippage</td><td>{_num(get(cfg,'costs.fee_bps'),1)} + {_num(get(cfg,'costs.slippage_bps'),1)} bps</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</section>

{_research_section(research)}

<section class="card">
  <h2>Journal d'exécution récent</h2>
  <table class="data compact">
    <thead><tr><th>Date</th><th>Actif</th><th>Sens</th><th>Prix</th><th>Notionnel</th><th>PnL</th><th>Motif</th></tr></thead>
    <tbody>{_trades_table(result.trades)}</tbody>
  </table>
</section>

<section class="card">
  <h2>Qualité des données</h2>
  <table class="data compact">
    <thead><tr><th>Actif</th><th>Bougies</th><th>Période</th><th>Statut</th><th>ADV 20j</th></tr></thead>
    <tbody>{quality_rows or '<tr><td colspan="5">n/a</td></tr>'}</tbody>
  </table>
</section>

<section class="card">
  <details open>
    <summary>Memo de séance (généré par le superviseur)</summary>
    {_md_to_html(memo)}
  </details>
</section>

<footer>
Document de recherche et de paper trading. Aucun ordre réel n'est émis par ce rapport.
Les performances simulées ne préjugent pas des performances futures. Les hypothèses
de coûts, de liquidité et d'exécution sont explicites mais imparfaites. Ne pas utiliser
comme conseil en investissement.
</footer>
</div></body></html>"""
    report_path = os.path.join(out_dir, "report.html")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report_html)
    os.makedirs(REPORTS, exist_ok=True)
    shutil.copyfile(report_path, os.path.join(REPORTS, "latest.html"))
    return report_path
