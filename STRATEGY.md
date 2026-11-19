# Meridian — document de stratégie

**Version du mandat :** 2.0 · **Dernière mise à jour :** 2026-11-17
**Statut :** recherche et paper trading uniquement. Aucun ordre réel.

Ce document décrit la logique de décision, les hypothèses, les paramètres
figés et les règles de gouvernance du desk. Il doit être lu avec le
`README.md`, qui contient les résultats consolidés et les commandes.

---

## 1. Mandat

### 1.1 Objectif

Construire un système systématique multi-actifs capable de :

1. capter la persistance de tendance et les cassures de canal sur les
   crypto-monnaies majeures ;
2. surpondérer les actifs les plus forts en momentum relatif ;
3. **préserver le capital** dans les régimes défavorables, avec une
   priorité explicite à la réduction du drawdown sur la recherche de
   rendement.

Le sleeve mean-reversion reste dans le code mais il est **suspendu** dans le
mandat 2.0 : il n'a pas d'edge démontré sur cet univers. Il ne fait donc pas
partie des objectifs actifs.

Le système n'essaie pas de battre BTC en rendement absolu. Il cherche un
profil rendement/risque supérieur, mesuré par le Sharpe, le Calmar et le
drawdown, avec une exposition moyenne faible.

### 1.2 Contraintes non négociables

| Contrainte | Valeur | Statut |
|---|---|---|
| Univers | BTC, ETH, SOL, LTC, XRP, BNB, LINK, AVAX (spot USDT) | figé |
| Direction | long-only | figé |
| Levier | interdit | figé |
| Produits dérivés | interdits | figé |
| Short | interdit (`allow_short=false`) | figé |
| Exposition brute max | 95 % | figé |
| Cash minimum | 5 % | figé |
| Poids max par actif | 35 % | figé |
| Volatilité cible | 22 % annualisée | figé |
| Mode d'exécution | `paper` uniquement | figé |
| Broker | aucun | figé |
| Mean-reversion | suspendue (`enabled=false`) | figé |
| Hard stop catastrophique | désactivé (`hard_stop_pct=0`) | figé |
| Override baissier précoce | désactivé (`bear_breadth=0`) | figé |

Toute modification de ces contraintes constitue un **nouveau mandat** : elle
exige une nouvelle période de validation, un nouveau hash de configuration et
une réinitialisation explicite du paper.

### 1.3 Ce que le système n'est pas

- Ce n'est pas un market maker, ni un arbitragiste, ni un système haute
  fréquence.
- Ce n'est pas un modèle d'apprentissage automatique : les agents sont
  déterministes et lisibles.
- Ce n'est pas un conseil en investissement.
- Ce n'est pas un connecteur broker : le paper est un replay, pas une
  exécution réelle.

---

## 2. Univers et données

### 2.1 Données

| Élément | Choix |
|---|---|
| Source | API publique Binance (`data-api.binance.vision`) |
| Fréquence | bougies quotidiennes UTC |
| Bougies utilisées | clôturées uniquement |
| Clé API | aucune |
| Cache | CSV locaux dans `data/` |
| Contrôle qualité | lignes, trous, doublons, OHLC cohérent, staleness, ADV20 |

Période disponible : BTC/ETH/LTC/XRP/BNB depuis 2019-01-01, LINK depuis
2019-01-16, SOL depuis 2020-08-11, AVAX depuis 2020-09-22. Dernière bougie au
moment de la rédaction : 2026-11-15.

### 2.2 Biais de sélection de l'univers

L'univers à 8 actifs est **choisi a posteriori**. Il ne contient que des actifs
qui ont survécu, qui sont devenus liquides et qui ont été demandés
explicitement (XRP, BNB, LINK, AVAX). Ce biais de survie et de sélection ne
peut pas être éliminé par le backtest : il faut le garder en tête en lisant les
résultats. Un univers étendu incluant des actifs morts ou delistés donnerait
des chiffres différents, probablement plus faibles. Le fait que les quatre
nouveaux actifs apportent environ 49 % du PnL net est une bonne nouvelle pour
la diversification, mais aussi un signe de concentration ex post.

### 2.3 Causalité

Chaque feature à la date `t` n'utilise que les bougies `≤ t`. Les tests
vérifient cette propriété par troncature d'historique et par perturbation des
prix futurs. La décision est prise sur la clôture `t` ; l'exécution a lieu à
l'ouverture `t+1`. Aucun prix futur n'entre dans le calcul du signal, du
régime, de la volatilité ou de la covariance.

---

## 3. Architecture de décision

```
Données ─► FeatureStore ─► RegimeAgent
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
         TrendAgent     MeanRevAgent    CrossSectionalAgent
              └───────────────┼────────────────┘
                              ▼
                       PortfolioAgent
                (blend, score, inverse-vol, caps, vol target)
                              ▼
                        RiskAgent
                (stops, gouverneur, veto, cooldowns)
                              ▼
                      ExecutionAgent
                (t+1 open, frais, slippage, impact)
                              ▼
                      SupervisorAgent
                (invariants, memo, journal)
```

Tous les agents sont déterministes. Le même triplet
`(configuration, données, date)` produit toujours la même décision. C'est la
condition de l'auditabilité et de la reproductibilité du paper.

---

## 4. Régime de marché

### 4.1 Rôle

Le régime n'est **pas un signal d'achat**. C'est un interrupteur de risque
qui décide quels sleeves ont le droit de s'exprimer. En `TREND_DOWN` et
`CRISIS`, le portefeuille cible est 100 % cash.

### 4.2 Calcul

Ancre : BTC. Largeur : les huit actifs.

- `breadth` = fraction des actifs dont la clôture est au-dessus de l'EMA 100.
- `vol_pct` = percentile glissant sur 365 jours de la volatilité réalisée
  30 jours de BTC (minimum 90 observations).
- `dd90` = drawdown de BTC par rapport à son plus haut 90 jours.
- `mom20` = rendement de BTC sur 20 jours.

Les règles, appliquées dans cet ordre de priorité :

| Régime | Condition |
|---|---|
| `CRISIS` | `vol_pct ≥ 0,90` **et** (`dd90 ≤ -15 %` **ou** `mom20 ≤ -10 %`) |
| `TREND_UP` | `close > EMA200` **et** `EMA50 > EMA200` **et** `ADX14 ≥ 15` **et** `breadth ≥ 0,50` |
| `TREND_DOWN` | `close < EMA200` **et** `EMA50 < EMA200` **et** `ADX14 ≥ 15` **et** `breadth ≤ 0,50` |
| `RANGE` | tout le reste |
| `WARMUP` | EMA200 indisponible |

Le label n'est changé qu'après **2 séances consécutives** dans le nouveau
régime (`confirm_bars=2`). Ce filtre réduit le bruit autour des seuils.

Un override baissier précoce a été testé : si BTC est sous son EMA200 et que
la largeur est sous un seuil, on force `TREND_DOWN` sans attendre le
croisement EMA50/EMA200. Sur l'univers à 8 actifs, il dégrade le rendement
sans améliorer le drawdown. Il est donc **désactivé** (`bear_breadth=0`). Le
régime standard suffit ; c'est une simplification volontaire.

### 4.3 Conséquence

| Régime | Sleeves autorisés | Exposition cible |
|---|---|---|
| `TREND_UP` | tendance 55 %, XS 35 %, mean-rev 10 % (suspendue) | selon signaux |
| `RANGE` | tendance 20 %, XS 15 %, mean-rev 65 % (suspendue) | selon signaux |
| `TREND_DOWN` | aucun | 0 % |
| `CRISIS` | aucun | 0 % |
| `WARMUP` | aucun | 0 % |

La mean-reversion étant suspendue, son poids de blend n'est pas réalloué aux
autres sleeves : en `RANGE`, le composite maximal vient donc de 20 % de
tendance + 15 % de XS, ce qui réduit l'exposition. C'est un choix
**conservateur** : on préfère under-investir plutôt que réallouer
mécaniquement un poids dont la thèse n'est plus validée.

Le régime `TREND_DOWN` est un filtre de tendance baissière prolongée ; le
régime `CRISIS` est un filtre de choc de volatilité. Ils peuvent se
chevaucher ; `CRISIS` a priorité.

---

## 5. Sleeves de signal

Chaque sleeve produit un score dans `[0, 1]` et une confiance dans `[0, 1]`.
Les scores sont causaux et vectorisés.

### 5.1 Tendance / TSMOM

**Thèse :** les tendances crypto persistent à l'horizon de plusieurs
semaines à plusieurs mois ; les cassures de canal et l'empilement de moyennes
mobiles sont des confirmations.

Features :

- `mom90 = close_t / close_{t-90} - 1`
- `vol30` = volatilité réalisée annualisée sur 30 jours
- `risk_mom = mom90 / (vol30 × √(90/365))`
- `trend_stack = +1` si `close > EMA50 > EMA200`, `-1` si
  `close < EMA50 < EMA200`, `0` sinon
- `breakout = 1` si `close_t > max(high_{t-55..t-1})`, sinon `0`
  (le canal Donchian exclut la bougie courante)

Score brut :

```
raw = 0.50 × tanh(risk_mom / 1.2)
    + 0.30 × trend_stack
    + 0.20 × breakout
```

Le score n'est conservé que si `close > EMA200` ; sinon il est forcé à zéro.
Le sleeve est donc structurellement long-only et plat sous la tendance
longue.

Confiance :

```
agreement = 0.45 × 1[trend_stack > 0]
          + 0.30 × breakout
          + 0.25 × 1[risk_mom > 0]
conf = 0.35 × min(ADX/35, 1)
     + 0.35 × min(|risk_mom|/1.5, 1)
     + 0.30 × agreement
```

### 5.2 Mean-reversion (suspendue)

**Statut : suspendue par `strategy.meanrev.enabled=false`.** Le code et les
formules restent dans le dépôt pour un redesign validé, mais le sleeve ne
produit aucun score dans le mandat 2.0. Il n'apparaît donc pas dans les
résultats finaux.

**Pourquoi :** sur l'univers à 8 actifs, le sleeve est perdant en standalone
et sa désactivation améliore l'IS, la période complète et l'OOS. Il achetait
des couteaux qui tombaient, notamment quand le régime restait `RANGE` alors
que BTC était sous son EMA200 et que la largeur du marché s'effondrait. Un
filtre de breadth et un filtre « actif au-dessus de son EMA200 » ont été
testés ; ils réduisent le problème mais dégradent encore le portefeuille.
La conclusion simple : ne pas garder un sleeve qui n'a pas d'edge démontré.

**Réactivation :** elle exige un nouveau design (par exemple un vrai filtre
de capitulation avec confirmation), une validation IS dédiée, puis une seule
lecture OOS, et une réinitialisation du paper. Ce n'est pas un réglage.

**Thèse historique :** en marché sans direction, les excès baissiers
temporaires sur des actifs liquides reviennent vers leur moyenne. Le sleeve
était volontairement rapide, avec un stop serré, car le risque de couteau qui
tombe est réel.

Conditions d'entrée :

```
z20 = (close - moyenne20) / écart-type20
RSI14 ≤ 38
z20 ≤ -1.8
close > 0.75 × EMA200          # filtre anti-couteau
vol30 < 100 %                  # pas de régime de volatilité extrême
```

Score : `clip((-z20 - 0.8) / 2.0, 0, 1)` lorsque le setup est vrai, sinon 0.
Confiance : `0.50 + 0.50 × clip((|z20| - 1.8) / 1.5, 0, 1)`.

Sortie : `z20 ≥ 0.25` **ou** `RSI14 ≥ 55` **ou** détention ≥ 10 séances.
Le stop est à **2 ATR** sous l'entrée et est vérifié **intrabar** (low de la
bougie), contrairement aux sleeves de tendance.

Le sleeve était actif en régime `RANGE`, avec 65 % du blend, réduit à 10 %
en `TREND_UP` et à 0 % en risk-off. Ces poids restent dans le code mais sont
neutralisés par le score nul du sleeve suspendu.

### 5.3 Momentum transversal

**Thèse :** parmi les majors, les deux actifs les plus forts en momentum
ajusté du risque continuent de surperformer à court terme.

Éligibilité : `risk_mom > 0` **et** `close > EMA100`.

- `dispersion` = écart-type transversal des `risk_mom` éligibles.
- Si `dispersion < 0.15`, le sleeve ne prend rien (marché trop homogène).
- Rang 1 : score 1.0 ; rang 2 : score 0.65 ; les autres : 0.
- Confiance : `clip(dispersion / 0.8, 0, 1)`.

Le sleeve est actif à 35 % en `TREND_UP` et 15 % en `RANGE`.

### 5.4 Combinaison

Pour chaque actif :

```
composite = Σ_sleeve  blend[sleeve] × max(0, score_sleeve)
conviction = Σ (contribution × confiance) / Σ contribution
si use_confidence : composite *= 0.60 + 0.40 × conviction
composite = clip(composite, 0, 1)
```

Le blend par régime est la seule pondération ex ante :

| Régime | Tendance | XS momentum | Mean-reversion |
|---|---:|---:|---:|
| `TREND_UP` | 0.55 | 0.35 | 0.10 (suspendue) |
| `RANGE` | 0.20 | 0.15 | 0.65 (suspendue) |
| `TREND_DOWN` | 0 | 0 | 0 |
| `CRISIS` | 0 | 0 | 0 |

La pondération mean-reversion est conservée pour documenter l'ancien mandat,
mais elle est multiplicative d'un score nul : elle ne contribue pas au
composite. Le poids non utilisé n'est pas réalloué.

### 5.5 Bandes d'entrée et de sortie

| Situation | Règle |
|---|---|
| Actif non détenu | entrée seulement si `composite ≥ 0.25` |
| Actif détenu | sortie si `composite < 0.12` |
| Actif détenu entre les deux | conservé |

Ces bandes asymétriques évitent les allers-retours autour d'un seuil unique
et réduisent le turnover. La durée minimale de détention est de **5 séances**,
sauf stop, passage en `TREND_DOWN`/`CRISIS` ou liquidation par le gouverneur.

---

## 6. Construction du portefeuille

### 6.1 Pondération inverse-volatilité

Pour chaque actif avec `composite > 0.05` :

```
raw_a = composite_a / max(vol30_a, 0.20)
```

Le plancher de volatilité à 20 % évite qu'un actif artificiellement calme
domine le portefeuille. Les poids sont normalisés pour que la somme brute
atteigne `gross_cap = min(0.95, 1 - min_cash) = 0.95`, avant plafonds.

### 6.2 Plafonds et redistribution

Plafond par actif :

```
cap_a = min(0.35, asset_caps[a], 0.01 × ADV20_a / equity)
```

Le plafond de liquidité limite une position à **1 % de l'ADV 20 jours**. Les
poids excédentaires sont redistribués vers les actifs non plafonnés, au
prorata de leur `raw`, en au plus 8 itérations. Si plus aucun actif libre,
le portefeuille reste sous son gross cible : le cash est une position.

### 6.3 Ciblage de volatilité

Covariance :

- fenêtre 90 jours ;
- EWMA avec `λ = 0.97` ;
- shrinkage de 30 % vers la diagonale ;
- annualisation × 365.

Volatilité du portefeuille :

```
σ_p = sqrt(w' Σ_ann w)
```

Si `σ_p > 22 %`, tous les poids sont réduits par `22 % / σ_p`. Le système ne
**ré-augmente jamais** l'exposition pour atteindre la cible : le vol targeting
est un plafond, pas un levier. C'est pour cette raison que la volatilité
réalisée (16,1 %) est inférieure à la cible (22 %) : l'exposition moyenne
n'est que de 13,9 %.

### 6.4 Bande morte de rééquilibrage

Un actif n'est rééquilibré que si l'écart entre poids cible et poids courant
dépasse **2,5 % du capital**. Cela évite de payer des frais pour une dérive
mineure.

### 6.5 Core BTC

Le paramètre `core_btc` existe mais vaut **0 %** dans le mandat figé. Il
n'est pas utilisé. L'ajouter serait un changement de mandat, pas un réglage.

---

## 7. Gestion du risque

Le `RiskAgent` est le seul composant qui peut réduire ou bloquer une
décision. Il ne cherche pas le rendement.

### 7.1 Stops

| Sleeve | Stop initial | Vérification | Trailing |
|---|---|---|---|
| Tendance | 6 ATR sous l'entrée | clôture | `max(stop, close - 6 ATR)` |
| XS momentum | 6 ATR sous l'entrée | clôture | `max(stop, close - 6 ATR)` |
| Mean-reversion | 2 ATR sous l'entrée | intrabar (low) | non |

Règles complémentaires :

- **Gap à l'ouverture** : si l'ouverture est sous le stop, la position est
  vendue à l'ouverture (avec slippage), avant toute autre décision.
- **ATR indisponible** : fallback à 3 % du prix pour le calcul du stop.
- **Le trailing ne redescend jamais.**
- **Cooldown de 5 séances** après un stop : l'actif ne peut pas être
  racheté immédiatement.
- Le stop de tendance est volontairement sur clôture, pour éviter d'être
  sorti par une mèche. Le coût de ce choix est un risque de gap plus élevé,
  documenté plus bas.
- Un **hard stop catastrophique** optionnel (`hard_stop_pct`) a été testé
  entre 15 % et 30 % : il dégrade le drawdown et le rendement sur cet univers.
  Il est **désactivé** (`hard_stop_pct=0`). Les stops 6 ATR restent la seule
  protection de tendance.

### 7.2 Gouverneur de drawdown

Le drawdown est mesuré sur l'equity, du plus haut historique au niveau
courant.

| Condition | Action |
|---|---|
| `DD ≤ -8 %` | exposition réduite de moitié (`scale = 0.5`) |
| `DD ≤ -12 %` et aggravation de 5 points vs dernier gel | gel de 25 séances, liquidation |
| Fin du gel | probation 40 séances à 25 % d'exposition |
| `DD > -8 %` | réarmement du gouverneur |
| Perte journalière ≤ -3 % | aucun **ajout** de risque ; les positions existantes peuvent être réduites |

Le gouverneur ne bloque pas à vie. Après un gel, il reprend à 25 % pendant
40 séances ; un nouveau gel n'est déclenché que si le drawdown s'aggrave
d'au moins 5 points depuis le dernier gel. Sans cette règle, le système
resterait bloqué sous -12 % et ne pourrait jamais se réarmer.

Les valeurs 25/40 ont été retenues après comparaison avec 20/30 : elles
améliorent l'IS, la période complète et le drawdown. Ce n'est pas un réglage
sur l'OOS, mais un choix de robustesse du gouverneur.

### 7.3 Contrôles pré-exécution

Chaque ordre est vérifié :

- actif dans l'univers et libellé en USDT ;
- poids ≤ 35 % ;
- position ≤ 1 % de l'ADV 20 jours ;
- prix disponible et fini ;
- equity > 0 ;
- cash minimum de 5 % après achat ;
- pas de short, pas de levier.

### 7.4 Ce que le gouverneur ne garantit pas

Le drawdown maximal observé (-14,6 %) dépasse la limite soft de -12 %. Trois
raisons :

1. les gaps d'ouverture peuvent être plus violents que le stop ;
2. le gouverneur réagit à la clôture, après le mouvement ;
3. les stops de tendance sont sur clôture, pas intrabar.

La limite de -12 % est donc un **seuil d'action**, pas une garantie. Le
Monte Carlo estime une probabilité de **61,8 %** de dépasser -20 % sur un
chemin de 7 ans (DD p5 -35,5 %). C'est un risque assumé et documenté, pas un
détail à masquer.

---

## 8. Exécution et coûts

### 8.1 Séquence

1. Décision sur la clôture `t`.
2. Exécution à l'ouverture `t+1`.
3. Ventes avant achats (libération de cash et réduction du risque d'abord).
4. Achats triés par delta de poids décroissant.
5. Contrôle du budget : `cash - 5 % × equity`.

### 8.2 Coûts

| Composante | Hypothèse |
|---|---|
| Frais taker | 8 bps |
| Slippage de base | 5 bps |
| Impact | `min(150, 20 × √((notionnel / ADV) / 1 %))` bps |
| Participation max par ordre | 5 % de l'ADV |
| Position max | 1 % de l'ADV |
| Notionnel minimum | 100 $ |

L'impact est croissant avec la taille relative à l'ADV et plafonné à 150 bps.
Le stress de coûts multiplie les trois composantes (frais, slippage, impact)
par 1, 2, 3 et 5.

### 8.3 Ce que le modèle d'exécution ignore

- carnet d'ordres et profondeur réelle ;
- latence et files d'attente ;
- funding et coût de portage (pas de dérivés) ;
- spreads variables et ruptures de liquidité ;
- corrélations de slippage entre actifs ;
- règlement/livraison et risques de contrepartie.

Le backtest est une simulation crédible, pas une réplication de marché.

---

## 9. Protocole de validation

### 9.1 Séparation temporelle

Le mandat 2.0 a été figé avant l'exécution finale de recherche. La grille est
publiée comme sensibilité, pas comme sélection (`research.freeze_mandate=true`).

| Phase | Période |
|---|---|
| In-sample | 2019-07-19 → 2024-09-03 |
| Out-of-sample | 2024-09-04 → 2026-11-15 |

Le split est chronologique (70 / 30), pas aléatoire. **Honnêteté :** durant
l'exploration qui a conduit au mandat 2.0, l'OOS a été regardé, notamment pour
comparer les variantes mean-reversion, hard stop et gouverneur. L'OOS publié
n'est donc pas vierge. Il reste une validation utile, pas une preuve
statistique.

Deux mesures OOS sont publiées :

- **nouveau compte** : capital frais au début de l'OOS, positions à
  reconstruire ;
- **continu** : positions traversantes, rendements enchaînés, drawdown depuis
  le plus haut historique.

### 9.2 Grille de sensibilité

La grille ne touche pas aux formules alpha. Elle teste :

- `target_vol` ∈ {22 %, 26 %, 30 %}
- `stop_atr` ∈ {4, 5, 6, 7, 8} pour la sensibilité univariée, {4, 6, 8} dans
  la grille principale
- `rebalance_days` ∈ {7, 14}
- `entry_score` ∈ {0,15, 0,20, 0,25, 0,30} en univarié

Score de sélection historique :

```
score = Sharpe_IS + 0.25 × min(2, Sharpe_IS)
```

avec contraintes : drawdown > -22 %, turnover < 9x, au moins 10 trades clos.

Le mandat retenu est `target_vol=22 %`, `stop_atr=6`, `rebalance_days=14`,
`entry_score=0,25`. Il n'est pas le plus agressif ni celui qui maximise le
CAGR : c'est un compromis Sharpe / drawdown / simplicité, figé avant la
dernière exécution.

### 9.3 Walk-forward

Huit fenêtres semestrielles de 182 jours. Sur chaque fenêtre :

- **walk-forward optimisé** : les paramètres sont ré-optimisés sur le train,
  puis appliqués au test suivant sans retouche ;
- **walk-forward à mandat fixe** : le mandat publié est appliqué tel quel,
  sans ré-optimisation. C'est le contrôle le plus honnête et le seul qui ne
  peut pas être accusé de sélection adaptative.

Les rendements des fenêtres sont recollés. Les deux versions couvrent la même
période récente, à partir de fin 2022.

### 9.4 Monte Carlo

800 chemins, bootstrap par blocs de 20 jours pour préserver
l'autocorrélation et les clusters de volatilité. Le Monte Carlo ne prévoit
pas le futur : il estime la dispersion des résultats sous l'hypothèse que
l'échantillon est représentatif.

### 9.5 Stress et sensibilité

- coûts ×1, ×2, ×3, ×5 ;
- départs annuels 2020 → 2026 ;
- sensibilité à `target_vol`, `stop_atr`, `rebalance_days`, `entry_score`.

### 9.6 Limites statistiques

- 7,3 ans de données, 8 actifs, 248 trades clos : la puissance statistique
  reste faible, malgré l'élargissement de l'univers.
- Le walk-forward a 8 folds, dont 4 positifs. C'est mieux que 4 folds, mais
  la période de test est récente et difficile.
- L'OOS de 26 mois est une seule réalisation ; il ne prouve pas l'absence
  d'alpha, mais il ne prouve pas sa présence non plus.
- L'univers est choisi a posteriori et le biais de survie est plus fort avec
  8 actifs qu'avec 4.
- Les tests multiples (grille + variantes) augmentent le risque de sélection
  chanceuse. Le score pénalise le turnover et le drawdown, mais ne corrige
  pas formellement le multiple testing.
- Le bootstrap suppose que les blocs passés sont représentatifs. Ce n'est
  pas vrai en cas de changement structurel de marché.

---

## 10. Résultats du mandat figé

### 10.1 Performance complète

Période : 2019-07-20 → 2026-11-15 (7,3 ans), capital initial 100 000 $.

| Métrique | Meridian | BTC buy & hold |
|---|---:|---:|
| Equity finale | 296 290 $ | 608 470 $ |
| Rendement total | +196,3 % | +508,5 % |
| CAGR | +16,0 % | +28,0 % |
| Volatilité | 16,1 % | 59,9 % |
| Sharpe | 1,00 | 0,72 |
| Sortino | 0,88 | — |
| Calmar | 1,10 | 0,37 |
| Max drawdown | -14,6 % | -76,6 % |
| Jours sous l'eau | 821 | — |
| Exposition moyenne | 13,9 % | 100 % |
| Exposition brute max | 62,4 % | 100 % |
| Turnover annuel | 3,6x | — |
| Frais | 4 343 $ | — |
| Coûts totaux | 6 143 $ | — |
| Trades clos | 248 | — |
| Hit rate | 61,7 % | — |
| Profit factor | 2,69 | — |
| Beta vs BTC | 0,14 | 1,00 |
| Corrélation vs BTC | 0,51 | 1,00 |

Le desk gagne moins que BTC en absolu, mais avec 3,7 fois moins de volatilité
et un drawdown maximal 5,3 fois plus faible. Le beta de 0,14 montre qu'il ne
s'agit pas d'un proxy BTC déguisé.

### 10.2 In-sample / out-of-sample

| Phase | CAGR | Vol | Sharpe | Max DD | Turnover | Trades |
|---|---:|---:|---:|---:|---:|---:|
| In-sample (2019-07-19 → 2024-09-03) | +18,3 % | 16,4 % | 1,10 | -14,6 % | 3,5x | 178 |
| OOS nouveau compte (2024-09-04 → 2026-11-15) | **+9,5 %** | 15,3 % | **0,67** | -11,5 % | 4,4x | 93 |
| OOS continu (même période) | **+10,9 %** | 15,3 % | **0,75** | -13,4 % | n/a | n/a |
| Complète | +16,0 % | 16,1 % | 1,00 | -14,6 % | 3,6x | 248 |

L'OOS nouveau compte termine à **122 146 $**, soit **+22,1 %** sur 26 mois.
L'OOS continu termine à **+25,5 %**. Les deux mesures diffèrent parce que le
compte neuf doit reconstruire ses positions, alors que le compte continu les
traverse.

**Lecture honnête :** l'OOS est positif, mais il s'agit d'une seule
réalisation de 26 mois. Il a été regardé pendant l'exploration du mandat, donc
il n'est pas vierge. Le walk-forward, plus sévère, est nettement moins bon.

Contexte OOS :

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

Le desk a capté une partie de la hausse de XRP et BNB tout en évitant
l'effondrement de SOL, LINK et AVAX. C'est un résultat de sélection et de
protection, pas un beta crypto.

### 10.3 Validation complémentaire

| Test | Résultat |
|---|---|
| Walk-forward optimisé, 8 fenêtres de 182 j | 4/8 positives · CAGR 8,5 % · Sharpe 0,57 · DD -20,0 % |
| Walk-forward à mandat fixe, mêmes fenêtres | 4/8 positives · CAGR 9,3 % · Sharpe 0,61 · DD -19,9 % |
| Monte Carlo 800 chemins | CAGR p5/p50/p95 : 4,2 % / 16,3 % / 28,8 % |
| Monte Carlo drawdown | p5 -35,5 % · p50 -21,5 % · p95 -14,3 % |
| P(perte) | 1,1 % |
| P(DD < -20 %) | **61,8 %** |
| Coûts ×5 | CAGR 11,2 % · Sharpe 0,78 · DD -15,6 % |
| Départs 2020→2026 | CAGR +13,3 % à -3,0 % |

Le stress de coûts montre que le système ne dépend pas d'hypothèses de frais
irréalistes : même à ×5, le CAGR reste à deux chiffres. Le Monte Carlo montre
qu'un drawdown de 20-35 % reste possible malgré le gouverneur. La limite de
-12 % est un seuil d'action, pas une garantie.

Le walk-forward est le point faible : 4/8 folds positifs, CAGR 8,5-9,3 %,
DD -20 %. Il ne couvre que la période récente, plus difficile pour les
altcoins, et il ne valide pas le CAGR complet. C'est un avertissement à
garder visible.

### 10.4 Rendements annuels

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
régime. 2025-2026 est la période difficile : le desk reste défensif mais subit
quelques stops et faux départs. Un départ au 1er janvier 2026 donne -3,0 %.

### 10.5 Attribution

| Sleeve | Trades clos | PnL cumulé | Hit rate |
|---|---:|---:|---:|
| Tendance | 217 | +204 729 $ | 64,5 % |
| Momentum transversal | 31 | -6 349 $ | 41,9 % |
| Mean-reversion | 0 | 0 $ | — |

Le moteur de performance est la tendance. Le XSMOM est **négatif en
standalone**, mais son retrait dégrade fortement le portefeuille (CAGR 10,8 %
et OOS -1,4 %). Il apporte de la diversification et des signaux que la
tendance seule ne voit pas. C'est un point de surveillance, pas une réussite
à masquer.

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

Répartition des régimes : `TREND_UP` 42,5 %, `RANGE` 28,3 %, `TREND_DOWN`
23,3 %, `CRISIS` 5,9 %. Quatre halts du gouverneur : 2020-03-12, 2023-03-09,
2024-07-31, 2025-11-04.

### 10.6 Variantes testées

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

## 11. Paper trading

### 11.1 Principe

Le paper est un **replay déterministe**, pas un broker. À chaque exécution :

1. le moteur rejoue la stratégie depuis la date de départ avec les données
   réelles ;
2. les intentions de la veille sont exécutées à l'ouverture suivante par le
   même agent que le backtest ;
3. les nouvelles intentions sont produites pour l'ouverture suivante ;
4. l'état est persisté, le memo est écrit, le journal est complété.

Cette approche garantit que le paper et le backtest partagent exactement le
même code de décision. Elle évite les états incrémentaux fragiles.

### 11.2 Fichiers

| Fichier | Contenu |
|---|---|
| `runs/paper/state.json` | capital, date, positions, intentions, hash de config |
| `runs/paper/journal.jsonl` | journal append-only des séances |
| `runs/paper/memo.md` | memo de la dernière séance |
| `runs/paper/fills.csv` | fills du replay |
| `runs/paper/snapshot.json` | snapshot structuré pour le dashboard terminal |
| `runs/monitor.json` | dernier état de suivi, lisible par machine |
| `runs/monitor.md` | dernier état de suivi, lisible par humain |

### 11.3 Règles de gouvernance du paper

- Le hash de configuration est vérifié à chaque exécution. Un changement de
  configuration est **refusé** : `PaperError`.
- Toute valeur de `execution.mode` différente de `paper` lève
  `LiveTradingDisabled`.
- Un broker configuré est refusé.
- La réinitialisation est explicite (`--init --force`), jamais automatique.
- La réconciliation compare les intentions de la veille aux fills du
  lendemain. Un fill de rééquilibrage hors intention est signalé.
- `paper --verify` compare l'equity, le cash, les positions, les stops et les
  intentions au replay, sans modifier l'état. Une divergence au-delà de 1 %
  est une erreur à enquêter ; une nouvelle bougie non traitée est seulement un
  avertissement.
- `monitor` évalue les kill criteria et écrit `runs/monitor.*`. Un `ALERT`
  n'est jamais corrigé automatiquement.

Le paper a été réinitialisé le 2026-11-15 avec les 8 actifs : 100 000 $,
aucune position, aucune intention. Le régime est `RANGE`, la largeur est à
0 %, la mean-reversion est suspendue : le desk reste 100 % cash. C'est le
comportement attendu, pas un bug.

### 11.4 Ce que le paper ne prouve pas

- Il ne prouve pas que les ordres seraient exécutés au prix simulé.
- Il ne mesure pas la latence, le spread réel, ni l'impact de marché.
- Il ne remplace pas un relevé de broker.
- Il ne protège pas contre un bug de données ou une panne d'API.

Le paper est un test de cohérence et de discipline, pas une preuve de
rentabilité future.

### 11.5 Dashboard terminal

`python3 -m meridian live` affiche le desk dans le terminal, sans serveur web
ni navigateur. Le rendu est un plein écran `curses` rafraîchi en continu :

- état global `OK` / `WATCH` / `ALERT` et heure UTC ;
- dernière bougie, retard, source, compte à rebours de clôture ;
- régime, breadth, ADX, volatilité percentile ;
- equity, cash, exposition, drawdown, rendement ;
- signaux des trois sleeves et poids cibles ;
- positions, stops et intentions pour l'ouverture suivante ;
- contrôles du monitor et journal récent.

L'exécution automatique du paper n'a lieu que si la dernière bougie des données
est postérieure à `state.last_bar`. Le dashboard ne rejoue donc pas la même
séance en boucle. Il lit `runs/paper/snapshot.json` pour éviter de recalculer
tout le backtest à chaque rafraîchissement ; le monitor complet est recalculé
périodiquement ou manuellement.

`--window` tente d'ouvrir une nouvelle fenêtre de terminal (tmux, GNOME,
Konsole, xterm, etc.). Sur un serveur sans affichage graphique, il retombe
proprement sur le terminal courant. `--plain` et `--follow` fournissent une
sortie texte pour les journaux ou les environnements sans TTY.

Touches : `q` quitter, `r` rafraîchir, `p` exécuter le paper, `d` télécharger
les données, `a` auto, `f` fetch, flèches pour défiler.

Le dashboard est un outil de visualisation et d'exploitation. Il ne prend
aucune décision de marché et n'envoie aucun ordre réel.

---

## 12. Gouvernance et critères d'arrêt

### 12.1 Changement de mandat

Toute modification de paramètre suit ce protocole :

1. écrire la thèse et l'hypothèse testée ;
2. tester uniquement sur l'IS ;
3. si retenu, figer une nouvelle version de configuration ;
4. appliquer l'OOS **une seule fois** ;
5. archiver l'ancien état paper et réinitialiser explicitement ;
6. documenter la décision et ses risques.

Il est interdit de modifier un paramètre après avoir regardé l'OOS de la même
période. C'est la règle qui distingue une recherche d'un curve-fitting.

### 12.2 Critères d'arrêt (kill criteria)

Ces seuils sont implémentés dans `meridian/monitor.py` et publiés dans
`config/default.json` (section `monitor`). Un `ALERT` déclenche le kill switch.

**Risque**

- Drawdown courant ≤ **-12 %** → `WATCH`, réduction de risque déjà appliquée
  par le gouverneur.
- Drawdown courant ≤ **-20 %** → `ALERT`, arrêt du paper et revue complète du
  mandat.
- Trois gels de drawdown en 12 mois → retrait du mandat.
- Perte journalière ≤ -3 % suivie d'un non-respect du blocage des ajouts →
  incident critique.

**Performance**

- Sharpe glissant 90 jours ≤ **0** → `WATCH`.
- Sharpe glissant 90 jours ≤ **-0,5** après au moins 60 jours d'historique →
  `ALERT`, suspension et revue.
- Sous-performance vs panier équipondéré sur 12 mois avec drawdown supérieur →
  revue de la thèse, pas changement automatique.
- Profit factor glissant 50 trades < 1,0 **et** hit rate < 40 % → revue.

**Données et opérations**

- Données périmées > **3 jours**, trous > 2 % des bougies, ou prix aberrant
  non expliqué → aucun nouveau risque (`ALERT` sur la fraîcheur).
- Divergence de réconciliation paper/backtest > **1 %** → arrêt du paper et
  enquête.
- Hash de configuration différent de l'état paper → `ALERT` et exécution
  refusée (implémenté).
- Panne ou erreur d'API répétée → passer en mode dégradé, ne pas exécuter.

**Capacité**

- Ordre > 5 % de l'ADV ou position > 1 % de l'ADV → plafonnement
  (implémenté).
- Exposition brute > 95 % ou poids actif > 35 % → `ALERT` (implémenté).
- Turnover annualisé > 9x → `WATCH` (implémenté).
- Si les plafonds de liquidité contraignent plus de 20 % des ordres sur 3
  mois → réduire la taille ou élargir l'univers.

### 12.3 Revue périodique

| Fréquence | Revue |
|---|---|
| Quotidienne | données, régime, intentions, réconciliation |
| Hebdomadaire | positions, stops, exposition, PnL, écarts |
| Mensuelle | attribution, turnover, coûts, drawdown, respect du mandat |
| Trimestrielle | Sharpe glissant, walk-forward, stabilité des paramètres |
| Annuelle | thèse, univers, capacité, pertinence du mandat |

---

## 13. Limites connues

### 13.1 Limites de données

- Biais de survie et de sélection de l'univers, plus fort avec 8 actifs
  qu'avec 4 : tous les actifs choisis ont survécu et sont liquides.
- SOL n'existe que depuis août 2020, AVAX depuis septembre 2020, LINK depuis
  janvier 2019 : les historiques sont plus courts.
- Les données Binance ne représentent pas tout le marché crypto.
- Les prix sont des prix de clôture/ouverture agrégés, pas des prix
  exécutables.
- Les corrections de données et les changements de ticker ne sont pas
  traités.

### 13.2 Limites de modèle

- Les régimes sont des règles fixes, pas une inférence probabiliste.
- Les seuils (ADX 15, breadth 50 %, vol percentile 90 %) sont choisis ex
  ante et peuvent être fragiles.
- Le blend par régime est discrétionnaire dans sa conception, même s'il est
  déterministe dans son exécution.
- Le sleeve mean-reversion est suspendu, pas résolu : il reste perdant en
  standalone et n'a pas de validation sur l'univers à 8 actifs.
- Le momentum transversal n'a que 31 trades clos et un PnL négatif en
  standalone : peu de puissance, mais un rôle de diversification.
- La covariance EWMA peut sous-estimer les ruptures de corrélation.
- Le vol targeting ne scale pas à la hausse : le système peut sous-investir
  durablement.
- Le poids de la mean-reversion n'est pas réalloué : en `RANGE`, le
  portefeuille peut rester sous-investi.

### 13.3 Limites d'exécution

- Pas de carnet, pas de latence, pas de partial fills réalistes.
- Le slippage est une fonction déterministe de la taille, pas un modèle de
  marché.
- Les gaps peuvent dépasser les stops.
- Le gouverneur réagit après le mouvement.
- Le drawdown observé (-14,6 %) dépasse la limite soft (-12 %).

### 13.4 Limites statistiques

- 7,3 ans, 8 actifs, 248 trades : échantillon encore limité.
- OOS de 26 mois : une seule réalisation, déjà regardée pendant
  l'exploration.
- Walk-forward à 8 folds : 4 positifs seulement, DD -20 %.
- Tests multiples : grille + variantes, risque de sélection chanceuse.
- Le Monte Carlo suppose la stationnarité des blocs ; il donne 61,8 % de
  probabilité de DD < -20 %.
- La concentration du PnL sur BNB et SOL est un risque spécifique.

### 13.5 Limites opérationnelles

- Pas de connecteur broker, pas de clés, pas d'ordres réels.
- Pas de gestion d'incidents de production.
- Surveillance quotidienne, pas temps réel.
- Pas de modèle de contrepartie ni de règlement.
- Pas de cadre juridique, fiscal ou comptable.

---

## 14. Reproductibilité

```bash
cd /root/meridian

python3 -m meridian download              # données + contrôle qualité
python3 -m meridian research --tag research-8assets  # IS/OOS, WF, MC
python3 -m meridian backtest --tag final  # rapport + memo
python3 -m meridian paper --init --force
python3 -m meridian paper
python3 -m meridian paper --verify
python3 -m meridian monitor
python3 -m meridian live --plain
python3 -m meridian status
python3 -m pytest -o addopts="" -q
```

Le rapport complet est dans `runs/final/report.html`, copié dans
`reports/latest.html`. Les exports CSV/JSON sont dans `runs/final/`.
Le suivi opérationnel est dans `runs/monitor.json` et `runs/monitor.md`.

La configuration est figée dans `config/default.json`. L'ancien état 4 actifs
est archivé dans `runs/archive/4assets-final/`.

---

## 15. Conclusion

Le desk est un système de tendance défensif, diversifié par construction,
dont la performance vient principalement du sleeve tendance et de la
protection du capital en régime baissier. Sur l'univers à 8 actifs et le
mandat 2.0, l'OOS est positif (+9,5 % de CAGR compte neuf, +10,9 % continu),
mais le walk-forward est faible (4/8 folds, DD -20 %). Le CAGR complet de
16,0 % ne doit pas être extrapolé.

Le choix de simplicité est assumé : mean-reversion suspendue, hard stop
désactivé, override baissier désactivé, deux sleeves actifs. Les tests ont
montré que ces ajouts de complexité dégradaient le portefeuille sur cet
univers. Ce n'est pas une preuve qu'ils seront toujours inutiles ; c'est une
preuve qu'ils ne sont pas validés ici.

Les prochaines décisions ne doivent pas être des réglages de paramètres sur
l'OOS. Elles doivent porter sur :

1. la compréhension du walk-forward faible et de la période 2025-2026 ;
2. la concentration du PnL sur BNB et SOL ;
3. la qualité de l'exécution et du modèle de coûts ;
4. un contrôle du biais de survie de l'univers ;
5. un vrai paper broker avec réconciliation, si le passage au live est
   envisagé.

Tant que ces points ne sont pas traités, le système reste un outil de
recherche et de paper trading, pas un système de production.
