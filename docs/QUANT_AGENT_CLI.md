# quant-agent — CLI de recherche quant

Prototype local du copilote de recherche Meridian. Il expose le moteur de
backtest, le portefeuille paper et le rapport de risque à un agent capable
d'appeler des outils déterministes.

> **Périmètre** : paper only. Long-only spot, sans levier, sans short, sans
> dérivés. Aucun ordre réel n'est envoyé. Aucun conseil financier.
> Les performances passées ne préjugent pas des performances futures.

- Version : `0.1.0`
- Date de référence : 20 novembre 2026
- Provider par défaut : `mock`, sans clé API
- Stratégies : `trend_momentum` (défaut) et `mean_reversion`
- Outils exposés : 8
- Suite de tests : 96 tests, dont 12 pour le CLI

---

## 1. Démarrage rapide

Depuis la racine du dépôt :

```bash
cd /root/meridian

# Lister les stratégies
python3 -m quant_agent strategies

# Poser une question à l'agent (provider mock, hors ligne)
python3 -m quant_agent ask "Fais un backtest trend momentum sur 2 ans"

# Backtest direct, sans passer par l'agent
python3 -m quant_agent backtest --strategy trend_momentum --lookback-days 730

# Afficher le dernier backtest et le comparer
python3 -m quant_agent show latest
python3 -m quant_agent compare bt_xxxxxxxxxxxx bt_yyyyyyyyyyyy

# Lire le paper et le risque
python3 -m quant_agent portfolio
python3 -m quant_agent risk

# Quotas locaux et catalogue d'outils
python3 -m quant_agent usage
python3 -m quant_agent tools
```

Le paquet n'a pas besoin d'être installé pour `python3 -m quant_agent`.
Pour disposer de la commande courte :

```bash
python3 -m pip install -e .
quant-agent --help
```

Le CLI n'utilise que le cache de données local par défaut. Il ne télécharge
pas silencieusement des bougies avant un backtest. Utiliser `--refresh` pour
demander explicitement une mise à jour Binance.

---

## 2. Commandes

| Commande | Effet |
|---|---|
| `quant-agent ask "question"` | Un tour d'agent : plan, outils, réponse. |
| `quant-agent chat` | Session interactive avec historique court. |
| `quant-agent backtest` | Backtest déterministe d'un preset. |
| `quant-agent compare <a> <b>` | Comparaison de deux runs. |
| `quant-agent show [run_id]` | Détail d'un run, `latest` par défaut. |
| `quant-agent strategies` | Liste les presets disponibles. |
| `quant-agent portfolio` | Lit le portefeuille paper, en lecture seule. |
| `quant-agent risk` | Rapport de risque Meridian et kill switch. |
| `quant-agent usage` | Quotas locaux du jour. |
| `quant-agent tools` | Catalogue des outils de l'agent. |

### `ask`

```bash
python3 -m quant_agent ask "Quelles stratégies as-tu ?"
python3 -m quant_agent ask "Compare trend et mean reversion sur 2 ans"
python3 -m quant_agent ask "Explique le max drawdown"
python3 -m quant_agent ask "Où en est le portefeuille paper ?"
python3 -m quant_agent ask "Quel est le risque actuel ?"
```

Options :

- `--provider` : `mock` (défaut), `openai`, `openrouter`, `groq`, `ollama`.
- `--model` : modèle du provider OpenAI-compatible.
- `--mode` : `fast`, `kraken` (défaut), `deep`.
- `--no-quota` : désactive les quotas pour la session.
- `--trace` : affiche les outils appelés, leur durée et leurs `run_id`.
- `--json` : réponse structurée, utile pour l'intégration.

### `chat`

```text
/help          aide
/clear         efface l'historique
/trace         active ou désactive la trace
/mode deep     change le budget de raisonnement
/reset         remet les quotas locaux à zéro
/strategies    liste les presets
/portfolio     lit le paper
/risk          rapport de risque
/usage         quotas
/exit          quitter
```

L'historique est volontairement limité (`--max-history`, 12 messages par
défaut) et ne conserve que les tours utilisateur/assistant.

### `backtest`

```bash
python3 -m quant_agent backtest \
  --strategy trend_momentum \
  --start 2024-01-01 --end 2026-11-15 \
  --initial-equity 100000 \
  --cost-multiplier 1.0
```

Options principales :

| Option | Défaut | Rôle |
|---|---:|---|
| `--strategy` | `trend_momentum` | Preset à tester. |
| `--start` | fenêtre glissante | Date de début ISO. |
| `--end` | dernière bougie | Date de fin ISO. |
| `--lookback-days` | `730` | Fenêtre si `--start` est absent. |
| `--initial-equity` | config Meridian | Capital simulé. |
| `--cost-multiplier` | `1.0` | Multiplie frais, slippage et impact. |
| `--refresh` | faux | Télécharge Binance avant le test. |
| `--detail` | faux | Affiche le détail après le résumé. |
| `--json` | faux | Sortie JSON. |

La fenêtre est ramenée aux bornes des données disponibles, avec un
avertissement. Un run identique sur les mêmes données réutilise le cache et
ne consomme pas de quota de backtest.

---

## 3. Stratégies

Les presets sont des surcharges de la configuration Meridian, validées par
`meridian.config.load_config`. Le moteur reste celui de Meridian.

### `trend_momentum` (défaut)

Suit la tendance moyen terme : TSMOM, empilement EMA, cassure Donchian, puis
renforce les deux actifs les plus forts. Le sleeve mean-reversion est
désactivé, le cross-sectional momentum activé.

### `mean_reversion`

Active les achats de survente en régime `RANGE` (z-score, RSI) avec sortie
rapide et stop serré. Le sleeve trend reste le filtre directionnel ; le
cross-sectional momentum est coupé.

Le Risk Manager peut suspendre un sleeve ou mettre un veto. Les presets ne
contournent ni les plafonds d'exposition, ni le cash minimum, ni le
gouverneur de drawdown.

---

## 4. Outils de l'agent

L'agent ne calcule aucun chiffre lui-même : il appelle un outil, lit le
résultat JSON, puis rédige. Les arguments sont validés avant exécution.

| Outil | Arguments | Résultat |
|---|---|---|
| `list_strategies` | aucun | Presets, description, défaut. |
| `run_backtest` | `strategy`, `start`, `end`, `lookback_days`, `initial_equity`, `cost_multiplier`, `refresh` | `run_id`, métriques, avertissements. |
| `get_backtest` | `run_id` (`latest` accepté), `include_curve` | Métriques, mois, trades, diagnostics. |
| `compare_backtests` | `run_id_a`, `run_id_b` | Tableau, deltas, corrélation. |
| `get_paper_portfolio` | aucun | Equity, cash, positions, intentions, memo. |
| `get_risk_report` | `refresh` | Checks, drawdown, rolling Sharpe, kill switch. |
| `explain_metric` | `metric` | Définition, lecture, limite. |
| `get_usage` | aucun | Quotas locaux du jour. |

Le catalogue est aussi disponible sans agent :

```bash
python3 -m quant_agent tools
python3 -m quant_agent tools --json
```

### Métriques expliquées

`explain_metric` couvre notamment : `cagr`, `sharpe`, `sortino`, `calmar`,
`volatility`, `max_drawdown`, `drawdown`, `turnover_x`, `hit_rate`,
`profit_factor`, `expectancy`, `exposure`, `beta`, `correlation`.

---

## 5. Providers de modèles

### `mock` — défaut

Routeur d'intentions déterministe, sans réseau ni clé. Il appelle les mêmes
outils que le provider réel et sert aux tests, aux démos et au fonctionnement
hors ligne.

```bash
python3 -m quant_agent ask "Fais un backtest trend sur 2 ans"
```

### Provider OpenAI-compatible

Le client minimal parle `/chat/completions` avec tool calling. Il fonctionne
avec OpenRouter, Groq, Ollama, OpenAI ou un serveur compatible local.

Variables d'environnement :

| Variable | Rôle |
|---|---|
| `QUANT_AGENT_PROVIDER` | `mock`, `openai`, `openrouter`, `groq`, `ollama`. |
| `QUANT_AGENT_MODEL` | Modèle demandé. |
| `QUANT_AGENT_API_KEY` | Clé du provider. |
| `QUANT_AGENT_BASE_URL` | URL compatible OpenAI. |
| `QUANT_AGENT_FALLBACK_MODELS` | Modèles de secours, séparés par des virgules. |
| `QUANT_AGENT_TIMEOUT` | Timeout HTTP en secondes (défaut 60). |
| `QUANT_AGENT_MAX_TOKENS` | Tokens de sortie (défaut 1500). |
| `QUANT_AGENT_TEMPERATURE` | Température (défaut 0.2). |
| `QUANT_AGENT_MODE` | `fast`, `kraken`, `deep`. |

Des alias sont acceptés pour les clés : `OPENROUTER_API_KEY`,
`GROQ_API_KEY`, `OPENAI_API_KEY`. Une clé peut rester absente pour un serveur
local (`localhost`, `127.0.0.1`, `0.0.0.0`).

Exemples :

```bash
# OpenRouter — remplacer par un modèle gratuit disponible
export QUANT_AGENT_PROVIDER=openrouter
export QUANT_AGENT_MODEL="<modele-gratuit>"
export QUANT_AGENT_API_KEY="..."
python3 -m quant_agent ask "Explique le Sharpe"

# Groq
export QUANT_AGENT_PROVIDER=groq
export QUANT_AGENT_MODEL=llama-3.3-70b-versatile
export QUANT_AGENT_API_KEY="..."

# Ollama local, sans clé
export QUANT_AGENT_PROVIDER=ollama
export QUANT_AGENT_BASE_URL=http://localhost:11434/v1
export QUANT_AGENT_MODEL=qwen2.5:7b
```

Le provider ne reçoit jamais de clé d'exchange. Il ne peut appeler que les
outils du registre. Une erreur HTTP ou un timeout est remonté lisiblement ;
le CLI reste utilisable avec `mock`.

---

## 6. Modes de raisonnement

« Kraken » est une politique d'orchestration, pas un modèle à entraîner.
Elle fixe le budget d'outils, le nombre d'étapes et la durée maximale.

| Mode | Appels d'outils | Étapes | Durée | Usage |
|---|---:|---:|---:|---|
| `fast` | 2 | 6 | 75 s | Réponse courte. |
| `kraken` | 8 | 12 | 180 s | Défaut, plan + vérification. |
| `deep` | 12 | 20 | 300 s | Analyse longue. |

Chaque appel est tracé dans `runs/agent/audit.jsonl` avec le `run_id`
éventuel, la durée, le mode et le provider.

---

## 7. Quotas et cache

### Quotas locaux

| Ressource | Limite par défaut |
|---|---:|
| Messages | 20 / jour |
| Backtests | 5 / jour |
| Tokens | 100 000 / jour |

Réinitialisation à `00:00 UTC`. Les limites sont surchargeables :

```bash
export QUANT_AGENT_DAILY_MESSAGES=50
export QUANT_AGENT_DAILY_BACKTESTS=10
export QUANT_AGENT_DAILY_TOKENS=200000
```

`--no-quota` désactive les compteurs pour une session. Un backtest déjà en
cache ne consomme pas de quota de backtest.

Ce système est un contrat d'usage local, pas un anti-abus de production. La
v1 publique devra déplacer les quotas côté serveur, avec authentification.

### Cache et reproductibilité

Un run est identifié par un hash stable de :

- la stratégie et sa configuration effective ;
- la fenêtre et le capital initial ;
- le multiplicateur de coûts ;
- un snapshot des données utilisées (nombre de bougies, première et dernière
  date par actif).

Même stratégie + mêmes données + mêmes paramètres ⇒ même `run_id`.
Les fichiers sont écrits dans :

```text
runs/agent/
├── backtests/<run_id>.json
├── index.json
├── audit.jsonl
└── usage.json
```

`runs/` est ignoré par Git : les runs sont reproductibles, pas versionnés.

---

## 8. Risk Manager

Le rapport de risque est produit par le moteur Meridian, pas par le LLM. Il
contrôle notamment :

- fraîcheur et qualité des données ;
- cohérence du mandat et de la configuration ;
- réconciliation paper/replay ;
- drawdown courant et drawdown maximal ;
- Sharpe glissant ;
- exposition brute et concentration ;
- turnover ;
- état du gouverneur et kill switch.

Un rapport `ALERT` n'est pas forcément une panne. Sur l'environnement local du
20 novembre 2026, la dernière bougie date du 15 novembre : le contrôle de
fraîcheur déclenche `ALERT` et le kill switch est actif. C'est le comportement
attendu tant que les données ne sont pas rafraîchies.

```bash
python3 -m quant_agent risk
python3 -m quant_agent risk --refresh
python3 -m quant_agent risk --json
```

---

## 9. Organisation du code

| Fichier | Rôle |
|---|---|
| `quant_agent/__main__.py` | Parser, commandes, entrée `quant-agent`. |
| `quant_agent/presets.py` | Presets et construction de config validée. |
| `quant_agent/data_service.py` | Cache local, snapshot, refresh explicite. |
| `quant_agent/store.py` | Hash des runs, cache JSON, comparaison. |
| `quant_agent/quotas.py` | Quotas journaliers locaux. |
| `quant_agent/tools.py` | 8 outils, validation, backtest, paper, risque. |
| `quant_agent/formatting.py` | Rendu texte français. |
| `quant_agent/providers.py` | Provider mock et client OpenAI-compatible. |
| `quant_agent/policy.py` | Budgets Kraken et prompt système. |
| `quant_agent/agent.py` | Boucle agent, traces, audit JSONL. |
| `quant_agent/jsonutil.py` | Sérialisation JSON stable. |

---

## 10. Tests

```bash
python3 -m pytest -q
```

La suite couvre le moteur Meridian, le paper, le monitor, la recherche et le
CLI. Les tests du CLI vérifient notamment :

- les deux presets et leurs différences ;
- la stabilité du `run_id` et le cache ;
- le routage du provider mock vers les bons outils ;
- l'extraction des dates et des comparaisons ;
- le refus du trading réel ;
- la validation des arguments ;
- les quotas ;
- la boucle d'agent et l'audit ;
- l'affichage du drawdown le plus faible.

Le panel synthétique des tests est figé au `2026-06-30` dans
`tests/conftest.py`. Sans cette date fixe, `synthetic_panel` se terminait au
jour courant et la fenêtre glissante rendait certains tests path-dependent
instables.

---

## 11. Limites du prototype

- Pas d'authentification, pas de multi-utilisateur, pas de PostgreSQL.
- Quotas locaux uniquement ; ils ne protègent pas un service public.
- Deux presets seulement ; pas de stratégie générée librement.
- Pas d'entraînement de modèle, pas de fine-tuning.
- Pas de mode Developer sandboxé dans ce CLI.
- Les backtests peuvent prendre plusieurs dizaines de secondes.
- Le cache de données peut être périmé ; le CLI l'affiche et ne le masque pas.
- Pas de carnet d'ordres, de latence réseau ou de fills partiels.
- Aucun ordre réel, aucun levier, aucun short, aucun dérivé.
- Aucune promesse de rendement ni conseil d'investissement personnalisé.

---

## 12. Prochaines étapes

1. Exposer les mêmes outils derrière une API FastAPI.
2. Ajouter une interface Next.js et une authentification.
3. Déplacer les quotas et l'audit côté serveur.
4. Ajouter PostgreSQL pour les runs, les usages et les snapshots.
5. Enrichir le registre de stratégies avec validation OOS.
6. Synchroniser le paper cloud et le paper local sans double source de vérité.
7. Préparer un mode Developer sandboxé, sans exécution libre sur le serveur.
8. Étudier les modèles payants après la v1 gratuite et ses retours utilisateurs.

---

*Document de travail. Paper only. Les performances passées ne préjugent pas
des performances futures.*
