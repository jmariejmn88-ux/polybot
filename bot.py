#!/usr/bin/env python3
"""
PolyBot — bot de PAPER TRADING (argent fictif) sur les marchés Polymarket
"Bitcoin / Ethereum Up or Down — 15 minutes".

Principe à chaque exécution (toutes les ~5 min via GitHub Actions) :
  1. Règle les paris fictifs dont le marché est terminé (gagné / perdu).
  2. Pour la fenêtre de 15 min en cours : estime la probabilité "Up" à partir
     du prix réel (Coinbase) et de la volatilité récente, la compare au prix
     proposé sur Polymarket, et "achète" (fictivement) le côté sous-évalué si
     l'avantage dépasse un seuil.
  3. Sauvegarde tout dans data/state.json et data/trades.csv.

Aucune clé, aucun portefeuille : ce script ne peut PAS dépenser d'argent réel.
Uniquement la bibliothèque standard Python (aucune installation requise).
"""
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request

# ----------------------------------------------------------------- réglages
def _f(name, default):
    return float(os.getenv(name, default))

CFG = {
    "START_BANKROLL": _f("START_BANKROLL", 10),   # mise de départ fictive ($)
    "STAKE": _f("STAKE", 1.0),                    # mise visée par pari ($)
    "MAX_STAKE_PCT": _f("MAX_STAKE_PCT", 0.25),   # jamais plus de 25 % du capital sur un pari
    "MIN_EDGE": _f("MIN_EDGE", 0.06),             # avantage minimum (6 points de proba)
    "FEE_RATE": _f("FEE_RATE", 0.07),             # frais Polymarket crypto : parts × 0,07 × p × (1-p)
    "MIN_SECONDS_LEFT": _f("MIN_SECONDS_LEFT", 120),
    "MAX_SECONDS_LEFT": _f("MAX_SECONDS_LEFT", 600),
    "MIN_PRICE": _f("MIN_PRICE", 0.05),
    "MAX_PRICE": _f("MAX_PRICE", 0.95),
    "MAX_TRADES_PER_DAY": _f("MAX_TRADES_PER_DAY", 20),
    "DAILY_LOSS_LIMIT": _f("DAILY_LOSS_LIMIT", 3.0),  # arrêt du jour si -3 $
    "MIN_BANKROLL": _f("MIN_BANKROLL", 1.0),          # arrêt total sous 1 $
    "ASSETS": os.getenv("ASSETS", "btc,eth"),
    # Règle de résolution (vérifiée le 19/11/2026 sur 1 344 marchés, 95,8 % de concordance) :
    #   moyenne de la DERNIÈRE minute ≥ moyenne de la minute AVANT le début (flux TWAP 60 s Chainlink)
    #   → modèle "close". Le modèle "twap" (moyenne des 15 min) concorde moins bien (84 %).
    "MODEL": os.getenv("MODEL", "close"),
    "EVENT_PAUSE_MIN": _f("EVENT_PAUSE_MIN", 30),    # pas de pari ±30 min autour d'une annonce
    "REPORT_HOUR_UTC": _f("REPORT_HOUR_UTC", 20),    # rapport Telegram quotidien vers 20h UTC
}
MODE = "PAPER"  # ce bot ne sait pas passer d'ordres réels
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

WINDOW = 900  # 15 minutes
PRODUCTS = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD"}
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
COINBASE = "https://api.exchange.coinbase.com"

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "state.json")
TRADES_FILE = os.path.join(DATA, "trades.csv")
SUMMARY_FILE = os.path.join(DATA, "summary.md")
CALENDAR_FILE = os.path.join(ROOT, "calendar.txt")
LEARNING_FILE = os.path.join(ROOT, "learning.md")


# ----------------------------------------------------------------- réseau
def http_json(url, params=None, tries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polybot-paper/1.0",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {url} a échoué : {last}")


def telegram(text):
    """Envoie un message Telegram si TELEGRAM_TOKEN et TELEGRAM_CHAT_ID sont définis."""
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:4000]}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                               data=data, timeout=15).read()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"Telegram : échec {e}")
        return False


def load_calendar():
    """calendar.txt : une annonce par ligne → « AAAA-MM-JJ HH:MM  description » (heure UTC)."""
    events = []
    if not os.path.exists(CALENDAR_FILE):
        return events
    with open(CALENDAR_FILE, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                when = dt.datetime.strptime(line[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
                events.append((when, line[16:].strip()))
            except ValueError:
                continue
    return events


def active_event(now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    pad = dt.timedelta(minutes=CFG["EVENT_PAUSE_MIN"])
    for when, label in load_calendar():
        if when - pad <= now <= when + pad:
            return f"{label} ({when:%d/%m %H:%M} UTC)"
    return None


# ----------------------------------------------------------------- données
def get_event(slug):
    res = http_json(f"{GAMMA}/events", {"slug": slug})
    if not res:
        return None
    ev = res[0]
    if not ev.get("markets"):
        return None
    m = ev["markets"][0]
    load = lambda v: json.loads(v) if isinstance(v, str) else (v or [])
    outcomes = load(m.get("outcomes"))
    tokens = load(m.get("clobTokenIds"))
    prices = [float(p) for p in load(m.get("outcomePrices"))]
    return {
        "title": ev.get("title") or m.get("question", slug),
        "closed": bool(m.get("closed")),
        "outcomes": outcomes,
        "tokens": dict(zip(outcomes, tokens)),
        "prices": dict(zip(outcomes, prices)),
        "min_size": float(m.get("orderMinSize") or 5),
    }


def best_ask(token_id):
    book = http_json(f"{CLOB}/book", {"token_id": token_id})
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    if not asks:
        return None, 0.0
    price = min(p for p, _ in asks)
    size = sum(s for p, s in asks if p == price)
    return price, size


def spot_and_candles(product, t_start):
    now = int(time.time())
    start = dt.datetime.fromtimestamp(now - 120 * 60, dt.timezone.utc).isoformat()
    end = dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()
    raw = http_json(f"{COINBASE}/products/{product}/candles",
                    {"granularity": 60, "start": start, "end": end})
    candles = sorted(raw, key=lambda c: c[0])  # [time, low, high, open, close, vol]
    tick = http_json(f"{COINBASE}/products/{product}/ticker")
    spot = float(tick["price"])
    # Prix de départ = moyenne de la minute qui précède la fenêtre (TWAP 60 s)
    open_price = None
    for c in candles:
        if c[0] == t_start - 60:
            open_price = (float(c[3]) + float(c[4])) / 2
            break
    closes = [float(c[4]) for c in candles]
    inwin = [float(c[4]) for c in candles if t_start <= c[0] < now - 60]  # minutes terminées
    return spot, open_price, closes, inwin


# ----------------------------------------------------------------- modèle
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def cost_per_share(price):
    """Prix + frais taker Polymarket (en $ par part)."""
    return price + CFG["FEE_RATE"] * price * (1 - price)


def vol_per_min(closes):
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return max(statistics.pstdev(rets) if len(rets) > 10 else 0.001, 1e-4)


def prob_up(spot, open_price, closes, secs_left, inwin=None, model=None):
    """
    Probabilité que le marché finisse « Up ».
    closes : clôtures 1 min des 2 dernières heures (volatilité)
    inwin  : clôtures 1 min déjà écoulées dans la fenêtre (pour le modèle twap)
    """
    model = model or CFG["MODEL"]
    sigma = vol_per_min(closes)
    r = max(secs_left / 60, 0.25)                 # minutes restantes
    if model == "twap" and inwin:
        e = 15 - r                                # minutes écoulées
        avg_known = sum(inwin) / len(inwin)
        mean = (avg_known * e + spot * r) / 15
        sd = spot * sigma * math.sqrt(r / 3) * r / 15
    else:                                         # prix final ≈ moyenne de la dernière minute
        mean = spot
        sd = spot * sigma * math.sqrt(max(r - 2 / 3, 0.25))
    z = (mean - open_price) / max(sd, 1e-9)
    return min(max(norm_cdf(z), 0.03), 0.97), sigma  # humilité : jamais 0 % ou 100 %


# ----------------------------------------------------------------- état
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"start_bankroll": CFG["START_BANKROLL"], "bankroll": CFG["START_BANKROLL"],
            "created": now_iso(), "open": [], "closed": [], "log": [], "halted": False}


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    st["log"] = st["log"][-300:]
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)


def now_iso():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(st, msg):
    line = f"{now_iso()}  {msg}"
    print(line)
    st["log"].append(line)


def today_stats(st):
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    opened = [p for p in st["open"] + st["closed"] if p["opened_at"].startswith(day)]
    pnl = sum(p["pnl"] for p in st["closed"] if p.get("settled_at", "").startswith(day))
    return len(opened), pnl


# ----------------------------------------------------------------- étapes
def settle(st):
    still = []
    for p in st["open"]:
        try:
            ev = get_event(p["slug"])
        except Exception as e:  # noqa: BLE001
            log(st, f"⚠️ règlement impossible pour {p['slug']} : {e}")
            still.append(p)
            continue
        final = ev and ev["closed"] and all(v in (0.0, 1.0) for v in ev["prices"].values())
        if not final:
            still.append(p)
            continue
        won = ev["prices"].get(p["side"]) == 1.0
        payout = p["shares"] if won else 0.0
        p.update(won=won, payout=round(payout, 4), pnl=round(payout - p["cost"], 4),
                 settled_at=now_iso())
        st["bankroll"] = round(st["bankroll"] + payout, 4)
        st["closed"].append(p)
        append_csv(p)
        log(st, f"{'✅ GAGNÉ' if won else '❌ PERDU'} {p['title']} [{p['side']}] "
                f"P&L {p['pnl']:+.2f} $ → capital {st['bankroll']:.2f} $")
    st["open"] = still


def try_trade(st, asset):
    now = int(time.time())
    t_start = now - now % WINDOW
    secs_left = t_start + WINDOW - now
    slug = f"{asset}-updown-15m-{t_start}"

    if any(p["slug"] == slug for p in st["open"] + st["closed"]):
        return log(st, f"{asset.upper()} : déjà positionné sur cette fenêtre")
    if not CFG["MIN_SECONDS_LEFT"] <= secs_left <= CFG["MAX_SECONDS_LEFT"]:
        return log(st, f"{asset.upper()} : {secs_left}s restantes, hors plage d'entrée")

    ev = get_event(slug)
    if not ev or ev["closed"]:
        return log(st, f"{asset.upper()} : marché {slug} introuvable ou fermé")

    spot, open_price, closes, inwin = spot_and_candles(PRODUCTS[asset], t_start)
    if not open_price:
        return log(st, f"{asset.upper()} : prix d'ouverture indisponible")
    p_up, sigma = prob_up(spot, open_price, closes, secs_left, inwin)
    model = {"Up": p_up, "Down": 1 - p_up}

    best = None
    for side in ("Up", "Down"):
        ask, size = best_ask(ev["tokens"][side])
        if ask is None or not CFG["MIN_PRICE"] <= ask <= CFG["MAX_PRICE"]:
            continue
        cps = cost_per_share(ask)
        edge = model[side] - cps
        if best is None or edge > best["edge"]:
            best = {"side": side, "ask": ask, "size": size, "cps": cps, "edge": edge}

    info = (f"{asset.upper()} ouverture {open_price:,.2f} spot {spot:,.2f} "
            f"σ/min {sigma*100:.3f}% → P(Up) modèle {p_up:.2f}")
    if not best or best["edge"] < CFG["MIN_EDGE"]:
        e = f"{best['edge']:+.3f} sur {best['side']} à {best['ask']:.2f}" if best else "aucun prix"
        return log(st, f"{info} | pas d'avantage suffisant ({e})")

    max_cost = st["bankroll"] * CFG["MAX_STAKE_PCT"]
    shares = max(CFG["STAKE"] / best["cps"], ev["min_size"])
    shares = min(shares, best["size"])
    cost = shares * best["cps"]
    if shares < ev["min_size"] or cost > max_cost or cost > st["bankroll"]:
        return log(st, f"{info} | signal {best['side']} (avantage {best['edge']:+.3f}) "
                       f"mais mise min {ev['min_size']:.0f} parts = {cost:.2f} $ > limite {max_cost:.2f} $")

    pos = {"slug": slug, "title": ev["title"], "asset": asset, "side": best["side"],
           "price": best["ask"], "shares": round(shares, 4), "cost": round(cost, 4),
           "model_p": round(model[best["side"]], 4), "edge": round(best["edge"], 4),
           "secs_left": secs_left, "spot": spot, "open_price": open_price,
           "sigma_min": round(sigma, 6), "fee": round(shares * (best["cps"] - best["ask"]), 4),
           "mode": MODE,
           "reason": f"modèle {model[best['side']]:.2f} vs prix {best['ask']:.2f} → avantage {best['edge']:+.3f}",
           "opened_at": now_iso()}
    st["bankroll"] = round(st["bankroll"] - cost, 4)
    st["open"].append(pos)
    log(st, f"{info} | 🟢 ACHAT fictif {pos['shares']:.2f} parts {best['side']} à "
            f"{best['ask']:.2f} (coût {cost:.2f} $, avantage {best['edge']:+.3f})")


def append_csv(p):
    new = not os.path.exists(TRADES_FILE)
    os.makedirs(DATA, exist_ok=True)
    with open(TRADES_FILE, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["mode", "ouvert", "réglé", "crypto", "marché", "côté", "prix", "parts",
                        "coût", "frais", "proba_modèle", "avantage", "secondes_restantes",
                        "prix_ouverture", "prix_entrée", "volatilité_min", "raison",
                        "gagné", "gain_net", "R"])
        w.writerow([p.get("mode", MODE), p["opened_at"], p["settled_at"], p["asset"].upper(),
                    p["title"], p["side"], p["price"], p["shares"], p["cost"], p.get("fee", ""),
                    p["model_p"], p["edge"], p.get("secs_left", ""), p.get("open_price", ""),
                    p.get("spot", ""), p.get("sigma_min", ""), p.get("reason", ""),
                    "oui" if p["won"] else "non", p["pnl"], round(p["pnl"] / p["cost"], 3)])


def write_summary(st):
    c = st["closed"]
    wins = sum(1 for p in c if p["won"])
    pnl = sum(p["pnl"] for p in c)
    locked = sum(p["cost"] for p in st["open"])
    equity = st["bankroll"] + locked
    lines = [
        "# 📊 PolyBot — paper trading (argent fictif)",
        "",
        f"_Mis à jour : {now_iso()}_",
        "",
        "| Indicateur | Valeur |", "|---|---|",
        f"| Capital de départ | {st['start_bankroll']:.2f} $ |",
        f"| Capital disponible | {st['bankroll']:.2f} $ |",
        f"| Engagé (paris en cours) | {locked:.2f} $ |",
        f"| **Valeur totale** | **{equity:.2f} $ ({(equity/st['start_bankroll']-1)*100:+.1f} %)** |",
        f"| Paris réglés | {len(c)} |",
        f"| Taux de réussite | {(wins/len(c)*100 if c else 0):.0f} % |",
        f"| Gain net cumulé | {pnl:+.2f} $ |",
        f"| Statut | {'⛔ arrêté' if st.get('halted') else '🟢 actif'} |",
        "", "## Derniers paris", "",
        "| Date | Marché | Côté | Prix | Résultat | Gain |", "|---|---|---|---|---|---|",
    ]
    for p in reversed(c[-15:]):
        lines.append(f"| {p['opened_at'][:16]} | {p['title']} | {p['side']} | {p['price']:.2f} | "
                     f"{'✅' if p['won'] else '❌'} | {p['pnl']:+.2f} $ |")
    for p in st["open"]:
        lines.append(f"| {p['opened_at'][:16]} | {p['title']} | {p['side']} | {p['price']:.2f} | ⏳ | — |")
    lines += ["", "## Journal (20 dernières lignes)", "", "```", *st["log"][-20:], "```"]
    with open(SUMMARY_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    gh = os.getenv("GITHUB_STEP_SUMMARY")
    if gh:
        with open(gh, "a") as fh:
            fh.write("\n".join(lines) + "\n")


def period_stats(st, since):
    c = [p for p in st["closed"] if p.get("settled_at", "") >= since]
    pnl = sum(p["pnl"] for p in c)
    staked = sum(p["cost"] for p in c)
    wins = sum(p["won"] for p in c)
    return c, pnl, staked, wins


def equity(st):
    return st["bankroll"] + sum(p["cost"] for p in st["open"])


def daily_report(st):
    now = dt.datetime.now(dt.timezone.utc)
    day = now.strftime("%Y-%m-%d")
    if now.hour < CFG["REPORT_HOUR_UTC"] or st.get("last_report") == day:
        return
    c, pnl, staked, wins = period_stats(st, day)
    allc = st["closed"]
    best = max(c, key=lambda p: p["pnl"], default=None)
    worst = min(c, key=lambda p: p["pnl"], default=None)
    msg = [f"📊 PolyBot — rapport du {now:%d/%m} (MODE : {MODE})",
           f"Capital : {equity(st):.2f} $ (départ {st['start_bankroll']:.2f} $)",
           f"Aujourd'hui : {len(c)} paris, {wins} gagnés, gain net {pnl:+.2f} $",
           f"Depuis le début : {len(allc)} paris, réussite "
           f"{(sum(p['won'] for p in allc)/len(allc)*100 if allc else 0):.0f} %"]
    if best:
        msg.append(f"Meilleur : {best['title']} [{best['side']}] {best['pnl']:+.2f} $")
    if worst and worst is not best:
        msg.append(f"Pire : {worst['title']} [{worst['side']}] {worst['pnl']:+.2f} $")
    if len(allc) < 200:
        msg.append(f"⏳ Verdict fiable après 200 paris ({len(allc)}/200).")
    if telegram("\n".join(msg)):
        log(st, "📨 rapport quotidien envoyé sur Telegram")
    st["last_report"] = day


def weekly_review(st):
    """Chaque lundi : ajoute un bilan chiffré de la semaine dans learning.md."""
    now = dt.datetime.now(dt.timezone.utc)
    week = now.strftime("%G-W%V")
    if now.weekday() != 0 or st.get("last_review") == week:
        return
    since = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d")
    c, pnl, staked, wins = period_stats(st, since)
    lines = [f"", f"### Semaine {week} (bilan automatique)", "",
             f"- Paris réglés : {len(c)} — réussite {(wins/len(c)*100 if c else 0):.0f} %",
             f"- Gain net : {pnl:+.2f} $ — ROI {(pnl/staked*100 if staked else 0):+.1f} % par dollar misé",
             f"- Capital : {equity(st):.2f} $"]
    for asset in sorted({p["asset"] for p in c}):
        a = [p for p in c if p["asset"] == asset]
        lines.append(f"  - {asset.upper()} : {len(a)} paris, {sum(p['pnl'] for p in a):+.2f} $")
    for side in ("Up", "Down"):
        a = [p for p in c if p["side"] == side]
        if a:
            lines.append(f"  - Côté {side} : {len(a)} paris, réussite "
                         f"{sum(p['won'] for p in a)/len(a)*100:.0f} %")
    lines.append("- Leçon de la semaine : _à compléter (une seule règle, un seul changement testé)_")
    with open(LEARNING_FILE, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    st["last_review"] = week
    log(st, "📝 bilan hebdomadaire ajouté à learning.md")


def main():
    st = load_state()
    log(st, f"— exécution — MODE : {MODE}")
    settle(st)

    n_today, pnl_today = today_stats(st)
    event = active_event()
    if equity(st) < CFG["MIN_BANKROLL"]:
        if not st.get("halted"):
            telegram(f"⛔ PolyBot arrêté : capital {equity(st):.2f} $ sous le minimum.")
        st["halted"] = True
        log(st, "⛔ capital sous le minimum : bot arrêté définitivement")
    elif pnl_today <= -CFG["DAILY_LOSS_LIMIT"]:
        if st.get("paused_day") != now_iso()[:10]:
            telegram(f"⏸ PolyBot en pause jusqu'à demain : perte du jour {pnl_today:+.2f} $.")
            st["paused_day"] = now_iso()[:10]
        log(st, f"⏸ perte du jour {pnl_today:+.2f} $ ≥ limite : pause jusqu'à demain")
    elif event:
        log(st, f"📅 annonce économique en cours : {event} → aucun nouveau pari")
    elif n_today >= CFG["MAX_TRADES_PER_DAY"]:
        log(st, "⏸ nombre max de paris du jour atteint")
    elif not st.get("halted"):
        for asset in [a.strip() for a in CFG["ASSETS"].split(",") if a.strip() in PRODUCTS]:
            try:
                try_trade(st, asset)
            except Exception as e:  # noqa: BLE001
                log(st, f"⚠️ {asset.upper()} : erreur {e}")
    daily_report(st)
    weekly_review(st)
    save_state(st)
    write_summary(st)


if __name__ == "__main__":
    sys.exit(main())
