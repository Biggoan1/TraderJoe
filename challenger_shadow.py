#!/usr/bin/env python3
"""
Challenger shadow forward-test (paper A/B).
Tracks a VIRTUAL $100k portfolio running the strategy-search winner
(volatility_regime_filter_v1 on 8 megacaps) gated by the refined kill-switch
(SPY > 200DMA +3% band, 3-day confirm). No broker orders — it recomputes the
gated equity curve from inception each run (idempotent) and snapshots the live
champion account for a since-inception A/B. Run daily after the close.
"""
import os, sqlite3, json
from datetime import datetime, timedelta, timezone
import numpy as np
import requests
import yfinance as yf
from dotenv import load_dotenv
from strategy.portfolio_simulator import PortfolioSimulatorConfig
from strategy.portfolio_simulator_cli import run_simulation

ROOT = "/root/hermes-trader"
load_dotenv(os.path.join(ROOT, ".env"))
DB = os.path.join(ROOT, "reports", "strategy_search", "challenger.db")
REPORT = os.path.join(ROOT, "reports", "strategy_search", "challenger_vs_champion.md")
SYMS = ["AAPL","AMD","AMZN","GOOGL","META","MSFT","NVDA","TSLA"]
CFG = dict(max_open_positions=6, position_size_pct=0.2, top_n_per_event=2)
CTX_START = "2025-05-01"           # warmup context so 200DMA + lookbacks are hot
START_CASH = 100000.0
MA_LEN, BAND, CONFIRM = 200, 0.03, 3

ET = timezone(timedelta(hours=-4))
def log(m): print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] {m}", flush=True)
def today(): return datetime.now(ET).strftime("%Y-%m-%d")

def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS challenger(date TEXT PRIMARY KEY, equity REAL, regime_on INT, strat_ret REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS champion(date TEXT PRIMARY KEY, equity REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return c

def spy_regime():
    spy = yf.download("SPY", start="2024-06-01", end=None, progress=False, auto_adjust=True)["Close"].squeeze()
    idx = [str(d)[:10] for d in spy.index]
    ma = spy.rolling(MA_LEN).mean()
    state, invested, brun, arun = {}, True, 0, 0
    for i, d in enumerate(idx):
        m = ma.iloc[i]
        if np.isnan(m):
            state[d] = True; continue
        px = float(spy.iloc[i])
        below = px < m * (1 - BAND); above = px > m * (1 + BAND)
        brun = brun + 1 if below else 0
        arun = arun + 1 if above else 0
        if invested and brun >= CONFIRM: invested = False
        elif (not invested) and arun >= CONFIRM: invested = True
        state[d] = invested
    return state, idx

def _champ_headers():
    return {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY"),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY")}

def champion_history(since):
    """Champion (live paper acct) daily equity since inception, from Alpaca."""
    try:
        r = requests.get("https://paper-api.alpaca.markets/v2/account/portfolio/history",
                         params={"date_start": since, "timeframe": "1D"},
                         headers=_champ_headers(), timeout=20)
        j = r.json()
        out = []
        for t, e in zip(j.get("timestamp", []), j.get("equity", [])):
            if e:
                d = datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d")
                out.append((d, float(e)))
        return out
    except Exception as e:
        log(f"champion history failed: {e}")
        return []

def main():
    c = db()

    # --- challenger: recompute gated equity curve from inception to last data date ---
    log("running challenger sim (provider fallback for fresh bars)...")
    dates, eq = [], np.array([])
    # query the fresh warehouse dataset's actual end_date so the resolver picks it
    try:
        mani = json.load(open(os.path.join(ROOT, "market_data/manifests/fafo-universe-2016.json")))
        DATA_END = mani.get("end_date") or today()
    except Exception:
        DATA_END = today()
    for end in [DATA_END, today(), "2026-07-04"]:
        try:
            res = run_simulation("volatility_regime_filter_v1", SYMS, CTX_START, end,
                                 config=PortfolioSimulatorConfig(**CFG), allow_provider_fallback=True)
            d = [ep.timestamp[:10] for ep in res.equity_curve]
            if len(d) >= 2:
                dates, eq = d, np.array([ep.equity for ep in res.equity_curve], float)
                log(f"  sim ok (end={end}): {len(dates)} bars, last data date {dates[-1]}")
                break
        except Exception:
            continue
    if not len(dates):
        log("NO DATA available for challenger sim — cannot proceed"); return

    # inception = last available data date on first run (persisted); forward from there
    row = c.execute("SELECT v FROM meta WHERE k='inception'").fetchone()
    INCEPTION = row[0] if row else dates[-1]
    c.execute("INSERT OR REPLACE INTO meta VALUES('inception', ?)", (INCEPTION,))
    c.execute("INSERT OR REPLACE INTO meta VALUES('last_data_date', ?)", (dates[-1],))
    regime, _ = spy_regime()

    # --- champion: backfill live-account equity since inception (fair A/B baseline) ---
    hist = champion_history(INCEPTION)
    for d, e in hist:
        c.execute("INSERT OR REPLACE INTO champion VALUES(?,?)", (d, e))
    if hist:
        log(f"champion history: {len(hist)} days, latest ${hist[-1][1]:,.2f}")

    # inception day = $100k baseline; returns accrue on days STRICTLY after,
    # each gated by the prior trading day's regime state (no lookahead).
    fwd = [i for i in range(1, len(dates)) if dates[i] > INCEPTION]
    if not fwd:
        log("no forward days past inception yet — baseline only (awaiting fresh data)")
    equity = START_CASH
    rows = [(INCEPTION, START_CASH, int(regime.get(INCEPTION, True)), 0.0)]
    for i in fwd:
        ret = eq[i] / eq[i - 1] - 1
        on = regime.get(dates[i - 1], True)
        gret = ret if on else 0.0
        equity *= (1 + gret)
        rows.append((dates[i], equity, int(on), ret))
    for r in rows:
        c.execute("INSERT OR REPLACE INTO challenger VALUES(?,?,?,?)", r)
    c.commit()
    cur_reg = rows[-1][2]
    log(f"challenger equity: ${rows[-1][1]:,.2f}  regime={'RISK-ON' if cur_reg else 'CASH'}  "
        f"forward days={len(rows)}")

    # --- report ---
    ch = c.execute("SELECT date,equity,regime_on FROM challenger ORDER BY date").fetchall()
    cp = c.execute("SELECT date,equity FROM champion ORDER BY date").fetchall()
    ch0, cp0 = (ch[0][1] if ch else START_CASH), (cp[0][1] if cp else None)
    ch_ret = (ch[-1][1] / ch0 - 1) * 100 if ch else 0.0
    cp_ret = (cp[-1][1] / cp0 - 1) * 100 if cp and cp0 else 0.0
    L = ["# Paper A/B — Challenger (shadow) vs Champion (live)",
         f"_Inception {INCEPTION}. Challenger = volatility_regime_filter_v1 on 8 megacaps + "
         f"kill-switch (SPY>{MA_LEN}DMA +{BAND*100:.0f}% band, {CONFIRM}-day confirm), virtual ${START_CASH:,.0f}, no broker orders._",
         f"_Champion = live paper stock account. Updated {datetime.now(ET).isoformat()}_\n",
         "| | Since-inception return | Latest equity | Days |",
         "|--|--|--|--|",
         f"| **Challenger** (shadow) | {ch_ret:+.2f}% | ${ch[-1][1]:,.2f} | {len(ch)} |" if ch else "| Challenger | — | — | 0 |",
         f"| **Champion** (live) | {cp_ret:+.2f}% | ${cp[-1][1]:,.2f} | {len(cp)} |" if cp else "| Champion | — | — | 0 |",
         "",
         f"Challenger regime today: **{'RISK-ON (invested)' if cur_reg else 'RISK-OFF (cash)'}**",
         "",
         "## Challenger daily equity",
         "| Date | Equity | Regime |", "|--|--|--|"]
    for d, e, on in ch[-12:]:
        L.append(f"| {d} | ${e:,.2f} | {'on' if on else 'CASH'} |")
    open(REPORT, "w").write("\n".join(L) + "\n")
    log(f"=== report -> {REPORT} ===")
    log(f"A/B since {INCEPTION}: challenger {ch_ret:+.2f}%  vs  champion {cp_ret:+.2f}%")

if __name__ == "__main__":
    main()
