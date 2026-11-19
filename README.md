# Meridian — desk crypto 8 actifs

Moteur de recherche et de **paper trading** systématique pour BTC, ETH, SOL,
LTC, XRP, BNB, LINK et AVAX. Le système est long-only spot, sans levier,
déterministe et auditable. Il décide sur bougies quotidiennes clôturées,
exécute à l'ouverture suivante, et n'envoie **aucun ordre réel**.

> Ce dépôt est un outil de recherche et de simulation. Ce n'est pas un conseil
> en investissement. Les performances passées, simulées ou papier, ne préjugent
> pas des performances futures.

---

## 1. Démarrage rapide

```bash
cd /root/meridian

# 1) données publiques Binance (aucune clé API)
python3 -m meridian download

# 2) backtest complet + rapport HTML
python3 -m meridian backtest --tag final

# 3) validation IS/OOS, walk-forward, Monte Carlo, stress de coûts
python3 -m meridian research --tag research-8assets

# 4) paper trading persistant
python3 -m meridian paper --init
python3 -m meridian paper

# 5) vérifier le paper contre le replay, puis suivre les kill criteria
python3 -m meridian paper --verify
python3 -m meridian monitor

# 6) dashboard terminal temps réel (sans web)
python3 -m meridian live

# 7) état du desk
python3 -m meridian status

# tests
python3 -m pytest -q
```

Le rapport est écrit dans `runs/<tag>/report.html` et copié dans
`reports/latest.html`. Le memo de séance est dans `runs/<tag>/memo.md`.
Le suivi opérationnel est écrit dans `runs/monitor.json` et `runs/monitor.md`.

Dépendances : Python ≥ 3.11 (testé sur 3.14), `numpy`, `pandas` ; `pytest`
uniquement pour les tests. Aucun package `requests` ni `matplotlib` n'est
nécessaire : HTTP via `urllib`, graphiques en SVG inline.

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install -r requirements-dev.txt
```

---

## 2. Principe : simple d'abord

Le mandat final tient en peu de règles :

- **un seul moteur de rendement** : la tendance / TSMOM, avec un filtre de
  régime BTC + largeur du marché ;
- **un overlay** : momentum transversal, top 2 des actifs les plus forts ;
- **pas de mean-reversion active** : le sleeve est conservé dans le code mais
  désactivé par `strategy.meanrev.enabled=false` ;
- **pas de hard stop catastrophique** : `risk.hard_stop_pct=0` ; les stops
  normaux restent des stops 6 ATR sur clôture ;
- **pas d'override baissier précoce** : `bear_breadth=0` ; le régime
  `TREND_DOWN` standard suffit ;
- **aucun levier, aucun short, aucun dérivé**.

Ce n'est pas un oubli : les quatre ajouts de complexité testés (mean-reversion,
override baissier, hard stop, sleeve XSMOM seul) ont été évalués puis, pour
trois d'entre eux, rejetés. Les bots les plus puissants sont parfois les plus
simples ; ici, la simplicité est un choix validé, pas une paresse.

---

## 3. Ce que fait le système

Le desk combine deux sources de rendement actives, filtrées par un régime de
marché global. La mean-reversion reste documentée comme sleeve suspendu.

| Sleeve | Thèse | Horizon | Statut |
|---|---|---|---|
| **Tendance / TSMOM** | Persistance des tendances crypto, cassures de canal, empilement EMA | semaines à mois | **actif** |
| **Momentum transversal** | Surperformance relative des majors les plus forts | semaines | **actif** |
| **Mean-reversion** | Excès baissiers temporaires en marché sans direction | jours | **suspendu** |

Le régime global (`TREND_UP`, `RANGE`, `TREND_DOWN`, `CRISIS`) est calculé sur
BTC + largeur du marché. En `TREND_DOWN` et `CRISIS`, le portefeuille va en
cash : le régime est un **interrupteur de risque**, pas un signal d'achat.

### Pipeline

```
API publique Binance (1d, bougies clôturées)
        │
        ▼
Data layer ── cache CSV ── contrôle qualité
        │
        ▼
FeatureStore (features causales, aucun look-ahead)
        │
        ▼
RegimeAgent ── BTC + breadth ── TREND_UP / RANGE / TREND_DOWN / CRISIS
        │
        ├── TrendAgent ──────────┐
        └── CrossSectionalAgent ─┴──► PortfolioAgent
                                        (blend, inverse-vol, plafonds, vol target)
                                             │
                                             ▼
                                       RiskAgent
                                       (stops, gouverneur drawdown, veto, cooldowns)
                                             │
                                             ▼
                                       ExecutionAgent
                                       (ouverture t+1, frais, slippage, impact, liquidité)
                                             │
                                             ▼
                                       SupervisorAgent
                                       (invariants, memo, journal)
```

Tous les agents sont **déterministes**. Aucun LLM n'intervient dans la chaîne
de décision. Un même état (config + données + date) produit toujours la même
décision, ce qui permet de rejouer et d'auditer chaque séance.

### Agents

| Agent | Rôle |
|---|---|
| `RegimeAgent` | Classer le marché et autoriser/interdire les sleeves |
| `TrendAgent` | TSMOM 90j, empilement EMA 50/200, cassure Donchian 55 |
| `MeanReversionAgent` | Suspendu ; code conservé pour un redesign validé |
| `CrossSectionalAgent` | Top 2 du momentum risque, dispersion minimale |
| `PortfolioAgent` | Blend par régime, score composite, inverse-vol, plafonds, vol target |
| `RiskAgent` | Stops, trailing, cooldowns, gouverneur drawdown, veto pré-exécution |
| `ExecutionAgent` | Ventes avant achats, frais/slippage/impact, plafond de participation |
| `SupervisorAgent` | Vérifie les invariants, produit le memo et journalise |

---

## 4. Mandat figé

Configuration par défaut : `target_vol=22%`, `max_gross=95%`, poids max par
actif `35%`, stop tendance `6 ATR`, rééquilibrage `14 jours`, bandes
d'entrée/sortie `25% / 12%`, gouverneur `-8% / -12%`, halt `25 séances`,
probation `40 séances`.

| Contrainte | Valeur |
|---|---|
| Univers | BTC, ETH, SOL, LTC, XRP, BNB, LINK, AVAX |
| Direction | long-only spot |
| Levier / short / dérivés | interdits |
| Volatilité cible | 22 % annualisée |
| Exposition brute max | 95 % |
| Cash minimum | 5 % |
| Poids max par actif | 35 % |
| Mode d'exécution | `paper` uniquement |
| Broker | aucun |

La configuration est figée dans `config/default.json`. La recherche finale
tourne en mode `research.freeze_mandate=true` : la grille de sensibilité ne
peut pas réécrire le mandat publié.

---

## 5. Résultats du mandat figé

Période complète : **2019-07-20 → 2026-11-15** (7,3 ans), capital initial
100 000 $.

| Métrique | Meridian | BTC buy & hold |
|---|---:|---:|
| Equity finale | **296 290 $** | 608 470 $ |
| Rendement total | **+196,3 %** | +508,5 % |
| CAGR | **+16,0 %** | +28,0 % |
| Volatilité | **16,1 %** | 59,9 % |
| Sharpe | **1,00** | 0,72 |
| Sortino | **0,88** | — |
| Calmar | **1,10** | 0,37 |
| Max drawdown | **-14,6 %** | -76,6 % |
| Jours sous l'eau | **821** | — |
| Exposition moyenne | **13,9 %** | 100 % |
| Exposition brute max | **62,4 %** | 100 % |
| Turnover annuel | **3,6x** | — |
| Frais | **4 343 $** | — |
| Coûts totaux | **6 143 $** | — |
| Trades clôturés | **248** | — |
| Hit rate | **61,7 %** | — |
| Profit factor | **2,69** | — |
| Beta vs BTC | **0,14** | 1,00 |
| Corrélation vs BTC | **0,51** | 1,00 |

Le rendement absolu est inférieur à BTC, mais avec **3,7 fois moins de
volatilité** et un drawdown maximal **5,3 fois plus faible**. Le beta de 0,14
signifie que le desk n'est pas un proxy BTC déguisé.

### In-sample / out-of-sample

Le mandat a été figé avant l'exécution finale. La grille de recherche est
publiée comme sensibilité, pas comme sélection. **Attention** : durant
l'exploration qui a conduit à ce mandat, l'OOS a été regardé. Ce n'est donc
pas un OOS totalement vierge ; il est publié comme validation, pas comme
preuve définitive.

| Phase | Période | CAGR | Vol | Sharpe | Max DD | Turnover | Trades |
|---|---|---:|---:|---:|---:|---:|---:|
| In-sample | 2019-07-19 → 2024-09-03 | +18,3 % | 16,4 % | 1,10 | -14,6 % | 3,5x | 178 |
| OOS nouveau compte | 2024-09-04 → 2026-11-15 | **+9,5 %** | 15,3 % | **0,67** | -11,5 % | 4,4x | 93 |
| OOS continu | 2024-09-04 → 2026-11-15 | **+10,9 %** | 15,3 % | **0,75** | -13,4 % | n/a | n/a |
| Complète | 2019-07-20 → 2026-11-15 | +16,0 % | 16,1 % | 1,00 | -14,6 % | 3,6x | 248 |

Les deux OOS diffèrent volontairement :

- **nouveau compte** : capital frais au début de l'OOS, il faut reconstruire
  les positions ; total **+22,1 %** sur 26 mois ;
- **continu** : les positions traversent la coupure, les rendements sont
  enchaînés et le drawdown se mesure depuis le plus haut historique ; total
  **+25,5 %** sur la même période.

**Lecture honnête :** l'OOS est positif, mais il reste une seule réalisation
de 26 mois. Il ne prouve pas que l'edge est permanent. Le walk-forward, plus
sévère, est nettement moins bon (section suivante).

Contexte OOS (2024-09-04 → 2026-11-15) :

| Actif | Rendement total | Max DD |
|---|---:|---:|
| BTC | +12,7 % | -53,0 % |
| ETH | -25,1 % | -67,6 % |
| SOL | -45,5 % | -76,3 % |
| LTC | -30,3 % | -70,1 % |
| XRP | +74,6 % | -74,2 % |
| BNB | +17,6 % | -58,2 % |
| LINK | -10,1 % | -75,4 % |
| AVAX | -66,6 % | -89,1 % |
| Panier équipondéré | -9,1 % | -64,5 % |
| **Meridian, compte neuf** | **+22,1 %** | **-11,5 %** |
| **Meridian, continu** | **+25,5 %** | **-13,4 %** |

Le desk a donc capté une partie de la hausse de XRP et BNB tout en évitant
l'effondrement de SOL, LINK et AVAX. C'est un résultat de sélection et de
protection, pas un beta crypto.

### Validation complémentaire

| Test | Résultat |
|---|---|
| Walk-forward optimisé, 8 fenêtres de 182 j | 4/8 positives · CAGR 8,5 % · Sharpe 0,57 · DD -20,0 % |
| Walk-forward à mandat fixe, mêmes fenêtres | 4/8 positives · CAGR 9,3 % · Sharpe 0,61 · DD -19,9 % |
| Monte Carlo, 800 chemins par blocs de 20 j | CAGR p5/p50/p95 : 4,2 % / 16,3 % / 28,8 % |
| Monte Carlo, drawdown | DD p5/p50/p95 : -35,5 % / -21,5 % / -14,3 % |
| Monte Carlo, risque | P(perte) 1,1 % · P(DD < -20 %) **61,8 %** |
| Stress de coûts ×5 | CAGR 11,2 % · Sharpe 0,78 · DD -15,6 % |
| Départs annuels 2020→2026 | CAGR de +13,3 % à -3,0 % selon le point de départ |

**Le walk-forward est le point faible.** Il ne couvre que les dernières
fenêtres (à partir de fin 2022), donc la période la plus difficile pour les
altcoins, et il ne reproduit pas le CAGR de la période complète. C'est un
avertissement, pas un détail. Le Monte Carlo confirme qu'un drawdown de 20 à
35 % reste possible malgré le gouverneur : **la limite de -12 % est un seuil
d'action, pas une garantie**.

Le stress de coûts montre que le système ne dépend pas d'hypothèses de frais
irréalistes : même à ×5, le CAGR reste à deux chiffres.

### Attributions

| Sleeve | Trades clôturés | PnL cumulé | Hit rate |
|---|---:|---:|---:|
| Tendance | 217 | +204 729 $ | 64,5 % |
| Momentum transversal | 31 | -6 349 $ | 41,9 % |
| Mean-reversion | 0 | 0 $ | — |

Le moteur de performance est la tendance. Le XSMOM est **négatif en
standalone**, mais son retrait dégrade fortement le portefeuille (CAGR 10,8 %
et OOS -1,4 % dans les tests) : il apporte de la diversification et des
signaux que la tendance seule ne voit pas. C'est un point de surveillance, pas
une réussite à masquer.

Par actif :

| Actif | Trades | PnL | Hit rate |
|---|---:|---:|---:|
| BNB | 42 | +52 474 $ | 66,7 % |
| SOL | 28 | +53 543 $ | 71,4 % |
| BTC | 44 | +30 988 $ | 65,9 % |
| XRP | 23 | +26 065 $ | 47,8 % |
| ETH | 44 | +20 928 $ | 61,4 % |
| LINK | 30 | +10 742 $ | 56,7 % |
| AVAX | 15 | +8 212 $ | 66,7 % |
| LTC | 22 | -4 573 $ | 50,0 % |

Les quatre nouveaux actifs (XRP, BNB, LINK, AVAX) contribuent **+97 493 $**,
soit environ **49 % du PnL net** des sleeves. La dépendance à BNB et SOL est
réelle et doit être surveillée.

Répartition des régimes sur la période : `TREND_UP` 42,5 %, `RANGE` 28,3 %,
`TREND_DOWN` 23,3 %, `CRISIS` 5,9 %. Quatre halts du gouverneur :
2020-03-12, 2023-03-09, 2024-07-31, 2025-11-04.

### Rendements annuels

| Année | Rendement | Exposition moyenne | DD fin d'année |
|---|---:|---:|---:|
| 2019 (partiel) | -7,1 % | 5,9 % | -7,4 % |
| 2020 | +37,6 % | 18,5 % | -0,5 % |
| 2021 | +53,3 % | 19,1 % | -9,1 % |
| 2022 | -1,4 % | 0,3 % | -10,4 % |
| 2023 | +6,8 % | 12,0 % | -4,3 % |
| 2024 | +33,7 % | 24,2 % | -7,1 % |
| 2025 | +8,5 % | 21,7 % | -12,2 % |
| 2026 (YTD) | -1,1 % | 4,1 % | -13,2 % |

La dispersion annuelle est importante. 2021 et 2024 portent une grande partie
du résultat. 2022 est défensif (exposition 0,3 %), ce qui illustre le rôle du
régime. 2025-2026 est plus difficile : le desk reste défensif mais subit
quelques stops et faux départs. Le démarrage 2026 est négatif (-3,0 % si on
part du 1er janvier 2026).

### Variantes testées

Comparaisons de diagnostic, pas de sélection OOS. Les trois premières lignes
sont des étapes cumulatives ; les suivantes sont des écarts au mandat final.

| Variante | CAGR complet | Sharpe | DD | OOS continu |
|---|---:|---:|---:|---:|
| Mean-reversion active | 12,5 % | 0,85 | -15,8 % | 4,8 % |
| Mean-reversion suspendue | 14,3 % | 0,93 | -15,5 % | 9,8 % |
| + gouverneur 25/40 (mandat final) | **16,0 %** | **1,00** | **-14,6 %** | **10,9 %** |
| XSMOM désactivé | 10,8 % | 0,78 | -15,5 % | -1,4 % |
| Override baissier 20 % | 11,6 % | 0,83 | -15,6 % | 3,7 % |
| Hard stop 20 % | 8,7 % | 0,71 | -17,8 % | 1,3 % |

Les hard stops 15-30 %, les overrides baissiers 15-35 % et le XSMOM seul ont
été testés et rejetés. Ne pas les réactiver sans une nouvelle validation
complète.

---

## 6. Paper trading et suivi

Le paper trading est un **replay déterministe**, pas un connecteur broker :

- `paper --init` démarre à la dernière séance clôturée, 100 % cash ;
- chaque exécution rejoue la stratégie depuis la date de départ avec les
  données réelles, exécute les intentions de la veille à l'ouverture suivante
  et produit les intentions de la prochaine ouverture ;
- l'état est persisté dans `runs/paper/state.json`, le journal en JSONL dans
  `runs/paper/journal.jsonl`, le memo dans `runs/paper/memo.md` ;
- le dernier snapshot structuré du moteur est écrit dans
  `runs/paper/snapshot.json` : il alimente le dashboard terminal sans
  recalculer tout le backtest à chaque rafraîchissement ;
- un changement de configuration est **refusé** : le mandat ne dérive pas en
  silence. Il faut archiver l'état et réinitialiser explicitement.

Le paper a été réinitialisé le 2026-11-15 avec les 8 actifs et le mandat
final : 100 000 $, aucune position, aucune intention. C'est le comportement
attendu : BTC est sous son EMA200, la largeur du marché est à 0 %, la
mean-reversion est suspendue, donc le desk reste 100 % cash.

```bash
python3 -m meridian paper --init --start 2025-01-01
python3 -m meridian paper
python3 -m meridian paper --status
python3 -m meridian paper --verify       # état vs replay, sans modifier l'état
python3 -m meridian paper --init --force # réinitialisation explicite
```

`paper --verify` compare l'equity, le cash, les positions, les stops et les
intentions au replay. Une nouvelle bougie disponible est signalée comme
avertissement, pas comme divergence : il faut simplement relancer `paper`.

### Monitor et kill criteria

`python3 -m meridian monitor` produit un état `OK`, `WATCH` ou `ALERT` :

| Critère | Watch | Kill / ALERT |
|---|---|---|
| Drawdown courant | ≤ -12 % | ≤ -20 % |
| Sharpe glissant 90 j | ≤ 0 | ≤ -0,5 (après 60 j d'historique) |
| Données périmées | — | > 3 jours |
| Config paper | — | hash différent |
| Réconciliation replay | nouvelle bougie | écart > 1 % |
| Exposition | — | brut > 95 % ou actif > 35 % |
| Turnover | > 9x | — |

Le monitor écrit `runs/monitor.json` et `runs/monitor.md`. Un `ALERT`
déclenche le kill switch : arrêt du paper et revue humaine. Il ne « répare »
jamais silencieusement un écart.

Au démarrage du paper, le Sharpe glissant est en `WATCH` car l'historique est
trop court (< 60 jours). Ce n'est pas une alerte de performance.

### Dashboard terminal temps réel

`python3 -m meridian live` ouvre un tableau de bord plein écran dans le
terminal. Il n'y a **pas de serveur web**, pas de navigateur et pas de port
réseau : tout reste dans la session terminal.

```bash
python3 -m meridian live                 # plein écran, rafraîchi en continu
python3 -m meridian live --window        # tente une nouvelle fenêtre terminal
python3 -m meridian live --plain         # une trame texte, puis quitter
python3 -m meridian live --follow        # texte continu, sans curses
```

Pour un affichage continu dans une autre fenêtre sans interface graphique,
`--follow` peut écrire dans un fichier suivi par `tail` :

```bash
python3 -m meridian live --follow --interval 10 > runs/live.log 2>&1 &
tmux new-session -d -s meridian "tail -n 300 -f runs/live.log"
tmux attach -t meridian                  # détacher : Ctrl-b puis d
```

Arrêt : `tmux kill-session -t meridian` puis
`pkill -f "meridian live --follow"`.

### Lancer hors OpenCode (Termux)

OpenCode tourne dans un conteneur Ubuntu géré par `proot-distro`, avec son
propre Python. Le dashboard doit donc être lancé **dans ce conteneur**, pas
avec le Python de Termux.

Depuis Termux, après avoir quitté OpenCode :

```bash
proot-distro login ubuntu -- bash -lc 'cd /root/meridian && exec python3 -m meridian live'
```

ou, avec le lanceur fourni :

```bash
bash ~/meridian-live.sh
```

Si tu veux un affichage détaché, lance ce script dans tmux côté Termux :

```bash
tmux new -s meridian 'bash ~/meridian-live.sh'
tmux attach -t meridian      # détacher : Ctrl-b puis d
```

Le dashboard est une interface **terminal plein écran**, pas une fenêtre
graphique Android ni un navigateur. `q` quitte et rend la main au shell.

Le dashboard affiche :

- l'heure et l'état `OK` / `WATCH` / `ALERT` ;
- la dernière bougie, le retard, la source et le compte à rebours de clôture ;
- le régime, la breadth, l'ADX et la volatilité percentile ;
- l'equity, le cash, l'exposition, le drawdown et les intentions ;
- les signaux tendance / mean-reversion / XSMOM et les poids cibles ;
- les positions, les stops et les intentions pour l'ouverture suivante ;
- les contrôles du monitor et les derniers événements du journal.

Touches : `q` quitter, `r` rafraîchir, `p` exécuter le paper, `d` télécharger
les données, `a` activer/désactiver l'exécution automatique, `f` activer le
téléchargement périodique, `↑` / `↓` défiler.

L'exécution automatique ne se déclenche **que si une nouvelle bougie clôturée
est disponible** ; elle ne rejoue pas la même séance en boucle. Le dashboard
réutilise `runs/paper/snapshot.json` pour rester fluide et ne recalcule le
monitor complet que périodiquement ou sur demande. En environnement sans
affichage graphique, `--window` ne peut pas ouvrir de fenêtre : le dashboard
démarre alors dans le terminal courant.

### Cadence d'exécution et hébergement

La stratégie travaille sur des bougies **journalières** (`interval: "1d"`).
Un seul passage par jour après la clôture UTC (00:00) suffit :

```bash
python3 -m meridian download   # récupère la bougie clôturée
python3 -m meridian paper      # rejoue et met à jour l'état
python3 -m meridian monitor    # vérifie les kill criteria
```

Le rééquilibrage n'a lieu que tous les **14 jours**
(`strategy.rebalance_days`), ou immédiatement si le régime change, si
l'échelle du gouverneur change, ou si une position mean-reversion doit
sortir. La plupart des jours ne produisent donc aucun nouvel ordre.

Le dashboard `live` est uniquement un affichage : 5 s entre deux trames,
snapshot recalculé toutes les 30 s, monitor toutes les 300 s. Il ne faut pas
confondre cette cadence d'affichage avec la cadence de trading.

Le script `scripts/daily.sh` enchaîne les trois étapes avec un verrou pour
éviter deux exécutions simultanées. Cron quotidien à 00:15 UTC :

```cron
15 0 * * * /root/meridian/scripts/daily.sh >> /root/meridian/runs/daily.log 2>&1
```

Sur un VPS, un timer `systemd` peut remplacer cron. Un petit serveur
1 vCPU / 1 Go suffit ; aucun port à ouvrir, aucune clé d'exchange, aucun
ordre réel. Sur Android/Termux, cron n'est pas installé et le système peut
suspendre les tâches : un VPS est plus fiable pour l'hébergement.

### GitHub Actions (option cloud)

Le workflow `.github/workflows/meridian-daily.yml` exécute la séance chaque
jour à 00:30 UTC. Il peut aussi être lancé à la main depuis l'onglet Actions
(`Run workflow`).

- l'état du paper est conservé sur la branche `meridian-state` ; ne pas la
  supprimer ;
- au premier run, le paper est initialisé à la dernière bougie clôturée ;
- les données sont téléchargées avec `--force`, puis mises en cache ;
- aucun secret, aucune clé d'exchange, aucun ordre réel ;
- le job passe en rouge si le monitor est en `ALERT`, si le paper diverge ou
  si les données réelles sont indisponibles.

Le workflow s'exécute uniquement sur la branche par défaut. Si le push de la
branche d'état est refusé, autoriser l'écriture dans Settings → Actions →
General → Workflow permissions. GitHub peut retarder les crons et désactive
les workflows planifiés après 60 jours sans activité du dépôt ;
`workflow_dispatch` permet de relancer manuellement.

Garde-fou live : `execution.mode` doit valoir `paper`. Toute autre valeur lève
`LiveTradingDisabled`. L'ajout du live exige un adaptateur broker, des clés
hors dépôt, une gestion d'erreurs réseau, un rapprochement de positions et une
revue de risque séparée. Ce n'est pas implémenté.

---

## 7. Commandes

| Commande | Effet |
|---|---|
| `python3 -m meridian download` | Télécharge les 8 actifs depuis Binance, cache CSV, contrôle qualité |
| `python3 -m meridian backtest` | Backtest complet, métriques, rapport HTML, memo |
| `python3 -m meridian research` | IS/OOS, grille, walk-forward, Monte Carlo, stress, sensibilité |
| `python3 -m meridian paper --init` | Initialise le portefeuille papier |
| `python3 -m meridian paper` | Exécute une séance papier (replay + intentions) |
| `python3 -m meridian paper --status` | Affiche l'état papier sans exécuter |
| `python3 -m meridian paper --verify` | Audite l'état papier contre le replay |
| `python3 -m meridian monitor` | Suivi opérationnel, rolling Sharpe, kill criteria |
| `python3 -m meridian live` | Dashboard terminal temps réel, avec `--window` |
| `python3 -m meridian report` | Reconstruit le rapport à partir de la config courante |
| `python3 -m meridian status` | État des données et du paper |
| `python3 -m pytest -q` | Suite de tests |

Options utiles : `--start`, `--end`, `--initial-equity`, `--cost-multiplier`,
`--tag`, `--config`, `--no-report`, `--json`. Pour le dashboard :
`--interval`, `--snapshot-interval`, `--monitor-interval`, `--fetch`,
`--no-auto-run`, `--plain`, `--follow`, `--window`.

---

## 8. Limites

- **Biais d'univers** : les 8 actifs sont choisis a posteriori ; ils ont
  survécu et sont liquides. Le biais de survie est plus fort qu'avec 4 actifs.
- **Walk-forward faible** : 4/8 folds positifs, CAGR 8,5-9,3 %, DD -20 %.
  C'est le test le plus sévère et il ne valide pas le CAGR complet.
- **Un seul OOS** : 26 mois, une seule réalisation, déjà regardé pendant
  l'exploration.
- **Drawdown** : le Monte Carlo donne 61,8 % de probabilité de dépasser
  -20 % sur un chemin de 7 ans. Le gouverneur réduit, il ne garantit pas.
- **Concentration** : BNB et SOL représentent une part importante du PnL.
  LTC est perdant. Les nouveaux actifs ont moins d'historique (LINK depuis
  2019-01, AVAX depuis 2020-09).
- **XSMOM négatif en standalone** : conservé pour la diversification, mais
  fragile.
- **Mean-reversion suspendue** : le sleeve n'est pas validé sur cet univers.
  Sa réactivation exige un redesign et une nouvelle période de validation.
- **Coûts et exécution** : pas de carnet, pas de latence, pas de partial
  fills. Le slippage est déterministe.
- **Paper, pas live** : aucun ordre réel, aucune preuve de rentabilité future.

---

## 9. Reproductibilité

```bash
cd /root/meridian

python3 -m meridian download
python3 -m meridian research --tag research-8assets
python3 -m meridian backtest --tag final
python3 -m meridian paper --init --force
python3 -m meridian paper --verify
python3 -m meridian monitor
python3 -m meridian live --plain
python3 -m pytest -o addopts="" -q
```

Le rapport complet est dans `runs/final/report.html`, copié dans
`reports/latest.html`. Les exports CSV/JSON sont dans `runs/final/`.
Le snapshot du dashboard est dans `runs/paper/snapshot.json`.
La configuration est figée dans `config/default.json`. L'ancien état 4 actifs
est archivé dans `runs/archive/4assets-final/`.
