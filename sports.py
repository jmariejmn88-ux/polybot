#!/usr/bin/env python3
"""
PolyBot Sport — paris sportifs en SIMULATION (argent virtuel uniquement).

Idée : Pinnacle est le bookmaker le plus « juste » du monde. PinnFeed donne ses
cotes et la probabilité « sans marge » (fairProb) de chaque issue. Si Polymarket
vend une issue nettement moins cher que cette probabilité (frais compris), le
bot prend un pari virtuel, puis le règle quand Polymarket publie le résultat.

Couverture (plan PinnFeed gratuit = 2 sports) :
  - football : toutes les ligues présentes à la fois sur Pinnacle et Polymarket
    (grands championnats, divisions 2, MLS, coupes, compétitions continentales
    et internationales) — 1X2 sur le temps réglementaire ;
  - basket : NBA (vainqueur du match, prolongation comprise).

Aucun ordre réel n'est jamais envoyé. Dépendances : bibliothèque standard.
"""
import csv
import datetime as dt
import difflib
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

import bot
import combos
import lab

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
STATE_FILE = os.path.join(DATA, "sports_state.json")
CSV_FILE = os.path.join(DATA, "sports_trades.csv")
SUMMARY_FILE = os.path.join(DATA, "sports_summary.md")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PINN = "https://pinnfeed.io/v1"

PINN_KEY = os.environ.get("PINNFEED_KEY", "").strip()
ODDS_KEY = os.environ.get("ODDS_API_KEY", "").strip()
ODDS = "https://api.the-odds-api.com/v4"


def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


CFG = {
    "START_BANKROLL": _f("SPORTS_START_BANKROLL", 100),
    "STAKE": _f("SPORTS_STAKE", 2),            # mise de base en $
    "MIN_EDGE": _f("SPORTS_MIN_EDGE", 0.03),   # avantage minimum, frais compris
    "MAX_EDGE": _f("SPORTS_MAX_EDGE", 0.20),   # au-delà : erreur d'appariement probable
    "MIN_PRICE": _f("SPORTS_MIN_PRICE", 0.08),
    "MAX_PRICE": _f("SPORTS_MAX_PRICE", 0.92),
    "MIN_HOURS": _f("SPORTS_MIN_HOURS", 0.25),  # pas de pari < 15 min du coup d'envoi
    "MAX_HOURS": _f("SPORTS_MAX_HOURS", 36),
    "MAX_OPEN": _f("SPORTS_MAX_OPEN", 15),
    "MIN_BANKROLL": _f("SPORTS_MIN_BANKROLL", 10),
    "DAILY_PINN_CAP": _f("SPORTS_PINN_CAP", 900),  # quota gratuit : 1 000 / jour
    "KELLY_FRACTION": _f("SPORTS_KELLY_FRACTION", 0.25),
    "USE_KELLY": os.environ.get("SPORTS_USE_KELLY", "0") == "1",
    "MATCH_MIN": _f("SPORTS_MATCH_MIN", 0.80),  # similarité minimale des noms
}

# Tags Polymarket et slug PinnFeed
SPORTS = {
    "foot": {"tag": 100350, "pinn": "soccer", "draw": True},
    "nba": {"tag": 745, "pinn": "basketball", "draw": False},
}
ENABLED = [s.strip() for s in os.environ.get("SPORTS_LIST", "foot,nba").split(",") if s.strip()]

# Source de secours : The Odds API (plan gratuit = 500 crédits / mois).
# Une requête = une ligue = 1 crédit. On ne l'appelle que pour les ligues qui ont
# un match Polymarket à venir, ~5 fois par jour.
# nom : (clé The Odds API, série Polymarket, match nul possible)
ODDS_LEAGUES = {
    "epl": ("soccer_epl", 10188, True),
    "laliga": ("soccer_spain_la_liga", 10193, True),
    "bundesliga": ("soccer_germany_bundesliga", 10194, True),
    "seriea": ("soccer_italy_serie_a", 10203, True),
    "ligue1": ("soccer_france_ligue_one", 10195, True),
    "ucl": ("soccer_uefa_champs_league", 10204, True),
    "mls": ("soccer_usa_mls", 10189, True),
    "nba": ("basketball_nba", 10345, False),
}
ODDS_ENABLED = [s.strip() for s in os.environ.get(
    "ODDS_LEAGUES", "epl,laliga,bundesliga,seriea,ligue1").split(",") if s.strip()]
ODDS_RESERVE = _f("ODDS_RESERVE", 15)  # crédits gardés en réserve


PINN_SPORTS = [x.strip() for x in os.environ.get("PINN_SPORTS", ",".join(ENABLED)).split(",") if x.strip()]


def pinn_units():
    """Sports couverts par PinnFeed (si sa clé existe)."""
    return [k for k in PINN_SPORTS if k in SPORTS] if PINN_KEY else []


def odds_units():
    """Ligues couvertes par The Odds API, sans doublon avec PinnFeed."""
    if not ODDS_KEY:
        return []
    pu = pinn_units()
    out = []
    for k in ODDS_ENABLED:
        lg = ODDS_LEAGUES.get(k)
        if not lg:
            continue
        is_soccer = lg[0].startswith("soccer_")
        if ("foot" in pu and is_soccer) or (k in pu):
            continue  # déjà couvert par PinnFeed
        out.append(k)
    return out


def sources():
    """Sources actives : PinnFeed et/ou The Odds API (les deux peuvent tourner ensemble)."""
    forced = os.environ.get("SPORTS_SOURCE", "auto")
    src = []
    if pinn_units() and forced in ("auto", "pinnfeed"):
        src.append("pinnfeed")
    if odds_units() and forced in ("auto", "oddsapi"):
        src.append("oddsapi")
    return src


def source():
    """Compatibilité : première source active."""
    s = sources()
    return s[0] if s else None


# fréquence de recherche (min) et horizon (h) de chaque source
EVERY_MIN = {"pinnfeed": _f("SPORTS_EVERY_MIN", 10), "oddsapi": _f("ODDS_EVERY_MIN", 288)}
HORIZON_H = {"pinnfeed": _f("SPORTS_MAX_HOURS", 36), "oddsapi": _f("ODDS_MAX_HOURS", 24)}

CSV_HEADER = ["id", "sport", "ligue", "match", "coup_envoi", "selection", "token",
              "prix", "cout_part", "parts", "mise", "proba_pinnacle", "avantage",
              "kelly", "ouvert_le", "statut", "gain", "regle_le"]


# ----------------------------------------------------------------- utilitaires
def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    if not s:
        return None
    s = str(s).strip().replace(" ", "T")
    if s.endswith("+00"):
        s += ":00"
    s = s.replace("Z", "+00:00")
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def jl(v):
    """Polymarket renvoie certaines listes sous forme de texte JSON."""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return []
    return v or []


STOP = {"fc", "cf", "sc", "ac", "afc", "cd", "ud", "sd", "fk", "sk", "nk", "bk", "if",
        "club", "de", "del", "la", "le", "the", "and", "football", "futbol", "calcio",
        "sv", "vfb", "vfl", "tsg", "rcd", "ca", "cs", "as", "ss", "us", "sporting",
        "ec", "se", "aa", "cr", "fbc", "ks", "1", "04", "05", "07", "09"}


def norm(name):
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    toks = [t for t in s.split() if t not in STOP]
    return " ".join(toks) or s.strip()


def similar(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if short and short <= long_:      # « Magic » ⊂ « Orlando Magic »
        return 0.95
    return difflib.SequenceMatcher(None, a, b).ratio()


def cost_per_share(price, rate):
    """Prix + frais taker Polymarket (rate × p × (1 − p) par part)."""
    return price + rate * price * (1 - price)


def kelly(p, cost):
    if cost >= 1 or p <= cost:
        return 0.0
    return (p - cost) / (1 - cost)


# ----------------------------------------------------------------- état
def load_state():
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    st.setdefault("bankroll", CFG["START_BANKROLL"])
    st.setdefault("start_bankroll", CFG["START_BANKROLL"])
    st.setdefault("open", [])
    st.setdefault("closed", [])
    st.setdefault("pinn_calls", {})
    st.setdefault("log", [])
    st.setdefault("last_scan", None)
    return st


def save_state(st):
    os.makedirs(DATA, exist_ok=True)
    st["log"] = st["log"][-200:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, STATE_FILE)


def log(st, msg):
    line = f"{iso(now())} {msg}"
    print("[sport] " + line, flush=True)
    st["log"].append(line)


def append_csv(row):
    os.makedirs(DATA, exist_ok=True)
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(CSV_HEADER)
        w.writerow([row.get(k, "") for k in CSV_HEADER])


# ----------------------------------------------------------------- PinnFeed
class QuotaAtteint(Exception):
    pass


def pinn_get(st, path, params):
    day = now().strftime("%Y-%m-%d")
    calls = st["pinn_calls"]
    for k in list(calls):
        if k != day:
            del calls[k]
    if calls.get(day, 0) >= CFG["DAILY_PINN_CAP"]:
        raise QuotaAtteint(f"quota PinnFeed du jour atteint ({calls[day]})")
    calls[day] = calls.get(day, 0) + 1
    url = f"{PINN}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": PINN_KEY, "Accept": "application/json",
                                               "User-Agent": "polybot-paper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise QuotaAtteint("PinnFeed : trop de requêtes (429)") from e
        if e.code in (401, 403):
            raise RuntimeError("PinnFeed : clé refusée — vérifie le secret PINNFEED_KEY "
                               "et les 2 sports choisis (soccer, basketball)") from e
        raise


def pinn_moneylines(st, sport_slug):
    """Matchs à venir + probabilités sans marge (temps plein, ligne principale)."""
    out, offset = [], 0
    for _ in range(3):
        data = pinn_get(st, "/odds", {"sport": sport_slug, "market": "moneyline", "main": "true",
                                      "live": "prematch", "limit": 1000, "offset": offset})
        evs = data.get("events") or []
        for e in evs:
            if e.get("special") or e.get("parentId"):
                continue
            mk = [m for m in (e.get("markets") or [])
                  if m.get("type") == "moneyline" and int(m.get("period") or 0) == 0]
            if not mk:
                continue
            probs = {}
            for o in mk[0].get("outcomes") or []:
                if o.get("fairProb") is not None:
                    probs[str(o.get("name")).lower()] = float(o["fairProb"])
            if "home" not in probs or "away" not in probs:
                continue
            out.append({"home": e.get("home"), "away": e.get("away"),
                        "start": parse_time(e.get("startTime")),
                        "league": (e.get("league") or {}).get("name") or "",
                        "probs": probs, "updated": mk[0].get("updatedAt")})
        if len(evs) < 1000:
            break
        offset += 1000
    return out


# ----------------------------------------------------------------- The Odds API
def odds_get(st, sport_key):
    rem = st.get("odds_remaining")
    if rem is not None and rem <= ODDS_RESERVE:
        raise QuotaAtteint(f"crédits The Odds API presque épuisés ({rem} restants) : pause jusqu'au mois prochain")
    params = {"apiKey": ODDS_KEY, "bookmakers": "pinnacle", "markets": "h2h",
              "oddsFormat": "decimal", "dateFormat": "iso"}
    url = f"{ODDS}/sports/{sport_key}/odds?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "polybot-paper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            for h, k in (("x-requests-remaining", "odds_remaining"), ("x-requests-used", "odds_used")):
                v = r.headers.get(h)
                if v is not None:
                    try:
                        st[k] = int(float(v))
                    except ValueError:
                        pass
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise QuotaAtteint("The Odds API : trop de requêtes (429)") from e
        if e.code == 401:
            raise QuotaAtteint("The Odds API : clé refusée ou crédits épuisés (401) — vérifie ODDS_API_KEY") from e
        raise


def odds_moneylines(st, sport_key):
    """Probabilités Pinnacle sans marge (méthode multiplicative) au format commun."""
    out = []
    for e in odds_get(st, sport_key) or []:
        bk = next((b for b in e.get("bookmakers") or [] if b.get("key") == "pinnacle"), None)
        mk = next((m for m in (bk or {}).get("markets") or [] if m.get("key") == "h2h"), None)
        if not mk:
            continue
        inv = {}
        for o in mk.get("outcomes") or []:
            try:
                price = float(o["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 1:
                continue
            name = o.get("name")
            role = ("home" if name == e.get("home_team") else
                    "away" if name == e.get("away_team") else
                    "draw" if str(name).lower() == "draw" else None)
            if role:
                inv[role] = 1 / price
        if "home" not in inv or "away" not in inv:
            continue
        tot = sum(inv.values())
        out.append({"home": e.get("home_team"), "away": e.get("away_team"),
                    "start": parse_time(e.get("commence_time")), "league": e.get("sport_title") or sport_key,
                    "probs": {k: v / tot for k, v in inv.items()}, "updated": mk.get("last_update")})
    return out


# ----------------------------------------------------------------- Polymarket
def pm_games(tag, start_min, start_max, series=None):
    """Matchs Polymarket (marchés « moneyline ») dont le coup d'envoi est dans la fenêtre."""
    games, offset = [], 0
    filt = {"series_id": series} if series else {"tag_id": tag}
    while offset < 2500:  # pages de 50 (au-delà, Polymarket exige la compression gzip)
        page = bot.http_json(f"{GAMMA}/events", {
            **filt, "closed": "false", "limit": 50, "offset": offset,
            "end_date_min": iso(start_min - dt.timedelta(hours=1)),
            "end_date_max": iso(start_max + dt.timedelta(hours=1))})
        for e in page:
            ml = [m for m in e.get("markets") or [] if m.get("sportsMarketType") == "moneyline"]
            if not ml:
                continue
            start = parse_time(e.get("startTime")) or parse_time(ml[0].get("gameStartTime"))
            if not start or not (start_min <= start <= start_max):
                continue
            games.append({"title": e.get("title", ""), "slug": e.get("slug"),
                          "league": e.get("seriesSlug") or "", "start": start, "markets": ml})
        if len(page) < 50:
            break
        offset += 50
    return games


def split_title(title):
    parts = re.split(r"\s+vs\.?\s+|\s+v\s+|\s+@\s+", title, maxsplit=1, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (title, "")


def pm_selections(game, has_draw):
    """Liste des issues achetables : (nom lisible, rôle home/away/draw/not_*, token, prix Gamma, frais)."""
    t1, t2 = split_title(game["title"])
    sels = []
    for m in game["markets"]:
        outs = jl(m.get("outcomes"))
        toks = jl(m.get("clobTokenIds"))
        prices = [float(x) for x in jl(m.get("outcomePrices"))] or [None] * len(outs)
        rate = float((m.get("feeSchedule") or {}).get("rate") or 0.05) if m.get("feesEnabled", True) else 0.0
        if len(outs) != 2 or len(toks) != 2:
            continue
        if has_draw:
            # 3 marchés Oui/Non : victoire équipe 1, nul, victoire équipe 2
            q = (m.get("question") or "").lower()
            label = m.get("groupItemTitle") or ""
            if "draw" in q:
                role = "draw"
            else:
                role = "home" if similar(label, t1) >= similar(label, t2) else "away"
            yes = 0 if str(outs[0]).lower() == "yes" else 1
            sels.append((f"{label or role} — OUI", role, toks[yes], prices[yes], rate, m.get("id")))
            sels.append((f"{label or role} — NON", "not_" + role, toks[1 - yes], prices[1 - yes], rate, m.get("id")))
        else:
            # NBA : un seul marché, une issue par équipe
            for i, name in enumerate(outs):
                role = "home" if similar(name, t1) >= similar(name, t2) else "away"
                sels.append((str(name), role, toks[i], prices[i], rate, m.get("id")))
    return t1, t2, sels


def match_pinn(game, t1, t2, pinn_events):
    """Trouve le match Pinnacle correspondant. Renvoie (événement, inversé?) ou (None, None)."""
    best, best_s, flip = None, 0.0, False
    for pe in pinn_events:
        if not pe["start"] or abs((pe["start"] - game["start"]).total_seconds()) > 3 * 3600:
            continue
        s_dir = min(similar(t1, pe["home"]), similar(t2, pe["away"]))
        s_inv = min(similar(t1, pe["away"]), similar(t2, pe["home"]))
        s, inv = (s_dir, False) if s_dir >= s_inv else (s_inv, True)
        if s > best_s:
            best, best_s, flip = pe, s, inv
    if best is None or best_s < CFG["MATCH_MIN"]:
        return None, None
    return best, flip


def fair_for(role, probs, flip):
    """Probabilité Pinnacle de l'issue (rôle exprimé selon l'ordre du titre Polymarket)."""
    swap = {"home": "away", "away": "home", "draw": "draw"}
    base = role[4:] if role.startswith("not_") else role
    key = swap[base] if flip else base
    if key not in probs:
        return None
    p = probs[key]
    if role.startswith("not_"):
        return 1 - p
    return p


def best_ask(token):
    book = bot.http_json(f"{CLOB}/book", {"token_id": token})
    asks = [(float(a["price"]), float(a["size"])) for a in book.get("asks", [])]
    if not asks:
        return None, 0.0
    p = min(x for x, _ in asks)
    return p, sum(s for x, s in asks if x == p)


def watch_entry(g, t1, t2, sels, pe, flip):
    """Photo d'un match analysé, pour le tableau de bord (prix Polymarket vs Pinnacle)."""
    pm, rates, toks, mids = {}, {}, {}, {}
    for _label, role, tok, gprice, rate, mid in sels:
        if role in ("home", "away", "draw") and gprice is not None:
            pm[role], rates[role], toks[role], mids[role] = round(gprice, 4), rate, tok, mid
    pin, edge = {}, {}
    if pe:
        for role in pm:
            f = fair_for(role, pe["probs"], flip)
            if f is not None:
                pin[role] = round(f, 4)
                edge[role] = round(f - cost_per_share(pm[role], rates[role]), 4)
    return {"slug": g["slug"], "match": g["title"], "t1": t1, "t2": t2, "start": iso(g["start"]),
            "ligue": (pe or {}).get("league") or g.get("league", ""), "pm": pm, "pin": pin, "edge": edge,
            "tok": toks, "mid": mids, "rate": rates}


def clean_watch(st):
    """Retire les matchs commencés depuis plus de 3 h."""
    limit = now() - dt.timedelta(hours=3)
    for k in list(st.get("watch", {})):
        games = [x for x in st["watch"][k].get("games", []) if (parse_time(x.get("start")) or now()) > limit]
        if games:
            st["watch"][k]["games"] = games
        else:
            del st["watch"][k]


# ----------------------------------------------------------------- règlement
def settle(st):
    changed = False
    keep = []
    for b in st["open"]:
        start = parse_time(b["coup_envoi"])
        if start and now() < start + dt.timedelta(hours=2):
            keep.append(b)
            continue
        try:
            m = bot.http_json(f"{GAMMA}/markets/{b['marche']}")
        except Exception as e:  # noqa: BLE001
            log(st, f"règlement reporté ({b['match']}) : {e}")
            keep.append(b)
            continue
        if not m or not m.get("closed"):
            keep.append(b)
            continue
        toks = jl(m.get("clobTokenIds"))
        prices = [float(x) for x in jl(m.get("outcomePrices"))]
        if b["token"] not in toks or not prices:
            keep.append(b)
            continue
        final = prices[toks.index(b["token"])]
        if final not in (0.0, 1.0) and abs(final - 0.5) > 1e-9:
            keep.append(b)  # pas encore définitif
            continue
        payout = b["parts"] * final
        b["gain"] = round(payout - b["mise"], 4)
        b["statut"] = "gagné" if final == 1.0 else ("perdu" if final == 0.0 else "remboursé")
        b["regle_le"] = iso(now())
        st["bankroll"] = round(st["bankroll"] + payout, 4)
        st["closed"].append(b)
        append_csv(b)
        log(st, f"{b['statut'].upper()} {b['match']} / {b['selection']} : {b['gain']:+.2f} $ "
                f"→ bankroll {st['bankroll']:.2f} $")
        bot.telegram(f"⚽🏀 PolyBot Sport (simulation)\n{b['statut'].upper()} — {b['match']}\n"
                     f"{b['selection']} : {b['gain']:+.2f} $\nBankroll sport : {st['bankroll']:.2f} $")
        changed = True
    st["open"] = keep
    return changed


# ----------------------------------------------------------------- recherche de paris
def groups(st, taken, due):
    """Donne, ligue par ligue : (nom, nul possible, matchs Polymarket, cotes Pinnacle)."""
    skipped = st.setdefault("_skipped", [])
    t0 = now()
    smin = t0 + dt.timedelta(hours=CFG["MIN_HOURS"])
    if "pinnfeed" in due:
        smax = t0 + dt.timedelta(hours=HORIZON_H["pinnfeed"])
        for key in pinn_units():
            sp = SPORTS[key]
            try:
                yield key, sp["draw"], pm_games(sp["tag"], smin, smax), pinn_moneylines(st, sp["pinn"])
            except QuotaAtteint as e:
                log(st, f"PinnFeed : {e}")
                break
            except Exception as e:  # noqa: BLE001
                log(st, f"{key} : données indisponibles ({e})")
    if "oddsapi" in due:
        smax = t0 + dt.timedelta(hours=HORIZON_H["oddsapi"])
        for key in odds_units():
            sport_key, series, draw = ODDS_LEAGUES[key]
            try:
                games = [g for g in pm_games(None, smin, smax, series=series) if g["slug"] not in taken]
                if not games:  # pas de match à venir : on économise un crédit
                    skipped.append(key)
                    continue
                yield key, draw, games, odds_moneylines(st, sport_key)
            except QuotaAtteint as e:
                log(st, f"The Odds API : {e}")
                break
            except Exception as e:  # noqa: BLE001
                log(st, f"{key} : données indisponibles ({e})")


def scan(st, due=None):
    if not sources():
        log(st, "aucune clé de cotes (PINNFEED_KEY ou ODDS_API_KEY) : module sport en attente.")
        return 0
    due = sources() if due is None else due
    taken = {b["evt"] for b in st["open"]}
    new = 0
    found = list(groups(st, taken, due))
    skipped = st.pop("_skipped", [])
    if skipped:
        log(st, f"recherche : pas de match Polymarket dans les {int(HORIZON_H['oddsapi'])} h "
                f"pour {', '.join(skipped)} (aucun crédit utilisé)")
    watch = st.setdefault("watch", {})
    for key, has_draw, games, pinn in found:
        matched = 0
        cands = []
        snap = []
        for g in games:
            if g["slug"] in taken:
                continue
            t1, t2, sels = pm_selections(g, has_draw)
            pe, flip = match_pinn(g, t1, t2, pinn)
            snap.append(watch_entry(g, t1, t2, sels, pe, flip))
            if not pe:
                continue
            matched += 1
            for label, role, tok, gprice, rate, mid in sels:
                fair = fair_for(role, pe["probs"], flip)
                if fair is None or gprice is None:
                    continue
                # tri rapide avec le prix affiché, avant de lire le carnet d'ordres
                if fair - cost_per_share(gprice, rate) < CFG["MIN_EDGE"] - 0.02:
                    continue
                cands.append((g, pe, label, role, tok, rate, fair, mid))
        watch[key] = {"at": iso(now()), "games": snap}
        log(st, f"{key} : {len(games)} matchs Polymarket, {len(pinn)} Pinnacle, "
                f"{matched} appariés, {len(cands)} pistes")
        for g, pe, label, role, tok, rate, fair, mid in cands:
            if len(st["open"]) >= CFG["MAX_OPEN"] or g["slug"] in taken:
                continue
            if st["bankroll"] < CFG["MIN_BANKROLL"]:
                log(st, "bankroll sport sous le minimum : pause des paris")
                return new
            try:
                ask, size = best_ask(tok)
            except Exception:  # noqa: BLE001
                continue
            if ask is None or not (CFG["MIN_PRICE"] <= ask <= CFG["MAX_PRICE"]):
                continue
            cost = cost_per_share(ask, rate)
            edge = fair - cost
            if edge < CFG["MIN_EDGE"]:
                continue
            if edge > CFG["MAX_EDGE"]:
                log(st, f"ignoré (avantage {edge:.0%} suspect) : {g['title']} / {label}")
                continue
            k = kelly(fair, cost) * CFG["KELLY_FRACTION"]
            stake = st["bankroll"] * k if CFG["USE_KELLY"] else CFG["STAKE"]
            stake = min(max(stake, 0.0), st["bankroll"] * 0.10)
            shares = stake / cost
            if shares < 5:  # minimum Polymarket : 5 parts
                shares = 5.0
                stake = shares * cost
            if size and shares > size:
                shares = size
                stake = shares * cost
            if stake <= 0 or stake > st["bankroll"]:
                continue
            b = {"id": f"S{int(time.time())}{len(st['closed']) + len(st['open'])}",
                 "sport": key, "ligue": pe["league"] or g["league"], "match": g["title"],
                 "evt": g["slug"], "coup_envoi": iso(g["start"]), "selection": label,
                 "token": tok, "marche": mid, "prix": round(ask, 4), "cout_part": round(cost, 4),
                 "parts": round(shares, 4), "mise": round(stake, 4),
                 "proba_pinnacle": round(fair, 4), "avantage": round(edge, 4),
                 "kelly": round(k, 4), "ouvert_le": iso(now()), "statut": "ouvert",
                 "gain": "", "regle_le": ""}
            st["bankroll"] = round(st["bankroll"] - stake, 4)
            st["open"].append(b)
            taken.add(g["slug"])
            new += 1
            log(st, f"PARI {key} {g['title']} / {label} @ {ask:.3f} (Pinnacle {fair:.1%}, "
                    f"avantage {edge:+.1%}) mise {stake:.2f} $")
            bot.telegram(f"⚽🏀 PolyBot Sport (simulation)\nPari : {g['title']}\n{label} @ {ask:.2f}\n"
                         f"Pinnacle : {fair:.0%} · avantage {edge:+.1%}\nMise virtuelle : {stake:.2f} $")
    return new


# ----------------------------------------------------------------- résumé
def write_summary(st):
    cl = st["closed"]
    won = sum(1 for b in cl if b["statut"] == "gagné")
    lost = sum(1 for b in cl if b["statut"] == "perdu")
    pnl = sum(float(b["gain"] or 0) for b in cl)
    staked = sum(float(b["mise"]) for b in cl)
    exp = sum(float(b["avantage"]) * float(b["parts"]) for b in cl)
    open_stake = sum(float(b["mise"]) for b in st["open"])
    lines = [
        "# PolyBot Sport — simulation (argent virtuel)", "",
        f"Mis à jour : {iso(now())}", "",
        f"- Bankroll disponible : **{st['bankroll']:.2f} $** (départ {st['start_bankroll']:.0f} $)",
        f"- En jeu : {len(st['open'])} pari(s), {open_stake:.2f} $",
        f"- Réglés : {len(cl)} (gagnés {won}, perdus {lost})",
        f"- Résultat réel : **{pnl:+.2f} $**" + (f" (ROI {pnl / staked:+.1%})" if staked else ""),
        f"- Résultat « attendu » selon Pinnacle : {exp:+.2f} $",
        "", "Le résultat attendu sert de contrôle : sur peu de paris, le résultat réel "
        "varie beaucoup. Il faut plusieurs centaines de paris pour conclure.", "",
    ]
    if st["open"]:
        lines += ["## Paris en cours", "", "| Match | Sélection | Prix | Pinnacle | Mise |",
                  "|---|---|---|---|---|"]
        for b in st["open"]:
            lines.append(f"| {b['match']} ({b['coup_envoi'][:16]}) | {b['selection']} | "
                         f"{b['prix']:.2f} | {b['proba_pinnacle']:.0%} | {b['mise']:.2f} $ |")
        lines.append("")
    if cl:
        lines += ["## 15 derniers résultats", "", "| Match | Sélection | Statut | Gain |",
                  "|---|---|---|---|"]
        for b in cl[-15:][::-1]:
            lines.append(f"| {b['match']} | {b['selection']} | {b['statut']} | {float(b['gain']):+.2f} $ |")
        lines.append("")
    day = now().strftime("%Y-%m-%d")
    if "pinnfeed" in sources():
        lines.append(f"PinnFeed ({', '.join(pinn_units())}) — requêtes aujourd'hui : "
                     f"{st['pinn_calls'].get(day, 0)} / 1 000")
    if "oddsapi" in sources():
        lines.append(f"The Odds API ({', '.join(odds_units())}) — crédits restants ce mois-ci : "
                     f"{st.get('odds_remaining', '?')} / 500")
    with open(SUMMARY_FILE, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main(force_scan=False):
    st = load_state()
    # Chaque source a son rythme : PinnFeed toutes les 10 min, The Odds API quelques fois par jour.
    per = st.setdefault("last_scan_src", {})
    try:
        settle(st)
        clean_watch(st)
        due = []
        for src in sources():
            last = parse_time(per.get(src))
            if force_scan or not last or (now() - last).total_seconds() >= EVERY_MIN[src] * 60:
                due.append(src)
        if due:
            for src in due:
                per[src] = iso(now())
            st["last_scan"] = iso(now())
            scan(st, due)
        combos.run(st)  # combos fictifs : règlement + nouveaux combos à partir des matchs analysés
        lab.run(st)     # laboratoire de stratégies (tendances à tester, 1 $ fictif par pari)
    finally:
        write_summary(st)
        save_state(st)
    return st


if __name__ == "__main__":
    main(force_scan=True)
