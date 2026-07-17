#!/usr/bin/env python3
"""
Trader Joe — LLM-driven strategy search loop.
qwen3.6-35b (AI3) PROPOSES backtest configs -> traderjoe-simulate SCORES them on
the local warehouse -> risk-adjusted leaderboard feeds back to qwen -> repeat.
Ranks by risk-adjusted fitness with a hard max-drawdown guardrail.
Writes a JSONL experiment ledger + a markdown leaderboard.
"""
import json, subprocess, os, sys, time, urllib.request, re
from datetime import datetime, timezone, timedelta

ROOT = "/root/hermes-trader"
OUTDIR = os.path.join(ROOT, "reports", "strategy_search")
os.makedirs(OUTDIR, exist_ok=True)
LEDGER = os.path.join(OUTDIR, "ledger.jsonl")
BOARD_MD = os.path.join(OUTDIR, "leaderboard.md")

QWEN_URL = "http://10.100.0.13:8080/v1/chat/completions"
QWEN_MODEL = "qwen3.6-35b"

START, END = "2025-07-01", "2026-07-04"
ROUNDS = int(os.environ.get("SS_ROUNDS", "4"))
PER_ROUND = int(os.environ.get("SS_PER_ROUND", "6"))
DD_CAP = 25.0            # disqualify configs whose max drawdown exceeds this
MIN_TRADES = 5          # disqualify near-inactive configs

STRATEGIES = [
    "champion-v0.4.0", "momentum-v0.1.0", "trend", "mean_reversion",
    "rsi_mean_reversion_v1", "volatility_regime_filter_v1", "sector_rotation_daily_v1",
]
POOL = ["AAPL","MSFT","NVDA","AMD","AMZN","AVGO","GOOGL","META","NFLX","TSLA",
        "SPY","QQQ","XLK","XLF","XLE","XLV","XLY","XLI","XLU","XLP","TLT","HYG"]

def log(msg):
    ts = datetime.now(timezone(timedelta(hours=-4))).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def clamp(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))

def normalize(cfg):
    """Validate/clamp an LLM (or seed) config into a runnable one, or None."""
    strat = cfg.get("strategy")
    if strat not in STRATEGIES:
        return None
    syms = [s for s in (cfg.get("symbols") or []) if s in POOL]
    if len(syms) < 2:
        syms = ["AAPL","MSFT","NVDA","SPY","QQQ"]
    syms = sorted(set(syms))[:14]
    return {
        "strategy": strat,
        "symbols": syms,
        "max_open_positions": int(clamp(cfg.get("max_open_positions"), 2, 12, 6)),
        "position_size_pct": round(clamp(cfg.get("position_size_pct"), 0.05, 0.6, 0.2), 3),
        "top_n_per_event": int(clamp(cfg.get("top_n_per_event"), 1, 6, 2)),
    }

def key(cfg):
    return (cfg["strategy"], tuple(cfg["symbols"]), cfg["max_open_positions"],
            cfg["position_size_pct"], cfg["top_n_per_event"])

def score(cfg):
    cmd = [f"{ROOT}/.venv/bin/python", "-m", "strategy.portfolio_simulator_main",
           "--strategy", cfg["strategy"], "--symbols", *cfg["symbols"],
           "--start", START, "--end", END,
           "--max-open-positions", str(cfg["max_open_positions"]),
           "--position-size-pct", str(cfg["position_size_pct"]),
           "--top-n-per-event", str(cfg["top_n_per_event"]),
           "--json", "--no-write"]
    env = dict(os.environ, HERMES_CONTEXT="simulator")
    try:
        p = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    out = p.stdout.strip()
    if not out:
        return {"error": (p.stderr.strip().splitlines() or ["no output"])[-1][:180]}
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", out, re.S)
        if not m:
            return {"error": "unparseable"}
        d = json.loads(m.group(0))
    return d.get("metrics", d)

def fitness(m):
    if "error" in m or m.get("total_return_pct") is None:
        return -999.0
    dd = m.get("max_drawdown_pct") or 0.0
    tc = m.get("trade_count") or 0
    ret = m.get("total_return_pct") or 0.0
    if dd > DD_CAP or tc < MIN_TRADES:
        return -100.0 + ret
    return round(ret - 0.4 * dd, 3)   # net return minus drawdown penalty

def ask_qwen(board, tried):
    schema = {
        "strategy": STRATEGIES,
        "symbols": "list (2-14) chosen from the universe pool",
        "max_open_positions": "int 2-12",
        "position_size_pct": "float 0.05-0.6 (fraction of equity per position)",
        "top_n_per_event": "int 1-6",
    }
    sys_prompt = (
        "You are a quantitative trading-strategy researcher. You propose backtest "
        "configurations to maximize RISK-ADJUSTED return: high total return with LOW "
        "max drawdown (hard cap 25%). Higher-volatility names (TSLA, NVDA, AMD, META) "
        "can lift returns but watch drawdown. Learn from the leaderboard: repeat what "
        "works, vary what doesn't, avoid configs already tried. "
        "Do NOT include any reasoning, <think> blocks, or explanation. "
        "Output ONLY the JSON array of config objects, nothing else."
    )
    top = sorted(board, key=lambda r: r["fitness"], reverse=True)[:8]
    board_view = [{"strategy": r["cfg"]["strategy"], "symbols": r["cfg"]["symbols"],
                   "max_open_positions": r["cfg"]["max_open_positions"],
                   "position_size_pct": r["cfg"]["position_size_pct"],
                   "top_n_per_event": r["cfg"]["top_n_per_event"],
                   "return_pct": r["m"].get("total_return_pct"),
                   "max_drawdown_pct": r["m"].get("max_drawdown_pct"),
                   "sharpe": r["m"].get("sharpe"), "trades": r["m"].get("trade_count"),
                   "fitness": r["fitness"]} for r in top]
    user = (
        "/no_think\n"
        f"UNIVERSE POOL: {POOL}\n"
        f"KNOB SCHEMA: {json.dumps(schema)}\n"
        f"FITNESS = total_return_pct - 0.4*max_drawdown_pct (drawdown>25% or <5 trades disqualified).\n"
        f"Backtest window {START}..{END} (~1y daily).\n\n"
        f"LEADERBOARD SO FAR (best first):\n{json.dumps(board_view, indent=1)}\n\n"
        f"Already tried ({len(tried)} configs) — do NOT repeat these exact combos.\n\n"
        f"Propose {PER_ROUND} NEW, DIVERSE configs as a JSON array to climb the leaderboard."
    )
    body = json.dumps({"model": QWEN_MODEL, "temperature": 0.7, "max_tokens": 4000,
                       "messages": [{"role": "system", "content": sys_prompt},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(QWEN_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            content = json.loads(r.read())["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"  qwen call failed: {e}")
        return []
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)   # drop reasoning
    content = re.sub(r"```(json)?|```", "", content).strip()
    start = content.find("[")
    if start == -1:
        log("  qwen returned no JSON array")
        return []
    frag = content[start:]
    try:
        return json.loads(frag)                          # well-formed array
    except json.JSONDecodeError:
        pass
    objs, depth, buf = [], 0, ""                          # salvage: collect complete {..} objects
    for ch in frag[1:]:
        if ch == "{":
            depth += 1
        if depth > 0:
            buf += ch
        if ch == "}":
            depth -= 1
            if depth == 0 and buf.strip():
                try:
                    objs.append(json.loads(buf))
                except json.JSONDecodeError:
                    pass
                buf = ""
    if objs:
        log(f"  salvaged {len(objs)} configs from truncated array")
    else:
        log("  qwen JSON parse failed")
    return objs

def record(round_no, cfg, m, fit):
    row = {"ts": datetime.now(timezone(timedelta(hours=-4))).isoformat(),
           "round": round_no, "cfg": cfg, "metrics": m, "fitness": fit}
    with open(LEDGER, "a") as f:
        f.write(json.dumps(row) + "\n")

def run_config(round_no, raw, board, tried):
    cfg = normalize(raw)
    if not cfg:
        return
    k = key(cfg)
    if k in tried:
        return
    tried.add(k)
    m = score(cfg)
    fit = fitness(m)
    record(round_no, cfg, m, fit)
    board.append({"cfg": cfg, "m": m, "fitness": fit})
    if "error" in m:
        log(f"  R{round_no} {cfg['strategy']:<24} ERR {m['error'][:60]}")
    else:
        log(f"  R{round_no} {cfg['strategy']:<24} ret={m.get('total_return_pct'):+6.2f}%"
            f" dd={m.get('max_drawdown_pct'):5.2f}% sharpe={m.get('sharpe'):+5.2f}"
            f" trades={m.get('trade_count'):>3} fit={fit:+7.2f}  {cfg['symbols']}")

def main():
    log(f"=== STRATEGY SEARCH START — {ROUNDS} rounds x {PER_ROUND} + seeds ===")
    open(LEDGER, "w").close()
    board, tried = [], set()

    log("Round 0: seeding baselines (each strategy, default execution)")
    seed_syms = ["AAPL","MSFT","NVDA","AMD","META","TSLA","SPY","QQQ"]
    for strat in STRATEGIES:
        run_config(0, {"strategy": strat, "symbols": seed_syms,
                       "max_open_positions": 6, "position_size_pct": 0.2,
                       "top_n_per_event": 2}, board, tried)

    for rd in range(1, ROUNDS + 1):
        log(f"Round {rd}: asking qwen for {PER_ROUND} proposals...")
        proposals = ask_qwen(board, tried)
        log(f"  qwen proposed {len(proposals)} configs")
        for raw in proposals:
            run_config(rd, raw, board, tried)

    ranked = sorted([r for r in board if r["fitness"] > -100],
                    key=lambda r: r["fitness"], reverse=True)
    lines = ["# Strategy Search Leaderboard",
             f"_Generated {datetime.now(timezone(timedelta(hours=-4))).isoformat()} — "
             f"window {START}..{END}, fitness = return - 0.4*maxDD, DD cap {DD_CAP}%_\n",
             "| # | Strategy | Return% | MaxDD% | Sharpe | Win% | Trades | Fitness | Universe |",
             "|--|--|--|--|--|--|--|--|--|"]
    for i, r in enumerate(ranked[:15], 1):
        m, c = r["m"], r["cfg"]
        wr = m.get("win_rate")
        lines.append(f"| {i} | {c['strategy']} | {m.get('total_return_pct'):+.2f} | "
                     f"{m.get('max_drawdown_pct'):.2f} | {m.get('sharpe'):+.2f} | "
                     f"{(wr*100 if wr else 0):.0f} | {m.get('trade_count')} | "
                     f"{r['fitness']:+.2f} | {','.join(c['symbols'])} |")
    open(BOARD_MD, "w").write("\n".join(lines) + "\n")
    log(f"=== DONE. {len(tried)} configs tested. Leaderboard -> {BOARD_MD} ===")
    if ranked:
        w = ranked[0]
        log(f"WINNER: {w['cfg']['strategy']} ret={w['m'].get('total_return_pct'):+.2f}% "
            f"dd={w['m'].get('max_drawdown_pct'):.2f}% sharpe={w['m'].get('sharpe'):+.2f} "
            f"fit={w['fitness']:+.2f} universe={w['cfg']['symbols']}")

if __name__ == "__main__":
    main()
