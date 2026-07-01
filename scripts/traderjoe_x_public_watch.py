#!/usr/bin/env python3
"""Watch public X profiles for new posts using r.jina.ai public proxy.

Designed for Hermes cron no_agent jobs: print only when there are newly
observed posts; stay silent otherwise.  Per-handle failures (429/451/timeouts)
are treated as misses — they never cause the whole job to error.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import random
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# ---------------------------------------------------------------------------
# State lives alongside the profile dir so cron workdir doesn't matter
# ---------------------------------------------------------------------------
PROFILE_DIR = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes" / "profiles" / "traderjoe"))
STATE_DIR = PROFILE_DIR / "state"
STATE_FILE = STATE_DIR / "traderjoe_x_public_watch_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

HANDLES = [
    "unusual_whales",
    "traderTVLIVE",
    "hmeisler",
    "charliebilello",
    "LizAnnSonders",
    "ReformedBroker",
]

SECTION_MARKER_RE = re.compile(r"^##\s+.*posts?\b", re.IGNORECASE)
NOISE_EXACT = {"Pinned"}
NOISE_PREFIXES = (
    "Title:",
    "URL Source:",
    "Published Time:",
    "Markdown Content:",
)
AD_KEYWORDS = (
    "subscribe",
    "discount",
    "promo",
    "promotion",
    "giveaway",
    "use code",
    "sale",
    "academy",
    "sponsored",
    "sign up",
    "register now",
    "join now",
    "free trial",
)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"seen": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"seen": {}}

def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))

def prune_seen(state: dict, limit: int = 1200) -> None:
    seen = state.setdefault("seen", {})
    for source, items in list(seen.items()):
        if len(items) > limit:
            seen[source] = items[-limit:]

def mark_seen(state: dict, source: str, key: str) -> bool:
    seen = state.setdefault("seen", {})
    bucket = seen.setdefault(source, [])
    if key in bucket:
        return False
    bucket.append(key)
    return True

# ---------------------------------------------------------------------------
# HTTP fetch via curl — fast, reliable, handles redirects
# ---------------------------------------------------------------------------

def fetch_with_curl(url: str) -> str | None:
    """Return page text or None on any failure. Never raises.
    
    Uses aggressive timeouts — 3s per attempt — to avoid stalling the cron
    window when r.jina.ai or X blocks/slow-downs.  Returns the HTTP status
    code via the global _last_status so callers can short-circuit on 429/451.
    """
    ua = random.choice(USER_AGENTS)
    try:
        result = subprocess.run(
            [
                "curl", "-sS", "--max-time", "3",
                "-w", "\n%{http_code}",
                "-H", f"User-Agent: {ua}",
                "-H", "Accept: text/markdown, text/plain, text/html;q=0.9",
                url,
            ],
            capture_output=True, text=True, timeout=6,
        )
        output = result.stdout.rsplit("\n", 1)
        body = output[0] if len(output) > 1 else ""
        code = int(output[1]) if len(output) > 1 and output[1].isdigit() else result.returncode

        if code == 200 and body.strip():
            return body
        # Fast-fail on known block codes — caller should try other URLs
        if code in (429, 451, 503):
            return ""
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return None

def profile_urls(handle: str) -> list[str]:
    """Return URL variants for fetching a handle's profile.
    
    x.com is fastest (returns 451 immediately if blocked).
    www.x.com is a fallback that sometimes succeeds when x.com doesn't.
    twitter.com is EXCLUDED — it hangs indefinitely on blocked requests.
    """
    return [
        f"https://r.jina.ai/http://x.com/{handle}",
        f"https://r.jina.ai/http://www.x.com/{handle}",
    ]

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def clean_post_text(lines: list[str], handle: str) -> str:
    cleaned: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s in NOISE_EXACT:
            continue
        if any(s.startswith(p) for p in NOISE_PREFIXES):
            continue
        if s == f"@{handle}":
            continue
        if SECTION_MARKER_RE.match(s):
            continue
        if re.fullmatch(r"\d+(?::\d+)+", s):
            continue
        if s in {"Image", "Photo", "Video"}:
            continue
        cleaned.append(s)

    text = " ".join(cleaned)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def looks_like_ad(text: str) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in AD_KEYWORDS)

def parse_profile(handle: str) -> list[dict]:
    """Return list of post dicts for *handle*, or [] on any failure.
    
    Short-circuits: if the first URL returns empty string (429/451 block),
    skip the slow www.x.com fallback and move on.
    """
    status_re = re.compile(rf"https://x\.com/{re.escape(handle)}/status/(\d+)")

    for i, url in enumerate(profile_urls(handle)):
        raw = fetch_with_curl(url)
        # None = timeout/network error — try next URL
        # "" = 451/429 block — no point trying www.x.com, skip this handle
        if raw == "":
            break
        if not raw:
            continue

        lines = raw.splitlines()
        started = False
        buffer: list[str] = []
        posts: list[dict] = []
        page_seen: set[str] = set()

        for line in lines:
            if not started:
                if SECTION_MARKER_RE.match(line.strip()):
                    started = True
                continue

            match = status_re.search(line)
            if match:
                post_id = match.group(1)
                if post_id in page_seen:
                    buffer = []
                    continue
                page_seen.add(post_id)
                text = clean_post_text(buffer, handle)
                posts.append({
                    "id": post_id,
                    "text": text,
                    "url": f"https://x.com/{handle}/status/{post_id}",
                })
                buffer = []
            else:
                buffer.append(line)

        if posts:
            return posts

    return []

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    state = load_state()
    lines: list[str] = []

    for handle in HANDLES:
        posts = parse_profile(handle)
        for post in posts[:12]:
            key = f"{handle}:{post['id']}"
            if not mark_seen(state, handle, key):
                continue
            text = post["text"] or "[no text extracted]"
            if looks_like_ad(text):
                continue
            if len(text) > 260:
                text = text[:257].rstrip() + "..."
            lines.append(f"• @{handle}: {text}")
            lines.append(f"  {post['url']}")

    prune_seen(state)
    save_state(state)

    if lines:
        now = datetime.now(ET)
        print(f"🐦 X public watch update ({now:%a %b %d, %I:%M %p ET})")
        print("\n".join(lines))

    return 0

if __name__ == "__main__":
    raise SystemExit(main())
