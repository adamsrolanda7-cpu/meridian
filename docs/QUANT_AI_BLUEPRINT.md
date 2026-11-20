# Meridian Lab — Blueprint du projet AI quant

**Statut** : document de travail, v0.1  
**Date** : 2026-11-20  
**But** : consigner la vision, les décisions, l'architecture, le périmètre v1, la roadmap et les idées du projet d'AI quant.

---

## 1. Vision

Construire un **« OpenCode for quant »** : un agent de recherche quant auditable, pas un chatbot qui prédit les prix.

Le produit doit permettre à un utilisateur de :

1. décrire une idée de stratégie ;
2. lancer un backtest reproductible ;
3. vérifier le risque ;
4. créer un portefeuille paper ;
5. obtenir un rapport clair, avec les `run_id` et les sources.

**Paper trading uniquement.** Aucun ordre réel, aucun conseil financier, aucune promesse de rendement.

### Positionnement recommandé

**« AI research copilot for crypto daily strategies »**

- marché : crypto, données journalières ;
- stratégies : trend, momentum cross-sectionnel, volatility targeting ;
- utilisateurs : retail crypto systematique, étudiants, développeurs curieux ;
- différenciation : backtests auditables, risk-first, paper trading, données vérifiées.

> Le produit est spécialisé. La plateforme, elle, peut rester générale.

---

## 2. Décisions structurantes

| Sujet | Décision |
|---|---|
| Plateforme vs produit | Plateforme générale, produit spécialisé |
| Spécialisation | Par problème/marché/workflow, pas par métier |
| Niche v1 | Crypto daily, long-only spot, 8 actifs |
| Stratégies v1 | Trend, XS momentum, vol targeting |
| Rôles | 8 profils = modules/workflows, pas 8 chatbots |
| Agent | 1 superviseur + 2 agents LLM + 6 services déterministes |
| Mode de raisonnement | « Kraken » = politique d'orchestration, pas un modèle à entraîner |
| Entraînement | Aucun entraînement en v1 |
| Modèles | Gratuits par défaut, fallback, payants plus tard |
| Monétisation | v1 gratuite avec quotas ; paiements à 18 ans |
| Trading | Paper only ; aucun ordre réel |
| Stack | Python/FastAPI + Next.js + PostgreSQL |
| Rust | Plus tard, pour les composants critiques |
| Nom public | À trouver ; « Kraken » reste un nom interne |

### Décision sur la spécialisation

Le produit se spécialise sur le **problème** « recherche, backtest, risque et paper trading pour stratégies crypto daily », pas sur le **métier** « quant developer ».

- Le mode **Developer** reste un mode interne (génération de code sandboxée, plus tard).
- Le positionnement public est **Researcher / Risk / Analyst**.
- Les 8 profils restent des modules de la plateforme.
- Cette décision modifie le **positionnement et le périmètre v1**, pas l'architecture.

---

## 3. Les 8 profils quant

Ce sont des **responsabilités**, pas huit LLM qui discutent entre eux.

| Profil | Responsabilité | Implémentation |
|---|---|---|
| **Quant Researcher** | hypothèses, littérature, design d'expériences | LLM + outils |
| **Quant Developer** | code, tests, stratégies, backtests | LLM + sandbox |
| **Data Engineer** | ingestion, nettoyage, qualité, snapshots | code + alertes LLM |
| **Portfolio Manager** | allocation, sizing, rebalancement | code + explication LLM |
| **Risk Manager** | exposition, drawdown, corrélation, stress, kill switch | code avec **droit de veto** |
| **Trader / Execution** | ordres paper, fills, slippage, coûts | paper broker code + résumé LLM |
| **Performance Analyst** | métriques, attribution, rapports | code + rapport LLM |
| **Compliance / Auditor** | audit trail, reproductibilité, disclaimers | code + rapport LLM |

### Principe

- Le **Risk Manager** ne doit jamais être un LLM qui décide seul.
- Le **Trader** ne peut créer que des ordres paper.
- Le **Compliance** vérifie les garde-fous et les disclaimers.
- Le **Researcher** et le **Developer** sont les deux vrais agents LLM.
- Les autres sont des services déterministes avec une interface en langage naturel.

---

## 4. Le mode Kraken

**Kraken** est le nom interne du mode de raisonnement par défaut.

### Politique

1. comprendre l'intention ;
2. choisir le ou les profils concernés ;
3. appeler les outils ;
4. vérifier que chaque chiffre vient d'un outil ;
5. répondre avec les `run_id` et les sources ;
6. si incertain → question ou refus.

### Budgets

- 8 appels d'outils maximum ;
- 60 secondes maximum ;
- tokens limités par requête ;
- pas de récursion infinie ;
- fallback si le modèle est rate-limité.

### Modes

| Mode | Usage |
|---|---|
| `kraken` | défaut : plan + outils + vérification |
| `fast` | réponse courte, un seul outil |
| `deep` | plus d'étapes, plus de vérification |
| `research` | RAG + backtests + rapport |

### Entraînement

**Aucun entraînement en v1.** Le mode Kraken s'écrit en Python, se teste sur 30 à 50 tâches, et s'améliore par itération. Le fine-tuning ne devient intéressant qu'avec des milliers de traces de qualité et un gain mesurable.

---

## 5. Architecture

```text
Next.js (web)
   │ REST + SSE
FastAPI
   ├── Auth / quotas
   ├── Kraken supervisor
   │     ├── Researcher agent
   │     ├── Developer agent
   │     └── routage vers services
   ├── Quant engine
   │     ├── Strategy registry
   │     ├── Backtester
   │     ├── Risk engine (veto)
   │     ├── Paper broker
   │     └── Performance / attribution
   ├── Data service
   │     ├── yfinance
   │     ├── Binance / Kraken via ccxt
   │     ├── cache + snapshots
   │     └── qualité
   ├── Model gateway
   │     ├── providers
   │     ├── fallback
   │     └── usage / quotas
   ├── PostgreSQL
   └── Object storage
```

### Règles

- L'agent n'appelle **jamais** Binance/Kraken directement : il passe par le `DataService`.
- L'agent n'exécute **jamais** un ordre réel : il passe par le `PaperBroker`.
- Chaque appel d'outil est validé par Pydantic, loggé et reproductible.
- Le Risk Manager peut veto une décision.
- Le code généré par l'agent est exécuté dans une sandbox, jamais directement.

---

## 6. Données

### Sources v1

| Source | Usage | Limites |
|---|---|---|
| **Binance** | crypto OHLCV | rate limits, conditions d'utilisation |
| **Kraken** | crypto OHLCV | rate limits, conditions d'utilisation |
| **yfinance** | actions/ETF, prototypage | non officiel, rate limits, usage commercial limité |

### Règles

- Cache local en PostgreSQL/Parquet.
- `snapshot_id` pour chaque backtest.
- Qualité : trous, doublons, outliers, bougies stale, pannes d'exchange, changements de symboles.
- Point-in-time pour les actions ; attention au survivorship bias.
- Ne pas redistribuer les données brutes sans autorisation.
- Pour la production : prévoir un provider payant (Polygon, Tiingo, Kaiko, CryptoCompare, etc.).

---

## 7. Modèles AI

### Modèles gratuits possibles

| Usage | Gratuit | Payant plus tard |
|---|---|---|
| Superviseur / planification | Gemini Flash, Groq Llama 3.3 70B, OpenRouter free | Claude, GPT, Gemini Pro |
| Tool calling | Gemini Flash, Groq, Mistral | Claude, GPT |
| Code | Qwen Coder, DeepSeek Coder | Claude, GPT |
| Résumés / rapports | petit modèle rapide | modèle économique |
| Embeddings / RAG | free tiers | payant |

### Critères

- fiabilité du tool calling ;
- respect du JSON ;
- contexte suffisant ;
- latence ;
- rate limits ;
- coût ;
- politique de données.

### Design

- modèle **par défaut gratuit** ;
- **fallback** automatique ;
- **model picker** dans l'interface ;
- **quotas** par utilisateur ;
- **tracking des tokens** ;
- **BYOK** plus tard.

> Les noms et quotas changent vite. Toujours garder un fallback.

---

## 8. Quotas et gratuité

### v1 gratuite

| Limite | Valeur proposée |
|---|---|
| Messages | 20/jour |
| Backtests | 5/jour |
| Jobs concurrents | 1 |
| Tokens | 100k/jour |
| Univers | 8 actifs |
| Historique | 2 ans |
| Portefeuilles paper | 1 |

### Implémentation

- table `usage_events` (user_id, date, type, count) ;
- compteurs Redis pour la vitesse ;
- middleware FastAPI qui renvoie `429` avec l'heure de reset ;
- barre d'usage dans l'interface ;
- vérification email, rate limit par IP, codes d'invitation ;
- file d'attente pour les backtests lourds.

### Paiements

- **Pas de paiement avant 18 ans.**
- À 18 ans : Paddle ou Lemon Squeezy (merchant of record, TVA gérée) ou Stripe.
- Vérifier les conditions d'âge ; un parent/tuteur ou une structure peut être nécessaire.
- Rester en **paper/éducation**, pas en conseil financier.

---

## 9. Périmètre v1

### Inclus

- crypto daily, 8 actifs ;
- 3 stratégies : trend, XS momentum, vol targeting ;
- backtest + métriques + walk-forward ;
- risk engine + paper broker ;
- agent CLI avec 6 outils ;
- modèle gratuit + fallback ;
- quotas ;
- rapports et mémo de séance ;
- interface web : chat, backtests, stratégie, portefeuille paper.

### Exclus

- trading réel ;
- HFT, market making, stat arb ;
- options complexes ;
- multi-marchés au départ ;
- paiements ;
- multi-agent swarm ;
- exécution de code non sandboxé ;
- promesses de rendement.

---

## 10. Roadmap

### 0–2 semaines

- carte des 8 profils ;
- 3 user stories ;
- 30 tâches d'évaluation ;
- schéma de données ;
- squelette CLI.

### 2–6 semaines

- DataService : Binance/Kraken, cache, qualité ;
- Quant engine : 3 stratégies, backtest, risk, paper ;
- CLI agent avec Kraken policy et 6 outils ;
- un modèle gratuit ;
- évaluation.

### 6–12 semaines

- FastAPI + PostgreSQL + auth ;
- Next.js : chat, backtests, stratégie, portefeuille ;
- quotas ;
- déploiement ;
- 10 bêta-testeurs.

### 3–6 mois

- itération ;
- model picker ;
- rapports ;
- plus de stratégies ;
- feedback ;
- préparation des paiements pour 18 ans.

---

## 11. Idées / backlog

### Produit

- backtests auditables (`run_id`, snapshot, version du code, hash des paramètres) ;
- Risk Manager avec veto ;
- Strategy registry ;
- Data doctor ;
- Strategy doctor (overfitting, turnover, concentration, look-ahead) ;
- Assumption ledger ;
- templates : trend vs mean reversion, risk review, weekly memo, data audit ;
- model picker ;
- usage dashboard ;
- eval harness ;
- admin kill switch ;
- liens de backtest reproductibles ;
- export notebook ;
- galerie communautaire modérée ;
- API ;
- mémo hebdomadaire ;
- comparaison de 3 stratégies ;
- explication des métriques ;
- dashboard paper ;
- alertes ;
- BYOK ;
- waitlist + invitations ;
- bouton feedback ;
- roadmap publique.

### Technique

- cache des backtests par hash ;
- versions de stratégies ;
- snapshots de données ;
- logs structurés ;
- tracing des appels d'outils ;
- tests de non-régression ;
- CI/CD ;
- sauvegardes ;
- rate limits ;
- sandbox ;
- observabilité (Sentry, uptime).

---

## 12. Risques et pièges

- 8 LLM agents dès le départ → coût, latence, chaos.
- Vouloir entraîner un modèle → distraction.
- yfinance en production → fragile et juridiquement risqué.
- Paiements à 16 ans → problème d'âge/légal.
- Trading réel → régulation et risque.
- « Tous les types de quant » → scope creep.
- Exécuter du code généré par l'LLM sans sandbox → danger.
- Fournir les clés Binance/Kraken à l'agent → jamais.
- Promettre des rendements → interdit et malhonnête.
- Utiliser « Kraken » comme nom public → marque déposée.
- Négliger l'école et le sommeil → burnout.

---

## 13. Questions ouvertes

1. Nom public du produit.
2. Premier client exact : retail crypto ? étudiant ? développeur ?
3. Modèle gratuit par défaut.
4. Provider de données pour la production.
5. Hébergement : Fly.io, Render, Railway, VPS ?
6. Prix à 18 ans.
7. Mode Developer en v1 ou plus tard ?
8. Chat classique ou « notebook » avec cellules ?
9. Compte obligatoire ou mode invité ?
10. Stratégie à mettre en avant dans la démo.

---

## 14. État du bot Meridian au 20 novembre 2026

### Cloud

- Run #2 : **succès**, déclenché par le schedule (créé à 07:34 UTC, retardé par GitHub).
  - https://github.com/adamsrolanda7-cpu/meridian/actions/runs/44695980342
- Dernière bougie : `2026-11-19`.
- Séances : 5.
- Equity : 100 000 $.
- Positions : 0.
- Intentions : 0.
- Régime : `TREND_DOWN`.
- Monitor : `WATCH` (historique court ; Sharpe rolling demande 60 jours).
- Kill switch : inactif.
- Décision : rester en cash, car BTC < EMA200, EMA50 < EMA200, ADX 42, breadth 0 %.

### Local

- État local : `2026-11-15`.
- Données locales : `2026-11-15`.
- Dashboard : `AUTO ON · FETCH OFF`.
- Le dashboard local ne télécharge pas les données par défaut ; il affiche l'état local.
- Monitor local recalculé le 20 nov : `ALERT` fraîcheur 5 j (seuil 3 j). Ce n'est pas un kill switch : les données locales sont périmées. Le fichier `runs/monitor.json` en cache date du 17 nov et affiche encore `WATCH` / stale 3.
- Le bot cloud et le bot local sont deux états séparés.

### Commandes utiles

```bash
cd /root/meridian
python3 -m meridian status
python3 -m meridian monitor
cat runs/paper/memo.md
tail -n 5 runs/paper/journal.jsonl
python3 -m meridian live --plain --no-auto-run --no-write-monitor
```

Synchroniser l'état cloud :

```bash
git fetch origin meridian-state
git archive origin/meridian-state runs | tar -x
```

Relancer localement :

```bash
python3 -m meridian download --force
python3 -m meridian paper
python3 -m meridian monitor
```

> Ne pas faire tourner le bot local et le bot cloud comme deux sources de vérité. Choisir l'un ou l'autre.

---

## 15. Décisions à prendre

- [ ] Valider le positionnement « AI research copilot for crypto daily strategies ».
- [ ] Valider la niche crypto daily, long-only spot.
- [ ] Valider les 3 stratégies v1.
- [ ] Valider les quotas.
- [ ] Choisir le nom public.
- [ ] Choisir le modèle gratuit par défaut.
- [ ] Choisir le provider de données de production.
- [ ] Décider si le mode Developer est en v1 ou plus tard.

---

## 16. Prototype CLI `quant-agent` (20 novembre 2026)

Le premier livrable du copilote est un CLI local, dans `quant_agent/`.
Il réutilise le moteur Meridian et n'envoie aucun ordre réel.

### Périmètre

- Provider `mock` par défaut, sans clé API, pour fonctionner hors ligne.
- Provider OpenAI-compatible optionnel : OpenRouter, Groq, Ollama, OpenAI.
- Deux presets : `trend_momentum` (défaut) et `mean_reversion`.
- Huit outils déterministes, validés avant exécution.
- Backtests reproductibles, cache local, quotas journaliers et audit JSONL.
- Paper trading en lecture seule ; le Risk Manager garde son veto.

### Commandes

```bash
python3 -m quant_agent ask "Fais un backtest trend momentum sur 2 ans"
python3 -m quant_agent chat
python3 -m quant_agent backtest --strategy mean_reversion
python3 -m quant_agent compare <run_a> <run_b>
python3 -m quant_agent show latest
python3 -m quant_agent portfolio
python3 -m quant_agent risk
python3 -m quant_agent usage
python3 -m quant_agent tools
```

Après `python3 -m pip install -e .`, les mêmes commandes sont disponibles via
`quant-agent`. La documentation complète est dans
[`docs/QUANT_AGENT_CLI.md`](QUANT_AGENT_CLI.md).

### Outils

| Outil | Rôle |
|---|---|
| `list_strategies` | Liste les presets et le défaut. |
| `run_backtest` | Backtest déterministe, cache prioritaire. |
| `get_backtest` | Détail d'un run, `latest` accepté. |
| `compare_backtests` | Métriques, deltas, corrélation. |
| `get_paper_portfolio` | Lecture seule du paper. |
| `get_risk_report` | Checks Meridian et kill switch. |
| `explain_metric` | Définition, lecture et limite d'une métrique. |
| `get_usage` | Quotas locaux du jour. |

### Politique Kraken

| Mode | Appels d'outils | Étapes | Durée |
|---|---:|---:|---:|
| `fast` | 2 | 6 | 75 s |
| `kraken` | 8 | 12 | 180 s |
| `deep` | 12 | 20 | 300 s |

Chaque appel est audité dans `runs/agent/audit.jsonl` avec le run_id, la
durée, le mode et le provider. Le cache ne consomme pas de quota de backtest.

### Quotas locaux

- 20 messages / jour ;
- 5 backtests / jour ;
- 100 000 tokens / jour ;
- réinitialisation à `00:00 UTC`.

Ces quotas sont un contrat d'usage local. Ils devront être déplacés côté
serveur pour la v1 publique, avec authentification.

### Validation

- Suite complète : **96 tests passent**.
- `tests/test_quant_agent.py` couvre les presets, le cache, le routage mock,
  la validation, les quotas, la boucle d'agent et l'audit.
- Provider OpenAI-compatible testé avec un faux serveur local : deux requêtes,
  schéma d'outil présent, résultat d'outil bien renvoyé.
- Deux backtests de référence sur le cache local au 15 novembre 2026 :
  - `trend_momentum` : +18,4 %, Sharpe 0,63, max drawdown -13,1 % ;
  - `mean_reversion` : +2,3 %, Sharpe 0,16, max drawdown -16,3 %.
  Ces chiffres illustrent le fonctionnement du prototype, pas une
  recommandation.
- Le test de stress de coûts était instable parce que le panel synthétique se
  terminait au jour courant. La date de fin des fixtures est désormais figée
  au 30 juin 2026.

### Limites

- Pas d'API, pas d'interface web, pas de PostgreSQL.
- Quotas locaux, sans authentification ni anti-abus.
- Deux stratégies seulement, pas de génération de code libre.
- Le mode Developer sandboxé n'est pas encore implémenté.
- Les backtests ne modélisent ni carnet, ni latence, ni fills partiels.

### Suite

1. Exposer les outils via FastAPI.
2. Ajouter l'authentification et les quotas serveur.
3. Construire l'interface Next.js.
4. Enrichir le registre de stratégies avec validation OOS.
5. Synchroniser le paper cloud et local sans double source de vérité.

---

## 17. Annexe — environnement de développement

- Windows 11 + WSL2 Ubuntu 24.04 LTS (ou macOS).
- VS Code + extension WSL.
- OpenCode dans le terminal (pas une extension VS Code).
- Python 3.12, venv, FastAPI, PostgreSQL.
- Node.js + Next.js pour l'interface.
- Git + GitHub.
- Pas de GPU local au départ ; API pour les modèles.
- Rust plus tard, pour les composants critiques.

---

*Document de travail. Aucun ordre réel n'est envoyé. Les performances passées ne préjugent pas des performances futures.*
