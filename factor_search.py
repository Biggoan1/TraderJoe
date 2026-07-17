#!/usr/bin/env python3
"""
qwen-driven factor-weight search over the whole-market factor lab.
Discipline: search/fitness on TRAIN (2017-06..2023-12) only; top candidates are then
validated OOS on TEST (2024-01..2026-07). Deploy pick = best train-fitness that
holds up out-of-sample. Matrices load once; each backtest is in-process (~3s).
"""
import os, sys, json, re, urllib.request
from datetime import datetime, timezone, timedelta

sys.path.insert(0, "/root/hermes-trader")
from factor_lab import load_matrices, run, spy_baseline

OUT = "/root/hermes-trader/reports/strategy_search"
LEDGER = f"{OUT}/factor_search_ledger.jsonl"
BOARD = f"{OUT}/factor_search_leaderboard.md"
QWEN = "http://10.100.0.13:8080/v1/chat/completions"
TRAIN_END = "2024-01-01"
TEST_START = "2024-01-01"
ROUNDS, PER_ROUND = 5, 6
FACTORS = ["mom12", "mom6", "mom3", "lowvol", "volgro"]

def log(m): print(f"[{datetime.now(timezone(timedelta(hours=-4))).strftime('%H:%M:%S')}] {m}", flush=True)

def clamp(v, lo, hi, d):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return d

def normalize(cfg):
    w = cfg.get("weights") or {}
    if isinstance(w, (list, tuple)):
        w = dict(zip(FACTORS, w))
    weights = [round(clamp(w.get(f), 0.0, 1.0, 0.0), 2) for f in FACTORS]
    if sum(weights) <= 0:
        return None
    return {"weights": weights,
            "K": int(clamp(cfg.get("K"), 10, 100, 20)),
            "reb_days": int(clamp(cfg.get("reb_days"), 5, 63, 21)),
            "liq_top": int(clamp(cfg.get("liq_top"), 200, 800, 600))}

def key(c): return (tuple(c["weights"]), c["K"], c["reb_days"], c["liq_top"])

def fitness(m): return round(m["cagr_pct"] - 0.5 * m["max_drawdown_pct"], 2)

def backtest(cfg, Cm, Vm, start="2017-06-01"):
    m, _ = run(Cm, Vm, cfg["weights"], K=cfg["K"], REB=cfg["reb_days"],
               LIQ_TOP=cfg["liq_top"], kill_switch=False, start=start)
    return m

def ask_qwen(board, tried):
    top = sorted(board, key=lambda r: r["fit"], reverse=True)[:8]
    view = [{"weights": dict(zip(FACTORS, r["cfg"]["weights"])), "K": r["cfg"]["K"],
             "reb_days": r["cfg"]["reb_days"], "liq_top": r["cfg"]["liq_top"],
             "train_cagr": round(r["m"]["cagr_pct"], 1), "train_maxDD": round(r["m"]["max_drawdown_pct"], 1),
             "train_sharpe": round(r["m"]["sharpe"], 2), "fitness": r["fit"]} for r in top]
    sysp = ("You are a quant researcher tuning a whole-market cross-sectional factor strategy. "
            "Propose factor-weight configs to maximize fitness = CAGR% - 0.5*maxDD%. "
            "Factors: mom12 (12-1 momentum), mom6, mom3, lowvol (penalizes high vol), volgro "
            "(dollar-volume growth). K = names held, reb_days = rebalance cadence (trading days), "
            "liq_top = eligible universe depth by liquidity. Learn from the leaderboard; diversify; "
            "avoid repeats. Do NOT include reasoning or <think>. Output ONLY a JSON array.")
    user = ("/no_think\n"
            f"SCHEMA: {{\"weights\":{{f: 0..1 for {FACTORS}}}, \"K\": 10..100, \"reb_days\": 5..63, \"liq_top\": 200..800}}\n"
            f"LEADERBOARD (train 2017-2023, best first):\n{json.dumps(view, indent=1)}\n"
            f"{len(tried)} configs tried. Propose {PER_ROUND} NEW diverse configs as a JSON array.")
    body = json.dumps({"model": "qwen3.6-35b", "temperature": 0.7, "max_tokens": 4000,
                       "messages": [{"role": "system", "content": sysp},
                                    {"role": "user", "content": user}]}).encode()
    try:
        req = urllib.request.Request(QWEN, data=body, headers={"Content-Type": "application/json"})
        content = json.loads(urllib.request.urlopen(req, timeout=240).read())["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  qwen failed: {e}"); return []
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
    content = re.sub(r"```(json)?|```", "", content).strip()
    i = content.find("[")
    if i == -1:
        return []
    frag = content[i:]
    try:
        return json.loads(frag)
    except json.JSONDecodeError:
        objs, depth, buf = [], 0, ""
        for ch in frag[1:]:
            if ch == "{": depth += 1
            if depth > 0: buf += ch
            if ch == "}":
                depth -= 1
                if depth == 0 and buf.strip():
                    try: objs.append(json.loads(buf))
                    except json.JSONDecodeError: pass
                    buf = ""
        if objs: log(f"  salvaged {len(objs)}")
        return objs

def main():
    log("loading matrices once...")
    C, V = load_matrices()
    Ct, Vt = C[C.index < TRAIN_END], V[V.index < TRAIN_END]
    log(f"train matrix {Ct.shape}, full {C.shape}")
    open(LEDGER, "w").close()
    board, tried = [], set()

    seeds = [
        {"weights": [1.0, 0, 0, 0, 0], "K": 20, "reb_days": 21, "liq_top": 600},
        {"weights": [0.5, 0.3, 0.2, 0, 0], "K": 20, "reb_days": 21, "liq_top": 600},
        {"weights": [0.5, 0.2, 0, 0, 0.3], "K": 20, "reb_days": 21, "liq_top": 600},
        {"weights": [0.4, 0.2, 0.1, 0.2, 0.2], "K": 50, "reb_days": 21, "liq_top": 600},
    ]
    def do(rd, raw):
        cfg = normalize(raw)
        if not cfg or key(cfg) in tried:
            return
        tried.add(key(cfg))
        try:
            m = backtest(cfg, Ct, Vt)
        except Exception as e:
            log(f"  R{rd} ERR {type(e).__name__}: {str(e)[:60]}"); return
        fit = fitness(m)
        board.append({"cfg": cfg, "m": m, "fit": fit})
        with open(LEDGER, "a") as f:
            f.write(json.dumps({"round": rd, "cfg": cfg, "train": {k: m[k] for k in
                    ("cagr_pct", "sharpe", "max_drawdown_pct")}, "fitness": fit}, default=str) + "\n")
        log(f"  R{rd} w={cfg['weights']} K={cfg['K']} reb={cfg['reb_days']} liq={cfg['liq_top']}"
            f" -> cagr={m['cagr_pct']:+6.1f}% dd={m['max_drawdown_pct']:4.1f}% sh={m['sharpe']:+.2f} fit={fit:+7.2f}")

    log("Round 0: seeds")
    for s in seeds: do(0, s)
    for rd in range(1, ROUNDS + 1):
        log(f"Round {rd}: asking qwen...")
        props = ask_qwen(board, tried)
        log(f"  qwen proposed {len(props)}")
        for p in props: do(rd, p)

    ranked = sorted(board, key=lambda r: r["fit"], reverse=True)
    log("=== OOS TEST (2024-01..2026-07) on top 4 ===")
    finals = []
    for r in ranked[:4]:
        mt = backtest(r["cfg"], C, V, start=TEST_START)
        finals.append({**r, "test": mt})
        log(f"  w={r['cfg']['weights']} K={r['cfg']['K']}: TRAIN cagr {r['m']['cagr_pct']:+.1f}%/DD {r['m']['max_drawdown_pct']:.0f}%"
            f"  ->  TEST cagr {mt['cagr_pct']:+.1f}%/DD {mt['max_drawdown_pct']:.0f}% sh {mt['sharpe']:+.2f}")
    spy_t = spy_baseline(C, start=TEST_START)
    log(f"  SPY TEST: cagr {spy_t['cagr_pct']:+.1f}% DD {spy_t['max_drawdown_pct']:.0f}%")
    # deploy pick: best train fitness whose TEST beats SPY and TEST DD <= 45
    pick = next((f for f in finals if f["test"]["cagr_pct"] > spy_t["cagr_pct"]
                 and f["test"]["max_drawdown_pct"] <= 45), finals[0] if finals else None)
    L = ["# Factor Search Leaderboard (train 2017-2023 / test 2024-2026)",
         f"_{len(tried)} configs; fitness = train CAGR% - 0.5*train maxDD%. SPY test cagr {spy_t['cagr_pct']:+.1f}%._\n",
         "| # | weights (m12/m6/m3/lv/vg) | K | reb | liq | trainCAGR | trainDD | trainSh | fit |",
         "|--|--|--|--|--|--|--|--|--|"]
    for i, r in enumerate(ranked[:15], 1):
        c, m = r["cfg"], r["m"]
        L.append(f"| {i} | {'/'.join(str(x) for x in c['weights'])} | {c['K']} | {c['reb_days']} | {c['liq_top']} |"
                 f" {m['cagr_pct']:+.1f}% | {m['max_drawdown_pct']:.1f}% | {m['sharpe']:+.2f} | {r['fit']:+.2f} |")
    L += ["", "## OOS test (top 4)", "| weights | K | test CAGR | test DD | test Sharpe |", "|--|--|--|--|--|"]
    for f in finals:
        L.append(f"| {'/'.join(str(x) for x in f['cfg']['weights'])} | {f['cfg']['K']} |"
                 f" {f['test']['cagr_pct']:+.1f}% | {f['test']['max_drawdown_pct']:.1f}% | {f['test']['sharpe']:+.2f} |")
    if pick:
        L += ["", f"## DEPLOY PICK\n`{json.dumps(pick['cfg'])}`",
              f"train {pick['m']['cagr_pct']:+.1f}%/DD{pick['m']['max_drawdown_pct']:.0f}% | "
              f"test {pick['test']['cagr_pct']:+.1f}%/DD{pick['test']['max_drawdown_pct']:.0f}%"]
        json.dump(pick["cfg"], open(f"{OUT}/factor_deploy_config.json", "w"))
    open(BOARD, "w").write("\n".join(L) + "\n")
    log(f"=== DONE. deploy pick -> {OUT}/factor_deploy_config.json ===")

if __name__ == "__main__":
    main()
