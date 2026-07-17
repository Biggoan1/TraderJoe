# Trader Joe web dashboard — read-only views over the agent's SQLite DBs, plus
# a command relay that forwards button presses to the hermes gateway webhook
# (the agent's reply is delivered to Telegram, same as a typed message).
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

BASE = "/root/hermes-trader"
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="Trader Joe dashboard")


def q(db, sql, args=()):
    con = sqlite3.connect(f"file:{os.path.join(BASE, db)}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def service_state():
    try:
        out = subprocess.run(
            ["systemctl", "show", "traderjoe", "-p", "ActiveState", "-p", "ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        props = dict(line.split("=", 1) for line in out.strip().splitlines() if "=" in line)
        return {"state": props.get("ActiveState", "unknown"), "since": props.get("ExecMainStartTimestamp", "")}
    except Exception:
        return {"state": "unknown", "since": ""}


def _env_keys():
    keys = {}
    for name in (".env", ".env.research"):
        try:
            for line in open(os.path.join(BASE, name)):
                line = line.strip()
                if "=" in line and "ALPACA" in line.split("=", 1)[0]:
                    k, v = line.split("=", 1)
                    keys[k] = v
        except Exception:
            pass
    return keys


def _alpaca_get(path, kid, sec, base="https://paper-api.alpaca.markets"):
    req = urllib.request.Request(
        base + path,
        headers={"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def _account(prefix):
    """Live account for a key namespace: '' = stock, 'CRYPTO_' = crypto. equity is
    live now; last_equity is the prior close, so their diff is today's P/L."""
    ks = _env_keys()
    kid, sec = ks.get(prefix + "ALPACA_API_KEY"), ks.get(prefix + "ALPACA_SECRET_KEY")
    if not (kid and sec):
        return None
    a = _alpaca_get("/v2/account", kid, sec)
    eq = float(a.get("equity") or 0)
    leq = float(a.get("last_equity") or 0)
    out = {"equity": eq, "cash": float(a.get("cash") or 0),
           "daily_pl": round(eq - leq, 2),
           "daily_pct": round((eq - leq) / leq * 100, 2) if leq else 0.0}
    pos = _alpaca_get("/v2/positions", kid, sec)
    out["positions"] = [{"symbol": p["symbol"], "qty": float(p["qty"]),
                         "market_value": float(p["market_value"]),
                         "unrealized_pl": float(p["unrealized_pl"]),
                         "today_pl": float(p.get("unrealized_intraday_pl") or 0)} for p in pos]
    return out


_cache = {}


def _cached(key, fn, ttl):
    now = time.time()
    c = _cache.get(key)
    if c and now - c["ts"] < ttl:
        return c["data"]
    try:
        data = fn()
    except Exception:
        data = None
    _cache[key] = {"ts": now, "data": data}
    return data


def live_account():
    return _cached("stock", lambda: _account(""), 10)


def crypto_state():
    return _cached("crypto", lambda: _account("CRYPTO_"), 15)


def fastre_state():
    """Factor + fast-re-entry strategy, live paper acct PA3POVULKVSB (research keys)."""
    return _cached("fastre", lambda: _account("RESEARCH_"), 15)


TICKER_STOCKS = [("SPY", "S&P 500"), ("QQQ", "Nasdaq"), ("DIA", "Dow"),
                 ("IWM", "Small caps"), ("SMH", "Semis"), ("XLF", "Financials"),
                 ("XLE", "Energy"),
                 ("SHY", "1-3y Tsy"), ("IEF", "7-10y Tsy"), ("TLT", "20y+ Tsy"),
                 ("AGG", "Agg bonds"), ("LQD", "IG credit"), ("HYG", "High yield"),
                 ("GLD", "Gold"), ("SLV", "Silver"), ("PPLT", "Platinum"),
                 ("PALL", "Palladium"), ("GDX", "Gold miners"), ("GDXJ", "Jr gold miners"),
                 ("GDXU", "Miners bull 3x"), ("GDXD", "Miners bear 3x"),
                 ("USO", "Oil (WTI)"), ("BNO", "Brent"), ("UNG", "Nat gas"),
                 ("XOP", "Oil & gas E&P")]
TICKER_CRYPTO = [("BTC/USD", "BTC"), ("ETH/USD", "ETH")]


def _snap_pct(s):
    last = (s.get("latestTrade") or {}).get("p") or (s.get("dailyBar") or {}).get("c")
    prev = (s.get("prevDailyBar") or {}).get("c")
    if not (last and prev):
        return None
    return {"price": round(float(last), 2), "pct": round((float(last) / float(prev) - 1) * 100, 2)}


def _market_today():
    """Market ticker: ETFs + BTC/ETH vs prior close, from the Alpaca data API
    (the SIP plan rides the main login, so any key pair works)."""
    ks = _env_keys()
    kid = ks.get("RESEARCH_ALPACA_API_KEY") or ks.get("ALPACA_API_KEY")
    sec = ks.get("RESEARCH_ALPACA_SECRET_KEY") or ks.get("ALPACA_SECRET_KEY")
    if not (kid and sec):
        return None
    out = []
    data = "https://data.alpaca.markets"
    try:
        snaps = _alpaca_get("/v2/stocks/snapshots?symbols=" +
                            ",".join(s for s, _ in TICKER_STOCKS), kid, sec, base=data)
        for sym, label in TICKER_STOCKS:
            p = _snap_pct(snaps.get(sym) or {})
            if p:
                out.append({"symbol": sym, "label": label, **p})
    except Exception:
        pass
    try:
        csnaps = _alpaca_get("/v1beta3/crypto/us/snapshots?symbols=" +
                             urllib.parse.quote(",".join(s for s, _ in TICKER_CRYPTO)),
                             kid, sec, base=data).get("snapshots", {})
        for sym, label in TICKER_CRYPTO:
            p = _snap_pct(csnaps.get(sym) or {})
            if p:
                out.append({"symbol": sym, "label": label, **p})
    except Exception:
        pass
    return out or None


def market_today():
    return _cached("market", _market_today, 60)


def spy_today():
    m = market_today()
    if not m:
        return None
    return next(({"price": t["price"], "pct": t["pct"]} for t in m if t["symbol"] == "SPY"), None)


def _swing_factor_corr(window=20, min_days=10):
    """Correlation of daily returns between the swing (champion) and factor
    (fastre_live) equity curves tracked by the challenger shadow in challenger.db.
    Low/negative = the two books are diversifying each other."""
    db = "reports/strategy_search/challenger.db"
    champ = {r["date"]: r["equity"] for r in q(db, "SELECT date, equity FROM champion")}
    fast = {r["date"]: r["equity"] for r in q(db, "SELECT date, equity FROM fastre_live")}
    days = sorted(set(champ) & set(fast))
    rc, rf = [], []
    for a, b in zip(days, days[1:]):
        if champ[a] and fast[a]:
            rc.append(champ[b] / champ[a] - 1)
            rf.append(fast[b] / fast[a] - 1)
    rc, rf = rc[-window:], rf[-window:]
    n = len(rc)
    if n < min_days:
        return {"corr": None, "days": n, "window": window}
    mc, mf = sum(rc) / n, sum(rf) / n
    cov = sum((x - mc) * (y - mf) for x, y in zip(rc, rf))
    vc = sum((x - mc) ** 2 for x in rc) ** 0.5
    vf = sum((y - mf) ** 2 for y in rf) ** 0.5
    if not (vc and vf):
        return {"corr": None, "days": n, "window": window}
    return {"corr": round(cov / (vc * vf), 2), "days": n, "window": window}


def swing_factor_corr():
    return _cached("corr", _swing_factor_corr, 600)


_CH_DB = "reports/strategy_search/challenger.db"


def metals_shadow_curve():
    """Full daily equity curve of the metals shadow sleeve (challenger.db)."""
    out = []
    for r in q(_CH_DB, "SELECT date, equity FROM metals_shadow ORDER BY date"):
        try:
            t = int(datetime.fromisoformat(r["date"]).timestamp() * 1000)
        except Exception:
            continue
        out.append({"t": t, "v": r["equity"]})
    return out


def metals_state():
    """Latest metals shadow equity + day-over-day change (virtual $100k sleeve)."""
    rows = q(_CH_DB, "SELECT date, equity FROM metals_shadow ORDER BY date")
    if not rows:
        return None
    last = rows[-1]["equity"]
    prev = rows[-2]["equity"] if len(rows) > 1 else last
    return {"equity": round(last, 2),
            "daily_pl": round(last - prev, 2),
            "daily_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
            "shadow": True}


def _metals_corr(window=20, min_days=10):
    """Correlation of daily returns: metals shadow vs swing (champion). The whole
    point of the metals sleeve is diversification, so low/negative is the goal."""
    met = {r["date"]: r["equity"] for r in q(_CH_DB, "SELECT date, equity FROM metals_shadow")}
    champ = {r["date"]: r["equity"] for r in q(_CH_DB, "SELECT date, equity FROM champion")}
    days = sorted(set(met) & set(champ))
    rm, rc = [], []
    for a, b in zip(days, days[1:]):
        if met[a] and champ[a]:
            rm.append(met[b] / met[a] - 1)
            rc.append(champ[b] / champ[a] - 1)
    rm, rc = rm[-window:], rc[-window:]
    n = len(rm)
    if n < min_days:
        return {"corr": None, "days": n, "window": window}
    mm, mc = sum(rm) / n, sum(rc) / n
    cov = sum((x - mm) * (y - mc) for x, y in zip(rm, rc))
    vm = sum((x - mm) ** 2 for x in rm) ** 0.5
    vc = sum((y - mc) ** 2 for y in rc) ** 0.5
    if not (vm and vc):
        return {"corr": None, "days": n, "window": window}
    return {"corr": round(cov / (vm * vc), 2), "days": n, "window": window}


def metals_corr():
    return _cached("metals_corr", _metals_corr, 600)


# Spot price per troy ounce for the metals tiles — Alpaca has no metals spot, so
# use yfinance front-month futures (near-spot). ETF symbol -> (futures, metal).
METALS_SPOT = {"GLD": ("GC=F", "gold"), "SLV": ("SI=F", "silver"),
               "PPLT": ("PL=F", "platinum"), "PALL": ("PA=F", "palladium")}


def _metals_spot():
    import yfinance as yf  # heavy import; only pulled on cache miss
    out = {}
    for etf, (fut, _metal) in METALS_SPOT.items():
        try:
            h = yf.Ticker(fut).history(period="5d")
            if len(h):
                out[etf] = round(float(h["Close"].iloc[-1]), 2)
        except Exception:
            pass
    return out


def metals_spot():
    # 10-min cache: spot barely moves intra-window and yfinance is slow/flaky.
    return _cached("metals_spot", _metals_spot, 600)


# Virtual shadow books tracked in challenger.db (no broker account). (_ks/_fastre
# factor twins are omitted — identical to factor_challenger until a regime flip.)
SHADOW_BOOKS = [
    ("metals_shadow", "Metals sleeve", "GLD/SLV/GDX buy & hold"),
    ("challenger", "Challenger", "vol-regime + kill-switch"),
    ("factor_challenger", "Factor (whole-mkt)", "top-25 momentum"),
]


def _shadow_books():
    out = []
    for table, label, desc in SHADOW_BOOKS:
        rows = q(_CH_DB, f"SELECT date, equity FROM {table} ORDER BY date")
        if not rows:
            continue
        first, last = rows[0]["equity"], rows[-1]["equity"]
        prev = rows[-2]["equity"] if len(rows) > 1 else last
        out.append({
            "label": label, "desc": desc, "equity": round(last, 2),
            "daily_pl": round(last - prev, 2),
            "daily_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
            "since_pct": round((last / first - 1) * 100, 2) if first else 0.0,
            "inception": rows[0]["date"],
        })
    return out


def shadow_books():
    return _cached("shadow_books", _shadow_books, 120)


def _portfolio_history(prefix, period, timeframe):
    """Equity curve for one account straight from Alpaca portfolio history."""
    ks = _env_keys()
    kid, sec = ks.get(prefix + "ALPACA_API_KEY"), ks.get(prefix + "ALPACA_SECRET_KEY")
    if not (kid and sec):
        return []
    try:
        h = _alpaca_get(f"/v2/account/portfolio/history?period={period}&timeframe={timeframe}",
                        kid, sec)
    except Exception:
        return []
    ts, eq = h.get("timestamp") or [], h.get("equity") or []
    return [{"t": t * 1000, "v": v} for t, v in zip(ts, eq) if v]


# UI range -> Alpaca portfolio/history (period, timeframe); intraday timeframes
# are only allowed for periods under 30 days.
CURVE_RANGES = {"today": ("1D", "15Min"), "7": ("1W", "1H"),
                "30": ("1M", "1D"), "90": ("3M", "1D"), "all": ("all", "1D")}
CURVE_LOOKBACK_DAYS = {"today": 1, "7": 7, "30": 30, "90": 90, "all": 10_000_000}


def _metals_curve_ranged(range_key):
    """Metals shadow is daily (challenger.db), not Alpaca — slice its own curve to
    the requested range. Daily granularity means 'today' has <2 points and simply
    won't draw, which is fine for a buy-and-hold sleeve."""
    curve = metals_shadow_curve()
    if not curve:
        return []
    days = CURVE_LOOKBACK_DAYS.get(range_key, 1)
    if days >= 10_000_000:
        return curve
    cutoff = (datetime.now() - timedelta(days=days)).timestamp() * 1000
    sliced = [p for p in curve if p["t"] >= cutoff]
    return sliced


@app.get("/api/curves")
def curves(range: str = "today"):
    period, timeframe = CURVE_RANGES.get(range) or CURVE_RANGES["today"]
    return _cached("curves:" + range, lambda: {
        "swing": _portfolio_history("", period, timeframe),
        "factor": _portfolio_history("RESEARCH_", period, timeframe),
        "crypto": _portfolio_history("CRYPTO_", period, timeframe),
        "metals": _metals_curve_ranged(range),
    }, 120)


def daily_pl(equity):
    """P/L since the start of the current (Eastern) day: latest portfolio value
    minus the last snapshot before today's midnight (else the first one today)."""
    if not equity:
        return None
    def parse(ts):
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    latest = equity[-1]["value"]
    before = [p for p in equity if (parse(p["ts"]) or midnight) < midnight]
    if before:
        base = before[-1]["value"]
    else:
        today = [p for p in equity if (parse(p["ts"]) or datetime.min) >= midnight]
        base = today[0]["value"] if today else None
    if base is None:
        return None
    return {"pl": round(latest - base, 2),
            "pct": round((latest - base) / abs(base) * 100, 2) if base else 0.0}


@app.get("/api/summary")
def summary():
    # snapshot rows in one batch differ by microseconds — group to the second
    equity = q("trades.db",
               "SELECT substr(created_at, 1, 19) AS ts, MAX(portfolio_value) AS value, MAX(cash) AS cash"
               " FROM portfolio_snapshots GROUP BY substr(created_at, 1, 19) ORDER BY ts")
    positions = []
    if equity:
        positions = q("trades.db",
                      "SELECT symbol, qty, market_value, unrealized_pl FROM portfolio_snapshots"
                      " WHERE substr(created_at, 1, 19) = ? AND symbol IS NOT NULL ORDER BY market_value DESC",
                      (equity[-1]["ts"],))
    recs = q("trades.db",
             "SELECT created_at, symbol, price, signal, ai_analysis, user_decision, order_status"
             " FROM recommendations ORDER BY id DESC LIMIT 30")
    approvals = q("trades.db",
                  "SELECT created_at, symbol, side, price, amount, status"
                  " FROM pending_approvals ORDER BY id DESC LIMIT 30")
    watchlist = [r["symbol"] for r in q("trades.db", "SELECT symbol FROM watchlist ORDER BY symbol")]
    history = q("trades_history.db",
                "SELECT symbol, side, entry_time, exit_time, entry_price, exit_price,"
                " quantity, pl, pl_percent FROM trades ORDER BY id DESC LIMIT 40")
    live, crypto, fastre = live_account(), crypto_state(), fastre_state()
    accounts = [a for a in (live, crypto, fastre) if a]
    combined = None
    if accounts:
        eq = sum(a["equity"] for a in accounts)
        pl = sum(a["daily_pl"] for a in accounts)
        prev = eq - pl
        combined = {"equity": round(eq, 2), "daily_pl": round(pl, 2),
                    "daily_pct": round(pl / prev * 100, 2) if prev else 0.0,
                    "accounts": len(accounts), "cash": round(sum(a["cash"] for a in accounts), 2)}
    return {
        "service": service_state(),
        "daily_pl": daily_pl(equity),
        "live": live,
        "crypto": crypto,
        "fastre": fastre,
        "combined": combined,
        "metals": metals_state(),
        "metals_corr": metals_corr(),
        "shadows": shadow_books(),
        "spy": spy_today(),
        "market": market_today(),
        "metals_spot": metals_spot(),
        "correlation": swing_factor_corr(),
        "history": history,
        "equity": equity,
        "positions": positions,
        "recommendations": recs,
        "approvals": approvals,
        "watchlist": watchlist,
    }


WEBHOOK_URL = os.environ.get("TJ_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/traderjoe-web")
WEBHOOK_SECRET = os.environ.get("TJ_WEBHOOK_SECRET", "")


class Command(BaseModel):
    message: str


@app.post("/api/command")
def command(cmd: Command):
    msg = cmd.message.strip()
    if not msg or len(msg) > 500:
        return JSONResponse({"ok": False, "error": "message empty or too long"}, status_code=400)
    if not WEBHOOK_SECRET:
        return JSONResponse({"ok": False, "error": "webhook secret not configured"}, status_code=500)
    body = json.dumps({"event": "web-button", "message": msg}).encode()
    sig = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        WEBHOOK_URL, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return JSONResponse({"ok": False, "error": f"gateway said {e.code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"),
                        headers={"Cache-Control": "no-cache, must-revalidate"})
