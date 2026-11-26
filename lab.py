"""
PolyBot Laboratoire — tester en SIMULATION des « tendances » de sites de pronostics.

Chaque stratégie parie 1 $ fictif sur chaque match qui la remplit, MÊME SANS AVANTAGE,
pour mesurer la tendance telle quelle. Pour chaque pari on note la probabilité Pinnacle
sans marge : l'écart moyen prix payé / Pinnacle dit vite si la tendance est déjà dans le prix.

Stratégies actives (vérifiées sur Polymarket le 26/11/2026) :
  laliga_outsiders : La Liga, victoire de l'outsider coté entre 5 et 9 (prix 0,11 à 0,20)
  serieb_under25   : Serie B, moins de 2,5 buts (marché « O/U 2.5 » de Polymarket)
Non testables sur Polymarket (pas de marché « total de buts ») : Eredivisie, Championship, MLS.
"""
import datetime as dt
import os
import re

import bot


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


STAKE = _f("LAB_STAKE", 1)
HORIZON_H = _f("LAB_MAX_HOURS", 36)
MIN_MINUTES = _f("LAB_MIN_MINUTES", 15)
TOTALS_EVERY_MIN = _f("LAB_TOTALS_EVERY_MIN", 30)  # requêtes PinnFeed « total » : ~48 / jour au plus

STRATEGIES = {
    "laliga_outsiders": {"nom": "La Liga – outsiders cote 5 à 9", "lo": 1 / 9, "hi": 1 / 5},
    "serieb_under25": {"nom": "Serie B – moins de 2,5 buts", "series": 10676, "side": "Under", "line": 2.5},
}
ENABLED = [s.strip() for s in os.environ.get("LAB_STRATEGIES", ",".join(STRATEGIES)).split(",") if s.strip()]


def _init(st):
    lab = st.setdefault("lab", {})
    lab.setdefault("open", [])
    lab.setdefault("closed", [])
    lab.setdefault("strategies", {k: v["nom"] for k, v in STRATEGIES.items()})
    return lab


def _taken(lab, strat, slug):
    return any(b["strategie"] == strat and b["slug"] == slug for b in lab["open"] + lab["closed"])


def _place(st, sp, lab, strat, slug, match, start, pick, token, mid, rate, fair, ligue):
    try:
        ask, _size = sp.best_ask(token)
    except Exception:  # noqa: BLE001
        return None
    if ask is None or not 0.02 < ask < 0.98:
        return None
    cost = sp.cost_per_share(ask, rate)
    shares = STAKE / cost
    b = {"id": f"L{len(lab['open']) + len(lab['closed']) + 1}", "strategie": strat,
         "slug": slug, "match": match, "ligue": ligue, "coup_envoi": start, "selection": pick,
         "token": token, "marche": mid, "prix": round(ask, 4), "cout_part": round(cost, 4),
         "parts": round(shares, 4), "mise": STAKE,
         "proba_pinnacle": None if fair is None else round(fair, 4),
         "ecart_pinnacle": None if fair is None else round(fair - cost, 4),
         "ouvert_le": sp.iso(sp.now()), "statut": "ouvert", "gain": "", "regle_le": ""}
    lab["open"].append(b)
    vs = f"Pinnacle {fair:.0%}, écart {fair - cost:+.1%}" if fair is not None else "pas de cote Pinnacle"
    sp.log(st, f"LABO {STRATEGIES[strat]['nom']} : {match} / {pick} @ {ask:.3f} ({vs})")
    return b


# --------------------------------------------------------------- stratégie 1 : La Liga outsiders
def run_laliga(st, sp, lab):
    cfg = STRATEGIES["laliga_outsiders"]
    lo_t = sp.now() + dt.timedelta(minutes=MIN_MINUTES)
    hi_t = sp.now() + dt.timedelta(hours=HORIZON_H)
    for block in (st.get("watch") or {}).values():
        for g in block.get("games", []):
            if not str(g.get("slug", "")).startswith("lal-"):
                continue
            start = sp.parse_time(g.get("start"))
            if not start or not lo_t <= start <= hi_t or _taken(lab, "laliga_outsiders", g["slug"]):
                continue
            for role in ("home", "away"):
                price = (g.get("pm") or {}).get(role)
                if price is None or not cfg["lo"] <= price <= cfg["hi"]:
                    continue
                tok, mid = (g.get("tok") or {}).get(role), (g.get("mid") or {}).get(role)
                if not tok or not mid:
                    continue
                pick = (g["t1"] if role == "home" else g["t2"]) + " gagne"
                _place(st, sp, lab, "laliga_outsiders", g["slug"], g["match"], g["start"], pick, tok, mid,
                       (g.get("rate") or {}).get(role, 0.05), (g.get("pin") or {}).get(role), g.get("ligue", ""))
                break


# --------------------------------------------------------------- stratégie 2 : Serie B under 2,5
def pm_totals(sp, series, line):
    """Marchés « O/U <line> » des matchs Polymarket d'une série (événements « More Markets »)."""
    t0 = sp.now()
    out, offset = [], 0
    while offset < 500:
        page = bot.http_json(f"{sp.GAMMA}/events", {
            "series_id": series, "closed": "false", "limit": 50, "offset": offset,
            "end_date_min": sp.iso(t0), "end_date_max": sp.iso(t0 + dt.timedelta(hours=HORIZON_H + 2))})
        for e in page:
            for m in e.get("markets") or []:
                if m.get("sportsMarketType") != "totals":
                    continue
                try:
                    if abs(float(m.get("line")) - line) > 1e-9:
                        continue
                except (TypeError, ValueError):
                    continue
                outs, toks = sp.jl(m.get("outcomes")), sp.jl(m.get("clobTokenIds"))
                if len(outs) != 2 or len(toks) != 2:
                    continue
                title = re.sub(r"\s*-\s*More Markets\s*$", "", e.get("title", ""), flags=re.I)
                start = sp.parse_time(e.get("startTime")) or sp.parse_time(m.get("gameStartTime"))
                rate = float((m.get("feeSchedule") or {}).get("rate") or 0.05)
                out.append({"title": title, "slug": e.get("slug"), "start": start, "mid": m.get("id"),
                            "rate": rate, "tok": dict(zip(outs, toks))})
        if len(page) < 50:
            break
        offset += 50
    return out


def pinn_totals(st, sp, line):
    """Probabilités sans marge Over/Under de la ligne principale Pinnacle, si c'est <line>."""
    data = sp.pinn_get(st, "/odds", {"sport": "soccer", "market": "total", "main": "true",
                                     "live": "prematch", "limit": 1000})
    out = []
    for e in data.get("events") or []:
        if e.get("special") or e.get("parentId"):
            continue
        for m in e.get("markets") or []:
            if m.get("type") != "total" or int(m.get("period") or 0) != 0:
                continue
            try:
                if abs(float(m.get("line")) - line) > 1e-9:
                    continue
            except (TypeError, ValueError):
                continue
            probs = {str(o.get("name")).lower(): float(o["fairProb"])
                     for o in m.get("outcomes") or [] if o.get("fairProb") is not None}
            if "over" in probs and "under" in probs:
                out.append({"home": e.get("home"), "away": e.get("away"), "start": sp.parse_time(e.get("startTime")),
                            "league": (e.get("league") or {}).get("name") or "", "probs": probs})
    return out


def run_serieb(st, sp, lab):
    cfg = STRATEGIES["serieb_under25"]
    lab_t = lab.setdefault("last_totals", None)
    last = sp.parse_time(lab_t)
    if last and (sp.now() - last).total_seconds() < TOTALS_EVERY_MIN * 60:
        return
    lab["last_totals"] = sp.iso(sp.now())
    lo_t = sp.now() + dt.timedelta(minutes=MIN_MINUTES)
    games = [g for g in pm_totals(sp, cfg["series"], cfg["line"])
             if g["start"] and g["start"] >= lo_t and not _taken(lab, "serieb_under25", g["slug"])]
    if not games:
        return
    pinn = []
    if sp.PINN_KEY:
        try:
            pinn = pinn_totals(st, sp, cfg["line"])
        except Exception as e:  # noqa: BLE001
            sp.log(st, f"LABO Serie B : cotes Pinnacle indisponibles ({e})")
    for g in games:
        t1, t2 = sp.split_title(g["title"])
        pe, _flip = sp.match_pinn(g, t1, t2, pinn) if pinn else (None, None)
        fair = pe["probs"]["under"] if pe else None  # over/under ne dépend pas du sens domicile/extérieur
        tok = g["tok"].get(cfg["side"])
        if not tok:
            continue
        _place(st, sp, lab, "serieb_under25", g["slug"], g["title"], sp.iso(g["start"]),
               "Moins de 2,5 buts", tok, g["mid"], g["rate"], fair, (pe or {}).get("league", "Serie B"))


# --------------------------------------------------------------- règlement
def settle(st, sp, lab):
    keep = []
    for b in lab["open"]:
        start = sp.parse_time(b["coup_envoi"])
        if start and sp.now() < start + dt.timedelta(hours=2):
            keep.append(b)
            continue
        try:
            m = bot.http_json(f"{sp.GAMMA}/markets/{b['marche']}")
        except Exception:  # noqa: BLE001
            keep.append(b)
            continue
        toks = sp.jl((m or {}).get("clobTokenIds"))
        prices = [float(x) for x in sp.jl((m or {}).get("outcomePrices"))]
        if not m or not m.get("closed") or b["token"] not in toks or not prices:
            keep.append(b)
            continue
        final = prices[toks.index(b["token"])]
        if final not in (0.0, 1.0) and abs(final - 0.5) > 1e-9:
            keep.append(b)
            continue
        payout = b["parts"] * final
        b["gain"] = round(payout - b["mise"], 4)
        b["statut"] = "gagné" if final == 1.0 else "perdu" if final == 0.0 else "remboursé"
        b["regle_le"] = sp.iso(sp.now())
        lab["closed"].append(b)
        sp.log(st, f"LABO {b['statut'].upper()} {STRATEGIES.get(b['strategie'], {}).get('nom', b['strategie'])} : "
                   f"{b['match']} {b['gain']:+.2f} $")
    lab["open"] = keep


def stats(lab):
    """Bilan par stratégie (sert au résumé et au tableau de bord)."""
    out = {}
    for k, nom in lab.get("strategies", {}).items():
        bets = [b for b in lab["open"] + lab["closed"] if b["strategie"] == k]
        done = [b for b in lab["closed"] if b["strategie"] == k]
        ecarts = [b["ecart_pinnacle"] for b in bets if b.get("ecart_pinnacle") is not None]
        out[k] = {"nom": nom, "paris": len(bets), "regles": len(done),
                  "gagnes": sum(1 for b in done if b["statut"] == "gagné"),
                  "gain": round(sum(float(b["gain"] or 0) for b in done), 2),
                  "ecart_moyen": round(sum(ecarts) / len(ecarts), 4) if ecarts else None}
    return out


def run(st):
    import sports as sp  # import différé (sports importe lab)
    if os.environ.get("LAB", "1") != "1":
        return
    lab = _init(st)
    settle(st, sp, lab)
    fresh = lab.get("last_watch") != st.get("last_scan")  # nouveaux matchs analysés depuis le dernier passage
    lab["last_watch"] = st.get("last_scan")
    for key, fn in (("laliga_outsiders", run_laliga), ("serieb_under25", run_serieb)):
        if key == "laliga_outsiders" and not fresh:
            continue
        if key in ENABLED:
            try:
                fn(st, sp, lab)
            except Exception as e:  # noqa: BLE001
                sp.log(st, f"LABO {key} : erreur ({e})")
    lab["bilan"] = stats(lab)
