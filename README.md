# PolyBot v2 — paper trading sur Polymarket 

Bot qui **simule** des paris sur les marchés « Bitcoin / Ethereum Up or Down — 15 minutes » de Polymarket, avec **100 $ fictifs**.
Il ne touche à aucun argent réel : pas de clé, pas de portefeuille.

Le but : voir pendant 2 à 4 semaines si la stratégie gagne **vraiment** avant d'y mettre un seul dollar.

---

## Comment il décide

Toutes les minutes (le bot tourne en continu, voir « Fonctionnement en continu ») :

1. **Il règle les paris terminés** : gagné, la part vaut 1 $ ; perdu, elle vaut 0 $.
2. **Il regarde la fenêtre de 15 min en cours** (ex. 14h00–14h15) :
   - il récupère le prix réel du BTC/ETH (Coinbase) au début de la fenêtre et maintenant ;
   - il calcule la probabilité que ça finisse **plus haut** (« Up »), selon la volatilité récente ;
   - il compare avec le prix sur Polymarket (ex. « Up » à 0,42 $ = le marché dit 42 %).
3. **Il « achète » seulement si son estimation dépasse le prix du marché d'au moins 6 points**, après frais.

## Nouveautés v2

- **Backtest** (`backtest.py`) : rejoue la stratégie sur des centaines de marchés déjà terminés et la compare à deux stratégies naïves (au hasard, toujours le favori). Verdict en ~15 min au lieu de semaines.
- **Pause les jours d'annonces** (`calendar.txt`) : aucun pari ±30 min autour de l'inflation US (CPI), de l'emploi US et des décisions de la Fed.
- **Rapport quotidien Telegram** vers 20h (heure d'Abidjan) + alerte si le bot se met en pause ou s'arrête.
- **Journal enrichi** (`data/trades.csv`) : raison de chaque pari, prix d'ouverture, volatilité, frais, R.
- **`learning.md`** : bilan chiffré ajouté automatiquement chaque lundi ; tu y écris la leçon de la semaine.

## Garde-fous

| Règle | Valeur par défaut |
|---|---|
| Mise visée par pari | 2 $ (2 % du capital) |
| Mise max par pari | 25 % du capital |
| Un seul pari par fenêtre et par crypto | ✔ |
| Entrée seulement entre 10 et 2 min avant la fin | ✔ |
| Pause du jour si perte ≥ 10 $ | ✔ |
| Max 20 paris / jour | ✔ |
| Arrêt définitif si capital < 10 $ | ✔ |

> ⚠️ Polymarket impose en général **5 parts minimum par ordre** : à 0,45 $ la part, ça fait déjà environ 2,30 $.
> Avec 10 $, le bot ne peut donc parier que quand le prix est bas ou que le capital le permet. C'est voulu : c'est la réalité d'un petit capital.

---

## Installation 

1. Crée un compte gratuit sur **github.com**.
2. Clique sur **+** → **New repository** → nom : `polybot` → choisis **Public** → **Create repository**.
   (Public = minutes d'automatisation illimitées et gratuites. Il n'y a aucun secret dedans.)
3. Sur la page du dépôt, clique sur **uploading an existing file**, puis glisse **tout le contenu** du dossier dézippé :
   `bot.py`, `backtest.py`, `calendar.txt`, `learning.md`, `README.md` et le dossier `.github`.
   *Astuce : le dossier `.github` est caché sur certains ordinateurs. S'il ne s'envoie pas, crée le fichier à la main :
   **Add file → Create new file**, nom `.github/workflows/polybot.yml` (puis `.github/workflows/backtest.yml`), et colle leur contenu.*
4. Clique sur **Commit changes**.
5. Onglet **Settings** → **Actions** → **General** → *Workflow permissions* → coche **Read and write permissions** → **Save**.
6. Onglet **Actions** → active les workflows si GitHub le demande → choisis **PolyBot (paper trading)** → **Run workflow**.

C'est tout : il tournera ensuite tout seul, jour et nuit.

## Fonctionnement en continu

Sur le plan gratuit, GitHub ne lance pas un workflow « toutes les 5 minutes » de façon fiable
(parfois une seule fois toutes les 1 à 2 heures). Le workflow lance donc `run_loop.py`, qui garde
le bot actif **~5h45 par session** : un passage par minute, et une sauvegarde des résultats dans
`data/` toutes les 5 minutes (ou tout de suite quand un pari est pris ou réglé). Un déclenchement
horaire garde toujours une nouvelle session prête à prendre le relais.

## Paris sportifs (simulation) — foot + NBA

Le module `sports.py` tourne dans la même boucle que le bot crypto, avec sa propre
bankroll virtuelle de 100 $ (séparée).

**Principe** : PinnFeed fournit les cotes de Pinnacle et la probabilité « sans marge »
de chaque issue. Quand Polymarket vend une issue moins cher que cette probabilité
(frais de 5 % × p × (1 − p) compris) d'au moins 3 points, le bot prend un pari virtuel
de 2 $, au plus un par match, entre 15 min et 36 h avant le coup d'envoi. Il le règle
quand Polymarket publie le résultat.

- Foot : toutes les ligues présentes à la fois chez Pinnacle et sur Polymarket
  (grands championnats, divisions 2, MLS, coupes, Ligue des champions, Europa League,
  compétitions internationales…). Résultat 1X2 sur le temps réglementaire.
- Basket : NBA (prolongation comprise).
- Recherche toutes les 10 min : environ 290 requêtes PinnFeed par jour (plafond 900, quota gratuit 1 000).

**Mise en route** :
1. Créer une clé gratuite sur pinnfeed.io et choisir les 2 sports **soccer** et **basketball**.
2. Dans GitHub : Settings → Secrets and variables → Actions → New repository secret,
   nom `PINNFEED_KEY`, valeur = la clé. Ne jamais la coller ailleurs.
3. Sans clé, le module reste en attente et le bot crypto continue normalement.

**Résultats** : `data/sports_summary.md` (résumé), `data/sports_trades.csv` (paris réglés),
`data/sports_state.json` (état). Le résumé compare le résultat réel au résultat
« attendu » selon Pinnacle : il faut plusieurs centaines de paris pour conclure.

Réglages (dans le workflow) : `SPORTS` (1/0), `SPORTS_LIST`, `SPORTS_STAKE`,
`SPORTS_MIN_EDGE`, `SPORTS_EVERY_MIN`, `SPORTS_USE_KELLY`.

## Ce que le 1er backtest a montré (19/11/2026)

Lancé sur 7 jours (1 344 marchés) : **aucun avantage prouvé** une fois corrigé le retard des prix historiques.
Détails dans `learning.md`. Le bot reste donc en **simulation**, et c'est la simulation en direct qui tranchera.

## Étape 1 : lancer le backtest 

Onglet **Actions** → **PolyBot Backtest** → **Run workflow** → laisse `7` jours et `btc,eth` → **Run workflow**.
Après ~10 à 20 minutes, le rapport apparaît dans **`data/backtest.md`** (et en bas de la page de l'exécution).

Comment le lire :
- **ROI** : gain moyen par dollar misé, après frais. Il doit être positif.
- **Bat le hasard** : il faut **≥ 95 %**. En dessous, le résultat peut n'être que de la chance.
- **Calibration** : si la réussite réelle colle au *prix payé* plutôt qu'à la *proba du modèle*, le marché avait raison et le bot n'a pas d'avantage.
- Si un réglage sort du lot, relance avec `14` jours pour vérifier qu'il tient sur une autre période.

## Étape 2 (optionnel) : rapport Telegram

1. Dans Telegram, écris à **@BotFather** → `/newbot` → choisis un nom → il te donne un **token**.
2. Écris un message quelconque à ton nouveau bot, puis ouvre dans ton navigateur
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` et repère `"chat":{"id": ...}` : c'est ton **chat ID**.
3. Sur GitHub : **Settings → Secrets and variables → Actions → New repository secret**,
   crée `TELEGRAM_TOKEN` puis `TELEGRAM_CHAT_ID`.

Le token reste secret : ne le colle jamais dans un fichier du dépôt.

## Étape 3 : mettre à jour le calendrier

Une fois par mois, ajoute les nouvelles dates dans `calendar.txt` (sources : bls.gov et federalreserve.gov), en heure UTC = heure d'Abidjan.

## Suivre les résultats

- **`data/summary.md`** dans le dépôt : capital, taux de réussite, derniers paris, journal (lisible depuis ton téléphone).
- **`data/trades.csv`** : historique complet, à ouvrir dans Excel ou Google Sheets.
- Onglet **Actions** → clique sur une exécution : le résumé s'affiche en bas.

## Régler la stratégie

Dans `.github/workflows/polybot.yml`, section `env:` :

- `MIN_EDGE` : plus haut = moins de paris, plus sélectifs (ex. `0.10`) ;
- `ASSETS` : `btc`, `eth`, `sol`, `xrp` (séparés par des virgules) ;
- `START_BANKROLL`, `STAKE`, `MAX_STAKE_PCT` ;
- `USE_KELLY` : `0` par défaut (mise fixe). Le bot calcule quand même la **mise de Kelly**
  (`KELLY_FRACTION`, ¼ par défaut) et la note dans le journal et `trades.csv`. Mettre `1` pour
  l'utiliser vraiment, seulement après avoir comparé les deux sur 200+ paris ;
- `LOG_CONTEXT` : `1` par défaut. À chaque pari, le bot note le contexte de marché
  **CoinLobster** (activité des baleines, funding, open interest), sans s'en servir pour décider.
  Ces colonnes de `trades.csv` permettront de vérifier plus tard si ce contexte aide.

Pour **repartir de zéro** : supprime le dossier `data/` dans le dépôt.

## Quand passer en réel ?

Seulement si, sur **au moins 200 paris réglés**, le gain net reste positif **avec les frais**.
En dessous de ce nombre, un bon résultat peut n'être que de la chance.
Le passage en réel demandera un portefeuille Polygon avec de l'USDC, une clé privée stockée en *secret* GitHub, et un serveur hors des États-Unis
(les serveurs GitHub sont aux USA, pays bloqué par Polymarket pour passer des ordres).

*Ce projet est éducatif, pas un conseil financier. Les marchés de prédiction peuvent faire perdre toute la mise.*
