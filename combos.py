"""
PolyBot Combos — combinés (parlays) en SIMULATION, sur le modèle des « Combos » Polymarket.

Un combo réunit 2 ou 3 issues de matchs différents ; il ne paie que si toutes gagnent.
- Prédiction de succès = produit des probabilités Pinnacle sans marge de chaque issue
  (matchs différents, donc indépendants).
- Prix simulé = produit des prix d'achat Polymarket × (1 + marge). Les vrais combos
  Polymarket sont cotés par des teneurs de marché (enchère RFQ) : la marge simule leur part.
- Un combo n'est pris que si sa prédiction dépasse son prix d'au moins COMBO_MIN_EV (en relatif).

Rythme : au plus COMBO_PER_DAY combos par jour (un à 2 matchs, un à 3 matchs), 1 $ chacun,
avec une cagnotte virtuelle séparée de 100 $. Aucun ordre réel n'est envoyé.
"""
import datetime as dt
import itertools
import os

import bot


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


CFG = {
    "START": _f("COMBO_START_BANKROLL", 100),
    "STAKE": _f("COMBO_STAKE", 1),
    "PER_DAY": int(_f("COMBO_PER_DAY", 2)),
    "LEGS": [int(x) for x in os.environ.get("COMBO_LEGS", "2,3").split(",") if x.strip()],
    "LEG_MIN_PRICE": _f("COMBO_LEG_MIN_PRICE", 0.35),   # sélections plutôt favorites
    "LEG_MAX_PRICE": _f("COMBO_LEG_MAX_PRICE", 0.85),
    "MARGIN": _f("COMBO_MARGIN", 0.05),                 # part supposée du teneur de marché
    "MIN_EV": _f("COMBO_MIN_EV", 0.03),                 # prédiction ≥ prix × 1,03
    "MAX_HOURS": _f("COMBO_MAX_HOURS", 24),
    "MIN_MINUTES": _f("COMBO_MIN_MINUTES", 15),
}
ROLE_FR = {"home": "victoire", "away": "victoire", "draw": "match nul"}


def _init(st):
    st.setdefault("combo_bankroll", CFG["START"])
    st.setdefault("combo_start", CFG["START"])
    st.setdefault("combos_open", [])
    st.setdefault("combos_closed", [])


def candidates(st, sp):
    """Issues éligibles tirées des matchs analysés (prix Polymarket + probabilité Pinnacle)."""
    t0 = sp.now()
    lo, hi = t0 + dt.timedelta(minutes=CFG["MIN_MINUTES"]), t0 + dt.timedelta(hours=CFG["MAX_HOURS"])
    out = []
    for league, block in (st.get("watch") or {}).items():
        for g in block.get("games", []):
            start = sp.parse_time(g.get("start"))
            if not start or not (lo <= start <= hi):
                continue
            for role, price in (g.get("pm") or {}).items():
                fair = (g.get("pin") or {}).get(role)
                tok, mid = (g.get("tok") or {}).get(role), (g.get("mid") or {}).get(role)
                if fair is None or not tok or not mid:
                    continue
                if not CFG["LEG_MIN_PRICE"] <= price <= CFG["LEG_MAX_PRICE"]:
                    continue
                pick = g["t1"] if role == "home" else g["t2"] if role == "away" else "Match nul"
                out.append({"slug": g["slug"], "match": g["match"], "ligue": g.get("ligue") or league,
                            "start": g["start"], "role": role, "pick": pick, "token": tok, "marche": mid,
                            "rate": (g.get("rate") or {}).get(role, 0.05),
                            "gprice": price, "fair": fair, "value": fair / price})
    # les meilleures valeurs d'abord ; une seule issue par match
    out.sort(key=lambda c: c["value"], reverse=True)
    best, seen = [], set()
    for c in out:
        if c["slug"] not in seen:
            best.append(c)
            seen.add(c["slug"])
    return best[:8]


def quote(legs, sp):
    """Prix simulé d'un combo : produit des prix d'achat (frais inclus) × (1 + marge)."""
    price = 1.0
    for leg in legs:
        price *= sp.cost_per_share(leg["ask"], leg["rate"])
    return min(price * (1 + CFG["MARGIN"]), 0.99)


def build(st, sp, n_legs):
    cands = candidates(st, sp)
    if len(cands) < n_legs:
        return None
    # 1) classement rapide avec les prix affichés
    ranked = []
    for legs in itertools.combinations(cands, n_legs):
        fair, price = 1.0, 1.0
        for leg in legs:
            fair *= leg["fair"]
            price *= sp.cost_per_share(leg["gprice"], leg["rate"])
        ranked.append((fair / (price * (1 + CFG["MARGIN"])), legs))
    ranked.sort(key=lambda x: x[0], reverse=True)
    # 2) vérification avec les vrais prix de vente (carnet d'ordres) pour les 3 meilleurs
    for _, legs in ranked[:3]:
        legs = [dict(leg) for leg in legs]
        ok = True
        for leg in legs:
            try:
                ask, _size = sp.best_ask(leg["token"])
            except Exception:  # noqa: BLE001
                ask = None
            if ask is None or not 0.01 < ask < 0.99:
                ok = False
                break
            leg["ask"] = ask
        if not ok:
            continue
        fair = 1.0
        for leg in legs:
            fair *= leg["fair"]
        price = quote(legs, sp)
        ev = fair / price - 1
        if ev >= CFG["MIN_EV"]:
            return legs, fair, price, ev
    return None


def open_combo(st, sp, n_legs):
    res = build(st, sp, n_legs)
    if not res:
        return False
    legs, fair, price, ev = res
    stake = min(CFG["STAKE"], st["combo_bankroll"])
    if stake <= 0:
        return False
    shares = stake / price
    c = {"id": f"C{len(st['combos_closed']) + len(st['combos_open']) + 1}",
         "n": n_legs, "prix": round(price, 4), "cote": round(1 / price, 2),
         "prediction": round(fair, 4), "ev": round(ev, 4), "mise": round(stake, 2),
         "parts": round(shares, 4), "gain_potentiel": round(shares - stake, 2),
         "ouvert_le": sp.iso(sp.now()), "statut": "ouvert", "gain": "", "regle_le": "",
         "legs": [{"slug": leg["slug"], "match": leg["match"], "ligue": leg["ligue"], "start": leg["start"],
                   "role": leg["role"], "pick": leg["pick"], "type": ROLE_FR[leg["role"]],
                   "token": leg["token"], "marche": leg["marche"], "prix": leg["ask"],
                   "proba": round(leg["fair"], 4), "resultat": None} for leg in legs]}
    st["combo_bankroll"] = round(st["combo_bankroll"] - stake, 4)
    st["combos_open"].append(c)
    desc = " + ".join(f"{leg['pick']}" for leg in c["legs"])
    sp.log(st, f"COMBO {n_legs} matchs : {desc} — cote {c['cote']:.2f}, prédiction {fair:.0%}, "
               f"prix {price:.3f}, avantage {ev:+.0%}, mise {stake:.2f} $")
    bot.telegram(f"🎰 PolyBot Combo (simulation)\n{desc}\nCote {c['cote']:.2f} · succès prédit {fair:.0%}\n"
                 f"Mise virtuelle {stake:.2f} $ → gain possible {c['gain_potentiel']:.2f} $")
    return True


def settle(st, sp):
    keep = []
    for c in st["combos_open"]:
        for leg in c["legs"]:
            if leg["resultat"] is not None:
                continue
            start = sp.parse_time(leg["start"])
            if start and sp.now() < start + dt.timedelta(hours=2):
                continue
            try:
                m = bot.http_json(f"{sp.GAMMA}/markets/{leg['marche']}")
            except Exception:  # noqa: BLE001
                continue
            if not m or not m.get("closed"):
                continue
            toks, prices = sp.jl(m.get("clobTokenIds")), [float(x) for x in sp.jl(m.get("outcomePrices"))]
            if leg["token"] in toks and prices:
                final = prices[toks.index(leg["token"])]
                if final in (0.0, 1.0) or abs(final - 0.5) < 1e-9:
                    leg["resultat"] = final
        res = [leg["resultat"] for leg in c["legs"]]
        if 0.0 in res or all(r is not None for r in res):
            mult = 0.0
            if 0.0 not in res:
                mult = 1.0
                for r in res:
                    mult *= r  # 1 = gagné ; 0,5 = match annulé (Polymarket rembourse à 50 ¢)
            payout = c["parts"] * mult
            c["gain"] = round(payout - c["mise"], 4)
            c["statut"] = "gagné" if mult == 1.0 else "perdu" if mult == 0.0 else "partiel"
            c["regle_le"] = sp.iso(sp.now())
            st["combo_bankroll"] = round(st["combo_bankroll"] + payout, 4)
            st["combos_closed"].append(c)
            ok = sum(1 for r in res if r == 1.0)
            sp.log(st, f"COMBO {c['statut'].upper()} ({ok}/{len(res)} bons) : {c['gain']:+.2f} $ "
                       f"→ cagnotte combos {st['combo_bankroll']:.2f} $")
            bot.telegram(f"🎰 Combo {c['statut']} ({ok}/{len(res)}) : {c['gain']:+.2f} $")
        else:
            keep.append(c)
    st["combos_open"] = keep


def run(st):
    import sports as sp  # import différé (sports importe combos)
    if os.environ.get("COMBOS", "1") != "1":
        return
    _init(st)
    settle(st, sp)
    today = sp.now().strftime("%Y-%m-%d")
    made = [c for c in st["combos_open"] + st["combos_closed"] if c["ouvert_le"].startswith(today)]
    if len(made) >= CFG["PER_DAY"] or st["combo_bankroll"] < CFG["STAKE"]:
        return
    # on ne cherche qu'après une nouvelle recherche de matchs (pas à chaque minute)
    if st.get("combo_try") == st.get("last_scan"):
        return
    st["combo_try"] = st.get("last_scan")
    sizes_done = {c["n"] for c in made}
    for n in CFG["LEGS"]:
        if len(made) >= CFG["PER_DAY"]:
            break
        if n in sizes_done:
            continue
        if open_combo(st, sp, n):
            made.append({"n": n})
