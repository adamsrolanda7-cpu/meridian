==============================================================================
MERIDIAN — SUIVI OPÉRATIONNEL · WATCH
==============================================================================
Généré le      : 2026-11-28T08:13:17+00:00
Source données : binance/cache · dernière bougie 2026-11-27 (retard 1 j)
Paper          : 2026-11-15 → 2026-11-27 · equity 100 000 $ · cash 100 000 $ · DD +0,0%

Critère                 Statut  Valeur            Détail
------------------------------------------------------------------------------
fraîcheur données       OK      1 j               dernière bougie 2026-11-27 · seuil 3 j
qualité données         OK      OK                tous les actifs OK
mandat                  OK      inchangé          la config courante correspond à l'initialisation du paper
réconciliation replay   OK      +0,000%           equity papier/replay identiques à +0,000% près.
drawdown                OK      +0,0%             drawdown courant · max historique du replay +0,0% · watch -12,0% · kill -20,0%
Sharpe glissant         WATCH   historique court  12 jours seulement ; minimum 60.
  → attendre l'historique minimal avant d'interpréter le rolling.
exposition              OK      +0,0%             brut / plafond 95,0% · positions 0 · aucune position au-dessus de 35,0%
turnover                OK      0,0x              turnover annualisé · seuil 9,0x
gouverneur              OK      normal            drawdown gouverneur +0,0% · dernier halt aucun

Attribution (trades clos, depuis le début du paper) :

Kill switch : inactif — aucun critère d'arrêt déclenché.
Aucun ordre réel n'a été envoyé.
