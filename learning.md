# 🧠 PolyBot — journal d'apprentissage

Règle d'or : **un seul changement à la fois**, testé sur au moins 200 paris avant de conclure.
Le bot ajoute ici un bilan chiffré chaque lundi. Toi, tu écris la leçon en une phrase.

## Règles actuelles

- Entrée entre 10 et 2 min avant la fin d'une fenêtre de 15 min.
- Pari seulement si l'avantage du modèle dépasse 6 points, frais compris.
- Pas de pari ±30 min autour des annonces de `calendar.txt`.
- Pause du jour à -3 $, arrêt définitif sous 1 $.

## Ce qui marche

- _(à remplir)_

## Résultats du 1er backtest (19/11/2026, 7 jours, 1 344 marchés BTC/ETH)

- **Règle de résolution** : moyenne de la dernière minute ≥ moyenne de la minute avant le début
  (95,8 % de concordance avec Coinbase). La moyenne des 15 min ne colle qu'à 84 %. → modèle `close`.
- **Frais réels** : parts × 0,07 × p × (1-p), soit ~3,5 % du montant misé à 50 ¢.
- **Piège évité** : l'historique de prix Polymarket a ~45 s de retard. Sans correction, le backtest affichait
  +40 à +99 % de rendement, un gain fictif.
- **À information égale** : résultats instables selon le moment d'entrée (de −20 % à +40 %),
  sur 50 à 150 paris seulement. **Aucun avantage prouvé.** Le seul vrai test : la simulation en direct
  (le bot lit le carnet d'ordres réel), sur 200+ paris.

## 2e backtest (24/11/2026, 14 jours, 2 688 marchés) — `data/backtest-2026-11-24.md`

- Règle de résolution confirmée (96 % de concordance).
- Réglage actuel (entrée 5 min, avantage ≥ 0,06) : +3,5 % sur 616 paris, bat le hasard 91 % du temps → **non significatif**.
- Le modèle est trop optimiste (annonce 35 %, réalité 19 %). Piste : réduire sa confiance avant de tester autre chose.
- Avec 10 $ et 5 parts minimum : seulement 6 paris possibles, capital final 3,47 $.
- Le verdict automatique du backtest était trop indulgent (meilleur de 16 essais) → corrigé.

## Ce qui ne marche pas

- _(à remplir)_

## Tests en cours

| Date | Changement testé | Paris | Résultat | Décision |
|---|---|---|---|---|

## Bilans hebdomadaires

## Simulation 100 $ (24/11/2026, 14 jours, règles du bot : 2 $/pari, min 5 parts, pause à −10 $/jour)

- PolyBot actuel : 100 $ → 245,83 $ (252 paris, 36 % gagnés, pire creux −35,56 $). L'essentiel du gain vient de 2 jours (15–16/11).
- Paris au hasard aux mêmes moments : médiane 106 $, mais de 19 $ à 204 $ selon la chance → énorme variabilité.
- Toujours le favori : 100 $ → 56,59 $.
- Lecture : résultat encourageant mais fragile (porté par quelques paris « longshot » gagnants). À confirmer en simulation en direct.
