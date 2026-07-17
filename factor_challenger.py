#!/usr/bin/env python3
"""
Shadow challenger #2: whole-market factor strategy (deployed config from
factor_deploy_config.json). Virtual $100k, no broker orders. Recomputes the
deterministic factor equity curve on fresh warehouse data each run, slices from
inception, and writes a combined report: champion (live paper acct) vs
challenger-1 (fixed-8 + kill-switch) vs factor challenger (this) vs the
fast-re-entry variants (shadow overlay + LIVE paper account PA3POVULKVSB).
"""
import os, sys, json, sqlite3, urllib.request
from datetime import datetime, timezone, timedelta
import numpy as np

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices, run, zscore

ROOT = "/root/hermes-trader"
OUT = f"{ROOT}/reports/strategy_search"
DB = f"{OUT}/challenger.db"
REPORT = f"{OUT}/challenger_vs_champion.md"
START_CASH = 100000.0
ET = timezone(timedelta(hours=-4))

def log(m): print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] {m}", flush=True)

def current_topk(C, V, cfg):
    """Replicate run()'s scoring at the latest date -> today's holdings list."""
    px, ret1 = C, C.pct_change()
    dollar = px * V
    d = px.index[-1]
    mom12 = (px.shift(21) / px.shift(252) - 1).loc[d]
    mom6 = (px / px.shift(126) - 1).loc[d]
    mom3 = (px / px.shift(63) - 1).loc[d]
    vol3 = ret1.rolling(63).std().loc[d]
    volgro = (dollar.rolling(63).mean() / dollar.rolling(252).mean() - 1).loc[d]
    liq = dollar.rolling(63).median().loc[d]
    elig = (liq.rank(ascending=False) <= cfg["liq_top"]) & (px.loc[d] > 5.0) & mom12.notna()
    uni = elig[elig].index
    w = cfg["weights"]
    score = (w[0]*zscore(mom12[uni]) + w[1]*zscore(mom6[uni]) + w[2]*zscore(mom3[uni])
             - w[3]*zscore(vol3[uni]) + w[4]*zscore(volgro[uni]))
    return list(score.nlargest(cfg["K"]).index)

def envf(p):
    d = {}
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip().strip('"')
    return d

def fastre_live_equity():
    """Equity of the LIVE fast-re-entry paper account (research keys)."""
    r = envf(f"{ROOT}/.env.research")
    req = urllib.request.Request("https://paper-api.alpaca.markets/v2/account",
        headers={"APCA-API-KEY-ID": r["RESEARCH_ALPACA_API_KEY"],
                 "APCA-API-SECRET-KEY": r["RESEARCH_ALPACA_SECRET_KEY"]})
    a = json.load(urllib.request.urlopen(req, timeout=30))
    return a["account_number"], float(a["equity"])

def main():
    cfg = json.load(open(f"{OUT}/factor_deploy_config.json"))
    log(f"deployed config: {cfg}")
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS factor_challenger(date TEXT PRIMARY KEY, equity REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS factor_meta(k TEXT PRIMARY KEY, v TEXT)")

    log("loading matrices...")
    C, V = load_matrices()
    log(f"matrix {C.shape}, last date {C.index[-1].date()}")
    # Kill-switch DROPPED as primary 2026-07-12 (John's call): premium ran ~21pp/yr
    # across 2019-2026 regimes (V-crash re-entry lag + rotation-bear whipsaw; 2022
    # with-switch -26% vs raw -9%) and only pays in a 2008-style grinding crash
    # (raw -50% vs -4%). Paper forward test runs NAKED; the switched curve is still
    # tracked side-by-side below so the debate stays data-fed. REVISIT before any
    # real money (vol-targeting / faster re-entry candidates).
    m, s = run(C, V, cfg["weights"], K=cfg["K"], REB=cfg["reb_days"],
               LIQ_TOP=cfg["liq_top"], kill_switch=False, start="2017-06-01")
    m_ks, s_ks = run(C, V, cfg["weights"], K=cfg["K"], REB=cfg["reb_days"],
                     LIQ_TOP=cfg["liq_top"], kill_switch=True, start="2017-06-01")
    # FAST RE-ENTRY overlay (2026-07-13 shootout winner): exposed unless SPY is
    # below BOTH its 200DMA and 20DMA at the prior close. Shadow twin of the
    # LIVE paper deployment on PA3POVULKVSB.
    spy = C["SPY"].dropna()
    out_mask = (spy < spy.rolling(200).mean()) & (spy < spy.rolling(20).mean())
    expo = (~out_mask).astype(float).shift(1)
    rets = s.pct_change().fillna(0.0)
    s_fr = (1 + expo.reindex(rets.index).fillna(1.0) * rets).cumprod()

    c.execute("CREATE TABLE IF NOT EXISTS factor_challenger_ks(date TEXT PRIMARY KEY, equity REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS factor_challenger_fastre(date TEXT PRIMARY KEY, equity REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS fastre_live(date TEXT PRIMARY KEY, equity REAL)")
    row = c.execute("SELECT v FROM factor_meta WHERE k='inception'").fetchone()
    INCEPTION = row[0] if row else str(s.index[-1].date())
    c.execute("INSERT OR REPLACE INTO factor_meta VALUES('inception',?)", (INCEPTION,))
    for series, table in [(s, "factor_challenger"), (s_ks, "factor_challenger_ks"),
                          (s_fr, "factor_challenger_fastre")]:
        fwd = series[series.index >= INCEPTION]
        if len(fwd):
            eq = fwd / fwd.iloc[0] * START_CASH
            for d, v in eq.items():
                c.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?)", (str(d.date()), float(v)))
    try:
        accno, live_eq = fastre_live_equity()
        c.execute("INSERT OR REPLACE INTO fastre_live VALUES(?,?)",
                  (str(datetime.now(ET).date()), live_eq))
        log(f"fast-re-entry LIVE {accno}: ${live_eq:,.2f}")
    except Exception as e:
        log(f"fastre live equity fetch failed: {e}")
    hold = current_topk(C, V, cfg)
    c.execute("INSERT OR REPLACE INTO factor_meta VALUES('holdings',?)", (json.dumps(hold),))
    c.execute("INSERT OR REPLACE INTO factor_meta VALUES('config',?)", (json.dumps(cfg),))
    c.commit()
    fc_last = c.execute("SELECT equity FROM factor_challenger ORDER BY date DESC LIMIT 1").fetchone()
    log(f"factor challenger equity ${fc_last[0]:,.2f} since {INCEPTION}")
    log(f"current top-{cfg['K']}: {hold[:10]}...")

    # ---- combined report ----
    ch = c.execute("SELECT date,equity,regime_on FROM challenger ORDER BY date").fetchall()
    cp = c.execute("SELECT date,equity FROM champion ORDER BY date").fetchall()
    fc = c.execute("SELECT date,equity FROM factor_challenger ORDER BY date").fetchall()
    fk = c.execute("SELECT date,equity FROM factor_challenger_ks ORDER BY date").fetchall()
    fr = c.execute("SELECT date,equity FROM factor_challenger_fastre ORDER BY date").fetchall()
    fl = c.execute("SELECT date,equity FROM fastre_live ORDER BY date").fetchall()
    def ret(rows): return (rows[-1][1]/rows[0][1]-1)*100 if len(rows) >= 1 and rows[0][1] else 0.0
    L = ["# Paper race — Champion vs Challengers (shadow + live)",
         f"_Updated {datetime.now(ET).isoformat()}_\n",
         "| Book | Since-inception | Latest equity | Days | Inception |",
         "|--|--|--|--|--|"]
    if cp: L.append(f"| **Champion** (live paper, 6-gate) | {ret(cp):+.2f}% | ${cp[-1][1]:,.2f} | {len(cp)} | {cp[0][0]} |")
    if ch: L.append(f"| **Challenger 1** (fixed-8 + kill-switch) | {ret(ch):+.2f}% | ${ch[-1][1]:,.2f} | {len(ch)} | {ch[0][0]} |")
    if fc: L.append(f"| **Challenger 2** (whole-market factor, naked) | {ret(fc):+.2f}% | ${fc[-1][1]:,.2f} | {len(fc)} | {fc[0][0]} |")
    if fl: L.append(f"| **Challenger 3** (factor + FAST RE-ENTRY, LIVE paper PA3POVULKVSB) | {ret(fl):+.2f}% | ${fl[-1][1]:,.2f} | {len(fl)} | {fl[0][0]} |")
    if fr: L.append(f"| _(shadow: factor + fast re-entry overlay)_ | {ret(fr):+.2f}% | ${fr[-1][1]:,.2f} | {len(fr)} | {fr[0][0]} |")
    if fk: L.append(f"| _(shadow: factor + old 200DMA kill-switch)_ | {ret(fk):+.2f}% | ${fk[-1][1]:,.2f} | {len(fk)} | {fk[0][0]} |")
    L += ["", f"Factor config: `{json.dumps(cfg)}`",
          f"Factor current top-{cfg['K']} holdings: {', '.join(hold)}", "",
          "## Factor challenger daily equity (last 12)", "| Date | Equity |", "|--|--|"]
    for d, v in fc[-12:]:
        L.append(f"| {d} | ${v:,.2f} |")
    open(REPORT, "w").write("\n".join(L) + "\n")
    log(f"report -> {REPORT}")

if __name__ == "__main__":
    main()
