# Checklist — passage en compte réel

Cette checklist est volontairement stricte. Le mode réel de Meridian est
**opt-in** : une clé API présente dans `.env` ne suffit jamais à envoyer un
ordre. Il faut aussi `trade --execute --confirm` et
`MERIDIAN_LIVE_CONFIRM=YES`.

> Meridian est un outil de recherche. Le backtest et le paper ne prouvent pas
> la rentabilité future. Ne risquez que du capital dont la perte totale est
> supportable.

---

## 0. Avant de toucher à une clé API

- [ ] Le mandat est figé et la configuration n'a pas changé depuis la
      validation.
- [ ] Le paper trading a tourné au moins 3 à 6 mois sans divergence
      (`paper --verify` OK).
- [ ] Le monitor est stable, sans `ALERT` répété (`monitor`).
- [ ] Les limites personnelles sont écrites noir sur blanc : capital maximum,
      perte journalière maximum, perte totale maximum.
- [ ] Je sais comment tout arrêter : révoquer la clé API, annuler les ordres
      ouverts, revendre manuellement.
- [ ] Je ne trade pas avec de l'argent emprunté, ni avec des fonds dont j'ai
      besoin à court terme.

Si une case n'est pas cochée, rester en paper.

---

## 1. Compte exchange

- [ ] Utiliser un **sous-compte** dédié à Meridian, pas le compte principal.
- [ ] Activer la 2FA sur le compte exchange.
- [ ] Créer une clé API avec :
  - [ ] lecture du compte ;
  - [ ] trading **spot** uniquement ;
  - [ ] **aucun** droit de retrait ;
  - [ ] **aucun** droit futures, marges, transferts ou sous-comptes.
- [ ] Restreindre la clé à l'adresse IP du serveur (si l'exchange le permet).
- [ ] Commencer par le **testnet** officiel de l'exchange.
- [ ] Vérifier que le solde du sous-compte est celui attendu, et pas celui du
      compte principal.

---

## 2. Installation et `.env`

```bash
cd /root/meridian
python3 -m pip install -e ".[live]"
cp .env.example .env      # si .env n'existe pas encore
```

Remplir `.env` :

```dotenv
MERIDIAN_EXCHANGE_ID=binance
MERIDIAN_EXCHANGE_API_KEY=...
MERIDIAN_EXCHANGE_API_SECRET=...
MERIDIAN_EXCHANGE_PASSPHRASE=        # seulement si l'exchange en exige une
MERIDIAN_EXCHANGE_SANDBOX=true       # testnet d'abord
MERIDIAN_QUOTE=USDT
MERIDIAN_LIVE_CONFIRM=NO             # YES seulement au moment d'exécuter
MERIDIAN_MAX_ORDER_USD=250
MERIDIAN_MAX_DAILY_TURNOVER_USD=1000
```

- [ ] `.env` est bien ignoré par git : `git check-ignore -v .env`.
- [ ] Aucune clé n'apparaît dans `README.md`, un test, un log ou un commit.
- [ ] Les plafonds `MAX_ORDER_USD` et `MAX_DAILY_TURNOVER_USD` sont adaptés à
      la taille du compte. Ils doivent rester petits au début.

Vérification locale, sans réseau :

```bash
python3 -m meridian live-status
```

- [ ] Le fichier `.env` est détecté.
- [ ] L'exchange et le mode (TESTNET/MAINNET) sont corrects.
- [ ] Les clés sont présentes.
- [ ] `MERIDIAN_LIVE_CONFIRM` vaut `NO` en dehors d'une exécution volontaire.

---

## 3. Lecture seule du compte

```bash
# Testnet (par défaut)
python3 -m meridian account

# Mainnet, lecture seule
python3 -m meridian account --mainnet
```

- [ ] L'equity affichée correspond au sous-compte.
- [ ] Les positions et les soldes libres sont cohérents.
- [ ] Aucun ordre n'est envoyé par `account`.

Si le compte est vide, transférer seulement le capital de départ prévu.

---

## 4. Dry-run du rééquilibrage

```bash
python3 -m meridian trade --mainnet
```

- [ ] Le régime et la dernière bougie sont récents.
- [ ] Les cibles respectent `max_gross`, `max_asset_weight` et `min_cash`.
- [ ] Les ventes sont planifiées avant les achats.
- [ ] Chaque notionnel est inférieur ou égal à `MERIDIAN_MAX_ORDER_USD`.
- [ ] Le total est inférieur ou égal à `MERIDIAN_MAX_DAILY_TURNOVER_USD`.
- [ ] Le plan est écrit dans `runs/live/plan.json`.
- [ ] Aucun ordre n'a été envoyé (le dry-run ne touche pas l'exchange).

Répéter le dry-run plusieurs jours de suite avant d'exécuter.

---

## 5. Premier envoi réel

Conditions obligatoires :

- [ ] Testnet validé au moins une fois de bout en bout.
- [ ] Plafonds très bas (`MAX_ORDER_USD=25` ou `50` pour un premier essai).
- [ ] `MERIDIAN_LIVE_CONFIRM=YES` dans `.env`.
- [ ] Je suis devant l'écran et je peux couper immédiatement.

```bash
python3 -m meridian trade --mainnet --execute --confirm
```

- [ ] Les ordres apparaissent sur l'exchange.
- [ ] Les quantités et les prix correspondent au plan.
- [ ] `runs/live/journal.jsonl` contient l'entrée du jour.
- [ ] `runs/live/state.json` a été mis à jour.
- [ ] Le solde et les positions après exécution sont cohérents.

Remettre `MERIDIAN_LIVE_CONFIRM=NO` après l'exécution.

---

## 6. Après chaque séance

- [ ] Vérifier `python3 -m meridian live-status`.
- [ ] Vérifier `python3 -m meridian account --mainnet`.
- [ ] Vérifier `python3 -m meridian monitor`.
- [ ] Comparer les positions réelles au plan et au paper.
- [ ] Vérifier les frais réellement payés.
- [ ] Noter les écarts : slippage, ordre non rempli, ordre refusé.
- [ ] Si un ordre est resté ouvert, décider manuellement de l'annuler ou de
      le laisser.

---

## 7. Arrêt d'urgence

- [ ] Révoquer la clé API sur l'exchange (le plus rapide et le plus sûr).
- [ ] Annuler tous les ordres ouverts.
- [ ] Revendre manuellement les positions si nécessaire.
- [ ] Archiver `runs/live/state.json`, `runs/live/journal.jsonl` et
      `runs/live/plan.json`.
- [ ] Ne réarmer qu'après avoir compris l'incident et corrigé la cause.

---

## 8. Limites techniques à connaître

- Meridian n'envoie **pas** d'ordres stop à l'exchange. Les stops de la
  stratégie sont évalués sur les clôtures quotidiennes par le processus local.
  Si le processus est arrêté, il n'y a plus de stop.
- Le mode réel ne gère pas encore les fills partiels, les ordres orphelins,
  les rejets partiels ni la réconciliation automatique des positions.
- Le prix utilisé pour le plan est le dernier ticker ; le prix d'exécution
  peut être différent.
- Les ordres `market` peuvent payer du slippage. Les ordres `limit` peuvent ne
  pas être remplis.
- Un exchange peut suspendre un marché, imposer des limites ou modifier ses
  règles sans préavis.
- Le code ne connaît pas votre situation fiscale. Les gains peuvent être
  imposables.

---

## 9. Feu vert final

Je passe en réel seulement si :

- [ ] toutes les cases ci-dessus sont cochées ;
- [ ] le premier essai est petit ;
- [ ] je peux perdre 100 % du capital engagé sans impact sur ma vie ;
- [ ] je continue à vérifier le compte chaque jour ;
- [ ] je sais que le mode réel peut échouer même si le backtest est bon.
