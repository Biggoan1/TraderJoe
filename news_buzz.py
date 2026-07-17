#!/usr/bin/env python3
"""
News/buzz radar. Pulls the Alpaca news firehose (last 24h), maps articles to the
800-name universe, computes buzz = today's article count vs each symbol's trailing
baseline (stored in buzz.db), and has qwen read the headlines of the top spikes to
score sentiment (-1..1) + catalyst type. Output: buzz_report.md + buzz.db history.
Run hourly during market hours (cron) or standalone.
"""
import os, json, re, sqlite3, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

ROOT = "/root/hermes-trader"
load_dotenv(f"{ROOT}/.env.research")
KEY, SEC = os.environ["RESEARCH_ALPACA_API_KEY"], os.environ["RESEARCH_ALPACA_SECRET_KEY"]
OUT = f"{ROOT}/reports/strategy_search"
DB = f"{OUT}/buzz.db"
QWEN = "http://10.100.0.13:8080/v1/chat/completions"
TOP_BUZZ = 15
MIN_ARTICLES = 3

ET = timezone(timedelta(hours=-4))
def log(m): print(f"[{datetime.now(ET).strftime('%H:%M:%S')}] {m}", flush=True)

def fetch_news(hours=24, max_pages=40):
    """Firehose: all news in the window, paginated."""
    start = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    arts, token = [], None
    for _ in range(max_pages):
        p = {"start": start, "limit": 50, "sort": "desc"}
        if token: p["page_token"] = token
        url = "https://data.alpaca.markets/v1beta1/news?" + urllib.parse.urlencode(p)
        req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SEC})
        j = json.loads(urllib.request.urlopen(req, timeout=60).read())
        arts += j.get("news", [])
        token = j.get("next_page_token")
        if not token: break
    return arts

def main():
    uni = set(json.load(open(f"{OUT}/market_universe.json")))
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS news_counts(date TEXT, symbol TEXT, n INT, PRIMARY KEY(date,symbol))")
    c.execute("""CREATE TABLE IF NOT EXISTS buzz_scores(ts TEXT, symbol TEXT, n24 INT, baseline REAL,
                 ratio REAL, sentiment REAL, catalyst TEXT, headline TEXT)""")

    arts = fetch_news(24)
    log(f"fetched {len(arts)} articles (24h firehose)")
    counts, heads = {}, {}
    for a in arts:
        for s in a.get("symbols", []):
            if s in uni:
                counts[s] = counts.get(s, 0) + 1
                heads.setdefault(s, []).append(a.get("headline", "")[:140])
    today = datetime.now(ET).strftime("%Y-%m-%d")
    for s, n in counts.items():
        c.execute("INSERT OR REPLACE INTO news_counts VALUES(?,?,?)", (today, s, n))
    c.commit()

    # baseline: avg daily count over prior stored days (excl today); fallback = universe median
    hist = c.execute("SELECT symbol, AVG(n) FROM news_counts WHERE date < ? GROUP BY symbol", (today,)).fetchall()
    base = dict(hist)
    days_hist = c.execute("SELECT COUNT(DISTINCT date) FROM news_counts WHERE date < ?", (today,)).fetchone()[0]
    med = sorted(counts.values())[len(counts)//2] if counts else 1
    rows = []
    for s, n in counts.items():
        b = base.get(s) if days_hist >= 3 else None
        b = b if b and b > 0 else float(med)
        rows.append({"symbol": s, "n24": n, "baseline": round(b, 2), "ratio": round(n / b, 2),
                     "heads": heads[s][:5]})
    rows = [r for r in rows if r["n24"] >= MIN_ARTICLES]
    rows.sort(key=lambda r: r["ratio"] * (r["n24"] ** 0.5), reverse=True)
    top = rows[:TOP_BUZZ]
    log(f"{len(rows)} symbols with >= {MIN_ARTICLES} articles; scoring top {len(top)} with qwen")

    scored = {}
    if top:
        payload = {r["symbol"]: r["heads"] for r in top}
        sysp = ("You are a trading news analyst. For each symbol, given recent headlines, output "
                "sentiment (-1 bearish .. +1 bullish), catalyst (one of: earnings, guidance, M&A, "
                "product, regulatory, legal, analyst, macro, hype, other), and note (<=12 words). "
                "No <think>, no prose. Output ONLY a JSON object {SYM: {sentiment, catalyst, note}}.")
        body = json.dumps({"model": "qwen3.6-35b", "temperature": 0.2, "max_tokens": 3000,
                           "messages": [{"role": "system", "content": sysp},
                                        {"role": "user", "content": "/no_think\n" + json.dumps(payload)}]}).encode()
        try:
            req = urllib.request.Request(QWEN, data=body, headers={"Content-Type": "application/json"})
            content = json.loads(urllib.request.urlopen(req, timeout=240).read())["choices"][0]["message"]["content"]
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
            content = re.sub(r"```(json)?|```", "", content).strip()
            i = content.find("{")
            scored = json.loads(content[i:]) if i >= 0 else {}
        except Exception as e:
            log(f"qwen scoring failed: {e}")

    ts = datetime.now(ET).isoformat()
    L = [f"# News/Buzz Radar — {ts}",
         f"_{len(arts)} articles/24h; buzz = count vs trailing baseline (history: {days_hist}d); "
         f"sentiment by qwen3.6-35b_\n",
         "| Sym | 24h | Base | Buzz | Sent | Catalyst | Note | Top headline |",
         "|--|--|--|--|--|--|--|--|"]
    for r in top:
        sc = scored.get(r["symbol"], {})
        sent = sc.get("sentiment")
        c.execute("INSERT INTO buzz_scores VALUES(?,?,?,?,?,?,?,?)",
                  (ts, r["symbol"], r["n24"], r["baseline"], r["ratio"],
                   sent if isinstance(sent, (int, float)) else None,
                   str(sc.get("catalyst", "")), r["heads"][0] if r["heads"] else ""))
        L.append(f"| {r['symbol']} | {r['n24']} | {r['baseline']} | {r['ratio']}x | "
                 f"{sent if sent is not None else '—'} | {sc.get('catalyst','—')} | {sc.get('note','—')} | "
                 f"{(r['heads'][0] if r['heads'] else '')[:60]} |")
    c.commit()
    open(f"{OUT}/buzz_report.md", "w").write("\n".join(L) + "\n")
    log(f"report -> {OUT}/buzz_report.md")
    for r in top[:8]:
        sc = scored.get(r["symbol"], {})
        log(f"  {r['symbol']:6} {r['n24']:>3} arts ({r['ratio']}x) sent={sc.get('sentiment','—')} "
            f"{sc.get('catalyst','')} — {sc.get('note','')}")

if __name__ == "__main__":
    main()
