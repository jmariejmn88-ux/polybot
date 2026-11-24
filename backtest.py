#!/usr/bin/env python3
"""
Backtest PolyBot sur les marchés Polymarket « Up or Down 15 min » déjà terminés.

Pour chaque fenêtre passée :
  - résultat réel (Up/Down) via l'API Gamma ;
  - prix historique du jeton « Up » via l'API CLOB (prices-history, pas de 1 min) ;
  - prix BTC/ETH minute par minute via Coinbase.
Puis on rejoue la stratégie du bot à plusieurs moments d'entrée et seuils
d'avantage, et on la compare à deux stratégies naïves :
  - « hasard » : côté tiré au sort aux mêmes moments ;
  - « favori » : toujours le côté le plus cher (celui que la foule préfère).

Limites honnêtes :
  - l'historique donne un prix moyen, pas le vrai carnet d'ordres → on ajoute
    un écart (SPREAD) au prix payé, plus les vrais frais Polymarket
    (parts × 0,07 × p × (1-p)) ;
  - les prix viennent de Coinbase, pas de l'oracle Chainlink de Polymarket :
    la section « Règle de résolution » mesure à quel point ils concordent.

Usage : python backtest.py --days 7 --assets btc,eth
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import random
import statistics
import sys
import time

import bot  # réutilise le modèle et les appels réseau du bot

WINDOW = 900
OUT = os.path.join(bot.DATA, "backtest.md")


# ----------------------------------------------------------------- collecte
def fetch_window(asset, t_start):
    slug = f"{asset}-updown-15m-{t_start}"
    try:
        ev = bot.get_event(slug)
    except Exception as e:  # noqa: BLE001
        return {"slug": slug, "error": str(e)}
    if not ev or not ev["closed"]:
        return None
    winner = next((o for o, p in ev["prices"].items() if p == 1.0), None)
    if winner not in ("Up", "Down"):
        return None
    try:
        hist = bot.http_json(f"{bot.CLOB}/prices-history",
                             {"market": ev["tokens"]["Up"], "startTs": t_start,
                              "endTs": t_start + WINDOW, "fidelity": 1})
    except Exception as e:  # noqa: BLE001
        return {"slug": slug, "error": str(e)}
    pts = sorted((int(h["t"]), float(h["p"])) for h in hist.get("history", []))
    if len(pts) < 3:
        return None
    return {"slug": slug, "asset": asset, "t_start": t_start, "winner": winner, "up_hist": pts}


def fetch_candles(product, start, end):
    """Bougies 1 min Coinbase sur [start, end] → {minute_ts: (open, close)}."""
    out = {}
    t = start
    while t < end:
        t2 = min(t + 300 * 60, end)
        raw = bot.http_json(f"{bot.COINBASE}/products/{product}/candles", {
            "granularity": 60,
            "start": dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat(),
            "end": dt.datetime.fromtimestamp(t2, dt.timezone.utc).isoformat()})
        for c in raw:
            out[int(c[0])] = (float(c[3]), float(c[4]))
        t = t2
        time.sleep(0.15)
    return out


def mid(c):
    return (c[0] + c[1]) / 2


RULES = {
    # nom : (fonction prix de départ, fonction prix final)
    "close vs open": (lambda cs, t: cs[t][0], lambda cs, t: cs[t + 840][1]),
    "moyenne 1re min vs moyenne dernière min": (lambda cs, t: mid(cs[t - 60]), lambda cs, t: mid(cs[t + 840])),
    "moyenne 15 min vs open": (lambda cs, t: cs[t][0],
                               lambda cs, t: sum(mid(cs[t + k]) for k in range(0, 900, 60)) / 15),
    "moyenne 15 min vs moyenne minute précédente": (lambda cs, t: mid(cs[t - 60]),
                               lambda cs, t: sum(mid(cs[t + k]) for k in range(0, 900, 60)) / 15),
}


def rule_check(windows, candles):
    """Quelle définition, calculée avec Coinbase, reproduit le mieux les résultats réels ?"""
    res = {}
    for name, (p0f, pff) in RULES.items():
        ok = n = 0
        for w in windows:
            cs, t = candles[w["asset"]], w["t_start"]
            try:
                up = pff(cs, t) >= p0f(cs, t)
            except KeyError:
                continue
            n += 1
            ok += (up == (w["winner"] == "Up"))
        res[name] = (ok / n if n else 0, n)
    return res


def price_at(pts, ts):
    before = [p for t, p in pts if t <= ts]
    return before[-1] if before else None


# ----------------------------------------------------------------- simulation
def build_signals(windows, candles, entry_left, fee, spread, model="twap", p0mode="open", lag=45):
    """Pour chaque fenêtre : proba du modèle, prix payé de chaque côté, résultat."""
    sigs = []
    for w in windows:
        t = w["t_start"]
        E = t + WINDOW - entry_left
        cs = candles[w["asset"]]
        o = cs.get(t) if p0mode == "open" else cs.get(t - 60)
        last = cs.get(E - 60)
        closes = [cs[m][1] for m in range(E - 120 * 60, E, 60) if m in cs]
        inwin = [cs[m][1] for m in range(t, E - 60, 60) if m in cs]
        # L'historique Polymarket a ~45 s de retard : on prend le prix 'lag' secondes plus tard
        # pour comparer à information égale (sinon le bot « voit l'avenir » et le gain est faux).
        up_mid = price_at(w["up_hist"], E + lag)
        if not o or not last or len(closes) < 30 or up_mid is None:
            continue
        p0 = o[0] if p0mode == "open" else mid(o)
        p_up, _ = bot.prob_up(last[1], p0, closes, entry_left, inwin, model)
        asks = {"Up": min(up_mid + spread, 0.99), "Down": min(1 - up_mid + spread, 0.99)}
        sigs.append({"slug": w["slug"], "asset": w["asset"], "t": E, "winner": w["winner"],
                     "model": {"Up": p_up, "Down": 1 - p_up}, "ask": asks, "fee": fee})
    return sigs


def cps_of(price, rate):
    return price + rate * price * (1 - price)


def pnl_of(side, s, stake=1.0):
    cps = cps_of(s["ask"][side], s["fee"])
    shares = stake / cps
    return (shares if s["winner"] == side else 0.0) - stake


def strategy(sigs, min_edge, lo=0.05, hi=0.95):
    trades = []
    for s in sigs:
        best = None
        for side in ("Up", "Down"):
            a = s["ask"][side]
            if not lo <= a <= hi:
                continue
            edge = s["model"][side] - cps_of(a, s["fee"])
            if best is None or edge > best[1]:
                best = (side, edge)
        if best and best[1] >= min_edge:
            trades.append({**s, "side": best[0], "edge": best[1], "pnl": pnl_of(best[0], s)})
    return trades


def stats(pnls):
    if not pnls:
        return {"n": 0, "win": 0, "roi": 0, "total": 0, "dd": 0}
    eq, peak, dd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"n": len(pnls), "win": sum(p > 0 for p in pnls) / len(pnls),
            "roi": sum(pnls) / len(pnls), "total": sum(pnls), "dd": dd}


def random_baseline(trades, runs=2000, seed=7):
    rng = random.Random(seed)
    rois = []
    for _ in range(runs):
        rois.append(statistics.mean(pnl_of(rng.choice(("Up", "Down")), t) for t in trades))
    return rois


def favorite(sigs):
    out = []
    for s in sigs:
        side = "Up" if s["ask"]["Up"] >= s["ask"]["Down"] else "Down"
        if 0.05 <= s["ask"][side] <= 0.95:
            out.append(pnl_of(side, s))
    return out


def bankroll_sim(trades, start=10.0, min_shares=5, max_pct=0.25, stake=1.0):
    bank, taken, skipped = start, 0, 0
    for t in sorted(trades, key=lambda x: x["t"]):
        cps = cps_of(t["ask"][t["side"]], t["fee"])
        shares = max(stake / cps, min_shares)
        cost = shares * cps
        if cost > bank * max_pct or bank < 1:
            skipped += 1
            continue
        bank += (shares if t["winner"] == t["side"] else 0) - cost
        taken += 1
    return bank, taken, skipped


def calibration(trades):
    rows = []
    for lo in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        b = [t for t in trades if lo <= t["model"][t["side"]] < lo + 0.1]
        if b:
            rows.append((lo, len(b), statistics.mean(t["model"][t["side"]] for t in b),
                         statistics.mean(t["ask"][t["side"]] for t in b),
                         sum(t["winner"] == t["side"] for t in b) / len(b)))
    return rows


# ----------------------------------------------------------------- rapport
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--assets", default="btc,eth")
    ap.add_argument("--fee", type=float, default=bot.CFG["FEE_RATE"])
    ap.add_argument("--spread", type=float, default=0.01)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--lag", type=int, default=45,
                    help="retard (s) de l'historique de prix Polymarket à compenser")
    a = ap.parse_args()

    assets = [x.strip() for x in a.assets.split(",") if x.strip() in bot.PRODUCTS]
    now = int(time.time())
    end = now - now % WINDOW - 2 * WINDOW           # fenêtres déjà résolues
    start = end - int(a.days * 86400)
    starts = list(range(start, end, WINDOW))
    print(f"Collecte : {len(starts)} fenêtres × {len(assets)} cryptos…", flush=True)

    windows, errors = [], 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(fetch_window, asset, t) for asset in assets for t in starts]
        for i, f in enumerate(cf.as_completed(futs), 1):
            r = f.result()
            if r and "error" in r:
                errors += 1
            elif r:
                windows.append(r)
            if i % 200 == 0:
                print(f"  {i}/{len(futs)}", flush=True)
    candles = {asset: fetch_candles(bot.PRODUCTS[asset], start - 130 * 60, end + WINDOW)
               for asset in assets}
    print(f"{len(windows)} fenêtres exploitables, {errors} erreurs réseau", flush=True)
    if not windows:
        sys.exit("Aucune donnée : vérifier l'accès aux API.")

    up_rate = sum(w["winner"] == "Up" for w in windows) / len(windows)
    rc = rule_check(windows, candles)
    best_rule = max(rc, key=lambda k: rc[k][0])
    model = "twap" if "15 min" in best_rule else "close"
    p0mode = "prev" if "précédente" in best_rule or "1re min" in best_rule else "open"
    L = ["# 🔬 Backtest PolyBot", "",
         f"_Généré le {bot.now_iso()} — {a.days:g} jours, {', '.join(x.upper() for x in assets)}, "
         f"{len(windows)} marchés résolus (Up {up_rate:.0%})._", "",
         f"Hypothèses : écart payé +{a.spread:.2f} $ par part, frais Polymarket parts × {a.fee} × p × (1-p), "
         f"mise fixe 1 $ par pari. Retard des prix historiques compensé : {a.lag} s.", "",
         "## 0. Règle de résolution", "",
         "Le texte officiel parle d'un prix moyen (TWAP Chainlink). On teste quelle définition, "
         "calculée avec les prix Coinbase, retrouve le plus souvent le vrai résultat :", "",
         "| Définition | Marchés | Concordance avec le vrai résultat |", "|---|---|---|",
         *[f"| {k}{' ✅' if k == best_rule else ''} | {v[1]} | {v[0]:.1%} |" for k, v in rc.items()],
         "", f"→ Modèle utilisé pour la suite : **{model}** (prix de départ : {p0mode}).", "",
         "## 1. Grille de réglages", "",
         "Moment d'entrée = secondes avant la fin. ROI = gain moyen par dollar misé.", "",
         "| Entrée | Avantage min | Paris | Réussite | ROI | Gain total | Pire creux | Hasard (médiane) | Bat le hasard |",
         "|---|---|---|---|---|---|---|---|---|"]
    best = None
    grid = {}
    for left in (540, 420, 300, 180):
        sigs = build_signals(windows, candles, left, a.fee, a.spread, model, p0mode, a.lag)
        grid[left] = sigs
        for me in (0.03, 0.06, 0.10, 0.15):
            tr = strategy(sigs, me)
            st = stats([t["pnl"] for t in tr])
            if st["n"] < 10:
                L.append(f"| {left}s | {me:.2f} | {st['n']} | — | — | — | — | — | trop peu |")
                continue
            rb = random_baseline(tr)
            pct = sum(r < st["roi"] for r in rb) / len(rb)
            L.append(f"| {left}s | {me:.2f} | {st['n']} | {st['win']:.0%} | {st['roi']:+.1%} | "
                     f"{st['total']:+.2f} $ | {st['dd']:.2f} $ | {statistics.median(rb):+.1%} | {pct:.0%} |")
            if st["n"] >= 30 and (best is None or st["roi"] > best[2]["roi"]):
                best = (left, me, st, tr, pct)

    L += ["", "« Bat le hasard » = part des 2 000 tirages au sort que la stratégie dépasse. "
              "Il faut ≥ 95 % pour parler d'un vrai avantage.", ""]

    cur = strategy(grid[300], bot.CFG["MIN_EDGE"])
    fav = stats(favorite(grid[300]))
    L += ["## 2. Comparaison à 5 min de la fin", "",
          "| Stratégie | Paris | Réussite | ROI | Gain total |", "|---|---|---|---|---|"]
    cs = stats([t["pnl"] for t in cur])
    L.append(f"| PolyBot (réglage actuel, avantage ≥ {bot.CFG['MIN_EDGE']:.2f}) | {cs['n']} | "
             f"{cs['win']:.0%} | {cs['roi']:+.1%} | {cs['total']:+.2f} $ |")
    L.append(f"| Toujours le favori | {fav['n']} | {fav['win']:.0%} | {fav['roi']:+.1%} | {fav['total']:+.2f} $ |")

    if cur:
        L += ["", "## 3. Le modèle est-il bien calibré ? (réglage actuel)", "",
              "| Proba du modèle | Paris | Proba moyenne | Prix payé moyen | Réussite réelle |",
              "|---|---|---|---|---|"]
        for lo, n, pm, pa, w in calibration(cur):
            L.append(f"| {lo:.0%}–{lo+.1:.0%} | {n} | {pm:.0%} | {pa:.2f} | {w:.0%} |")
        L += ["", "Si la réussite réelle est proche du prix payé (et pas de la proba du modèle), "
                  "c'est le marché qui avait raison."]
        bank, taken, skipped = bankroll_sim(cur)
        L += ["", "## 4. Avec tes 10 $ (minimum 5 parts, max 25 % par pari)", "",
              f"- Paris possibles : **{taken}** (refusés faute de capital : {skipped})",
              f"- Capital final : **{bank:.2f} $** ({(bank/10-1):+.0%})"]

    L += ["", "## Verdict", ""]
    # 16 réglages testés : le meilleur bat souvent le hasard par chance.
    # Correction de Bonferroni : il faut battre 99,7 % des tirages ET avoir ≥ 200 paris.
    if best and best[2]["roi"] > 0 and best[4] >= 0.997 and best[2]["n"] >= 200:
        L.append(f"✅ Piste sérieuse : entrée {best[0]}s, avantage ≥ {best[1]:.2f} → ROI {best[2]['roi']:+.1%} "
                 f"sur {best[2]['n']} paris, mieux que {best[4]:.0%} des tirages au hasard. "
                 "À confirmer sur une autre période (relancer avec --days 14) avant tout argent réel.")
    elif best and best[2]["roi"] > 0:
        L.append(f"🟡 Meilleur réglage positif (entrée {best[0]}s, avantage ≥ {best[1]:.2f}, ROI {best[2]['roi']:+.1%} "
                 f"sur {best[2]['n']} paris, bat le hasard dans {best[4]:.0%} des cas), mais c'est le meilleur de 16 essais : "
                 "il faudrait ≥ 99,7 % et ≥ 200 paris pour exclure la chance. Pas d'avantage prouvé.")
    else:
        L.append("❌ Aucun réglage n'est rentable après frais sur cette période. "
                 "Ne pas passer en réel ; chercher une autre idée de stratégie.")
    L += ["", "_Rappel : un backtest positif ne garantit rien. Les vrais résultats sont presque toujours moins bons._"]

    os.makedirs(bot.DATA, exist_ok=True)
    text = "\n".join(L) + "\n"
    with open(OUT, "w") as fh:
        fh.write(text)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(text)
    print(text)


if __name__ == "__main__":
    main()
