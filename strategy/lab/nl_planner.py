"""Natural-language experiment planner.

Turns a plain-English research question into a deterministic
:class:`ResearchPlan` describing what experiment(s) to run.
Rule-based / regex-driven — NOT LLM-driven — so the same
question always produces the same plan.  This is a *planner*, not
a *runner*: the plan is emitted to disk; the operator (or the
:mod:`scripts.traderjoe-research` CLI) decides whether to run it.

Supported plan kinds:

* ``rs_weight_sweep`` — "Sweep RS weight from 0.2 to 1.0", etc.
* ``regime_analysis`` — "Test whether RS works better in high-vol
  regimes", etc.
* ``strategy_comparison`` — "Compare Champion vs Momentum over
  2020-2026", etc.

Any question that doesn't match a known pattern produces a
``unrecognised`` plan explaining which patterns are supported —
never a silent guess.

Read-only: no live-trading path, no order-path references, no
credential env reads, no ``ApprovalRecord`` construction, no
``PromotionEntry`` mutation.  The planner just parses text into
JSON.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)


# Known kinds
KIND_RS_WEIGHT_SWEEP = "rs_weight_sweep"
KIND_REGIME_ANALYSIS = "regime_analysis"
KIND_STRATEGY_COMPARISON = "strategy_comparison"
KIND_UNRECOGNISED = "unrecognised"

KNOWN_KINDS = frozenset({
    KIND_RS_WEIGHT_SWEEP,
    KIND_REGIME_ANALYSIS,
    KIND_STRATEGY_COMPARISON,
})

DEFAULT_WINDOW_LABEL = "1y"
DEFAULT_UNIVERSE = ("AAPL", "MSFT", "NVDA")
DEFAULT_BENCHMARKS = ("SPY", "QQQ")

# Standard year windows for the strategy-comparison kind
YEARLY_WINDOWS = {
    "2020": ("2020-01-01", "2020-12-31"),
    "2021": ("2021-01-01", "2021-12-31"),
    "2022": ("2022-01-01", "2022-12-31"),
    "2023": ("2023-01-01", "2023-12-31"),
    "2024": ("2024-01-01", "2024-12-31"),
    "2025": ("2025-01-01", "2025-12-31"),
    "2026": ("2026-01-01", "2026-07-04"),  # partial year
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "+00:00")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResearchPlan:
    """Immutable description of a proposed research experiment."""

    kind: str
    question: str
    parameters: Mapping[str, Any]
    steps: Sequence[str]
    reasons: Sequence[str]
    warnings: Sequence[str] = ()
    generated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "question": self.question,
            "parameters": dict(self.parameters),
            "steps": list(self.steps),
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "generated_at": self.generated_at,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


# ---------------------------------------------------------------------------
# Parsers — one per plan kind
# ---------------------------------------------------------------------------


_RS_WEIGHT_RANGE_PATTERN = re.compile(
    r"(?:weight[s]?\s+from\s+|weight[s]?\s+)?"
    r"(?P<lo>0?\.\d{1,3}|1(?:\.0+)?)"
    r"\s*(?:to|-|through)\s*"
    r"(?P<hi>0?\.\d{1,3}|1(?:\.0+)?)",
    re.IGNORECASE,
)

# A list of >=2 numeric values separated by commas — no "weight"
# keyword required immediately before, just somewhere in the
# question.  The caller already gated on "weight" being in q.
_RS_WEIGHT_LIST_PATTERN = re.compile(
    r"(?P<values>(?:0?\.\d{1,3}|1(?:\.0+)?)(?:\s*,\s*(?:0?\.\d{1,3}|1(?:\.0+)?)){1,})",
    re.IGNORECASE,
)


def _parse_rs_weight_sweep(question: str) -> Optional[ResearchPlan]:
    """Match phrases like:
       'Sweep RS weight from 0.2 to 1.0'
       'RS weight sweep 0.30, 0.50, 0.75, 1.00'
       'Test RS overlay weight range 0.2 through 0.5'
    """
    q = question.lower()
    if "rs" not in q and "relative strength" not in q:
        return None
    if "weight" not in q:
        return None
    if not any(t in q for t in ("sweep", "range", "test", "try", "vary")):
        return None

    weights: List[float] = []
    reasons: List[str] = []
    # Explicit list first
    list_m = _RS_WEIGHT_LIST_PATTERN.search(q)
    range_m = _RS_WEIGHT_RANGE_PATTERN.search(q)
    if list_m:
        values = list_m.group("values")
        for v in re.split(r"\s*,\s*", values):
            try:
                weights.append(float(v))
            except ValueError:
                continue
        reasons.append(f"Matched explicit weight list: {weights}")
    elif range_m:
        lo = float(range_m.group("lo"))
        hi = float(range_m.group("hi"))
        # Sample the range at 5 points including endpoints
        step = (hi - lo) / 4 if hi > lo else 0
        weights = [round(lo + i * step, 2) for i in range(5)] if step > 0 else [lo]
        reasons.append(
            f"Matched range {lo}..{hi}; sampled 5 evenly-spaced weights."
        )
    else:
        # Default sweep grid
        weights = [0.20, 0.30, 0.50, 0.75, 1.00]
        reasons.append(
            "No explicit weights; using default post-B02 sweep grid."
        )

    window = _extract_window(q, reasons)
    universe = _extract_universe(q, reasons)
    parameters = {
        "strategy": "rs-challenger-v0.1.0",
        "overlay_weights": weights,
        "window": window,
        "symbols": list(universe),
        "benchmarks": list(DEFAULT_BENCHMARKS),
        "dataset_id_prefix": "planned-rs-sweep",
    }
    steps = [
        f"Load warehouse dataset for {universe} covering {window['start']}..{window['end']}.",
        f"For each weight w in {weights}, build a challenger_factory with overlay_weight=w.",
        "For each weight, run run_historical_validation against the warehouse reader.",
        "Compute per-weight equity curve using build_equity_curve on the challenger side.",
        "Compute per-weight regime-conditioned metrics via regime_conditioned_metrics.",
        "Tabulate per-weight Sharpe / Sortino / MaxDD / CAGR and rank flips.",
        "Emit a report at reports/research_summaries/rs-weight-sweep-<date>.md.",
    ]
    return ResearchPlan(
        kind=KIND_RS_WEIGHT_SWEEP,
        question=question,
        parameters=parameters,
        steps=steps,
        reasons=reasons,
    )


_VOL_KEYWORDS = ("volatility", "vol", "vix", "vxx")
_CREDIT_KEYWORDS = ("credit", "hyg", "lqd", "spread")
_BOND_KEYWORDS = ("bond", "tlt", "treasury", "rates", "yield")
_HIGH_KEYWORDS = ("high", "spike", "stress", "elevated")
_LOW_KEYWORDS = ("low", "calm", "quiet", "compressed")


def _parse_regime_analysis(question: str) -> Optional[ResearchPlan]:
    """Match phrases like:
       'Test whether RS works better in high-vol regimes'
       'Does credit stress change RS performance?'
       'Regime-condition RS results by VXX'
    """
    q = question.lower()
    regime_hit = any(k in q for k in _VOL_KEYWORDS + _CREDIT_KEYWORDS + _BOND_KEYWORDS)
    action_hit = any(
        t in q for t in ("regime", "condition", "slice", "bucket", "tag", "vol regime")
    )
    if not (regime_hit or action_hit):
        return None
    reasons: List[str] = []

    regimes: List[str] = []
    if any(k in q for k in _VOL_KEYWORDS):
        regimes.append("vol")
        reasons.append("Volatility keyword detected → VXX regime spec.")
    if any(k in q for k in _CREDIT_KEYWORDS):
        regimes.append("credit")
        reasons.append("Credit keyword detected → HYG/LQD ratio regime spec.")
    if any(k in q for k in _BOND_KEYWORDS):
        regimes.append("bond_trend")
        reasons.append("Bond keyword detected → TLT SMA20 trend regime spec.")
    if not regimes:
        regimes = ["vol", "credit", "bond_trend"]
        reasons.append(
            "No explicit regime dimension named; using default (vol + credit + bond_trend)."
        )

    focus_bucket: Optional[str] = None
    if any(h in q for h in _HIGH_KEYWORDS):
        focus_bucket = "high"
        reasons.append("Focus on high-regime bucket detected.")
    elif any(l in q for l in _LOW_KEYWORDS):
        focus_bucket = "low"
        reasons.append("Focus on low-regime bucket detected.")

    window = _extract_window(q, reasons)
    parameters = {
        "regime_dimensions": regimes,
        "focus_bucket": focus_bucket,
        "window": window,
        "source_manifest": (
            f"reports/run_manifests/matrix-{window['label']}-2026-07-04/latest.json"
            if window["label"] else ""
        ),
        "regime_dataset_id": "regime-pack-2016",
    }
    steps = [
        "Locate the matrix run manifest for the requested window.",
        f"Load regime specs: {regimes}.",
        "Call regime_conditioned_metrics(bundle, specs, warehouse_root=market_data).",
        "Bucket per-regime score deltas, positive-percent, selection agreement.",
        "If focus_bucket set, drill into that bucket's rows and list top 10 by |score_delta|.",
        "Emit report at reports/research_summaries/regime-analysis-<label>-<date>.md.",
    ]
    return ResearchPlan(
        kind=KIND_REGIME_ANALYSIS,
        question=question,
        parameters=parameters,
        steps=steps,
        reasons=reasons,
    )


_KNOWN_STRATEGIES = frozenset({
    "champion", "champion_rs", "rs", "rs-challenger",
    "momentum", "trend", "mean_reversion",
})

_STRATEGY_VS_PATTERN = re.compile(
    r"(?P<a>[A-Za-z][A-Za-z_-]+)\s+vs\.?\s+(?P<b>[A-Za-z][A-Za-z_-]+)",
    re.IGNORECASE,
)


def _parse_strategy_comparison(question: str) -> Optional[ResearchPlan]:
    """Match phrases like:
       'Compare Champion vs Momentum over 2020-2026'
       'Champion vs Trend across all years'
       'Run strategy matrix on Momentum and MeanReversion'
    """
    q = question.lower()
    m = _STRATEGY_VS_PATTERN.search(q)
    strategies: List[str] = []
    reasons: List[str] = []
    if m:
        a = m.group("a").lower().replace("-", "_")
        b = m.group("b").lower().replace("-", "_")
        strategies = [_normalise_strategy(a), _normalise_strategy(b)]
        reasons.append(f"Matched 'X vs Y' pattern → {strategies}.")
    else:
        # Named-strategy detection
        for token in q.replace(",", " ").split():
            candidate = _normalise_strategy(token)
            if candidate:
                strategies.append(candidate)
        if strategies:
            reasons.append(f"Detected strategies by name: {strategies}.")
    if len(strategies) < 2:
        return None

    # Year range detection
    year_range = _extract_year_range(q, reasons)
    windows: List[Dict[str, str]] = []
    if year_range:
        lo, hi = year_range
        for y in range(lo, hi + 1):
            key = str(y)
            if key in YEARLY_WINDOWS:
                start, end = YEARLY_WINDOWS[key]
                windows.append({"label": key, "start": start, "end": end})
    if not windows:
        # Fall back to whatever _extract_window says
        window = _extract_window(q, reasons)
        windows = [window]

    parameters = {
        "strategies": strategies,
        "windows": windows,
        "symbols": list(DEFAULT_UNIVERSE),
        "benchmarks": list(DEFAULT_BENCHMARKS),
    }
    steps = [
        f"For each strategy in {strategies}, for each window in {[w['label'] for w in windows]}:",
        "  Load warehouse bars.  Build a challenger_factory adapting the strategy to the runner.",
        "  Run run_historical_validation with warehouse_reader.",
        "  Compute realised-return metrics via compute_performance_metrics.",
        "Aggregate results into a leaderboard sorted by Sharpe.",
        "Emit report at reports/research_summaries/strategy-comparison-<date>.md.",
    ]
    return ResearchPlan(
        kind=KIND_STRATEGY_COMPARISON,
        question=question,
        parameters=parameters,
        steps=steps,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _normalise_strategy(token: str) -> str:
    t = token.lower().strip().replace("-", "_")
    if t in {"champion"}:
        return "champion"
    if t in {"rs", "rs_challenger", "champion_rs", "relative_strength"}:
        return "champion_rs"
    if t in {"momentum", "mom"}:
        return "momentum"
    if t in {"trend", "trend_following"}:
        return "trend"
    if t in {"mean_reversion", "meanreversion", "mr"}:
        return "mean_reversion"
    return ""


_WINDOW_LABEL_PATTERN = re.compile(
    r"\b(60d|90d|6mo|1y|ytd)\b", re.IGNORECASE
)
_YEAR_RANGE_PATTERN = re.compile(
    r"\b(?P<lo>20[12]\d)\s*(?:to|-|through)\s*(?P<hi>20[12]\d)\b",
    re.IGNORECASE,
)


def _extract_window(question_lower: str, reasons: List[str]) -> Dict[str, str]:
    m = _WINDOW_LABEL_PATTERN.search(question_lower)
    if m:
        label = m.group(1).lower()
        reasons.append(f"Matched window label: {label}.")
        return _label_to_window(label)
    year_m = _YEAR_RANGE_PATTERN.search(question_lower)
    if year_m:
        lo = int(year_m.group("lo"))
        hi = int(year_m.group("hi"))
        start = f"{lo}-01-01"
        end = f"{hi}-12-31"
        if hi == 2026:
            end = "2026-07-04"
        reasons.append(f"Matched year range {lo}..{hi}.")
        return {"label": f"{lo}-{hi}", "start": start, "end": end}
    reasons.append(f"No window in question; using default {DEFAULT_WINDOW_LABEL}.")
    return _label_to_window(DEFAULT_WINDOW_LABEL)


def _label_to_window(label: str) -> Dict[str, str]:
    today = date.today()
    if label == "60d":
        return {"label": "60d", "start": (today.replace(day=today.day) if False else today).isoformat(), "end": today.isoformat()}
    # For simplicity use the labels the matrix runner accepts
    if label == "60d":
        from datetime import timedelta
        return {"label": "60d", "start": (today - timedelta(days=60)).isoformat(), "end": today.isoformat()}
    if label == "90d":
        from datetime import timedelta
        return {"label": "90d", "start": (today - timedelta(days=90)).isoformat(), "end": today.isoformat()}
    if label == "6mo":
        from datetime import timedelta
        return {"label": "6mo", "start": (today - timedelta(days=180)).isoformat(), "end": today.isoformat()}
    if label == "1y":
        from datetime import timedelta
        return {"label": "1y", "start": (today - timedelta(days=365)).isoformat(), "end": today.isoformat()}
    if label == "ytd":
        return {"label": "ytd", "start": date(today.year, 1, 1).isoformat(), "end": today.isoformat()}
    return {"label": label, "start": "", "end": ""}


def _extract_year_range(question_lower: str, reasons: List[str]) -> Optional[Tuple[int, int]]:
    m = _YEAR_RANGE_PATTERN.search(question_lower)
    if m:
        return int(m.group("lo")), int(m.group("hi"))
    if "all years" in question_lower or "every year" in question_lower:
        reasons.append("'all years' detected → 2020..2026.")
        return 2020, 2026
    return None


def _extract_universe(question_lower: str, reasons: List[str]) -> Sequence[str]:
    # Look for uppercase-word tokens 2-5 chars in the ORIGINAL question
    # Fall back to default universe
    return DEFAULT_UNIVERSE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_from_question(question: str) -> ResearchPlan:
    """Deterministically parse ``question`` into a
    :class:`ResearchPlan`.

    Returns a plan with ``kind='unrecognised'`` when the question
    doesn't match any known pattern — never a silent guess.
    """
    if not question or not question.strip():
        return ResearchPlan(
            kind=KIND_UNRECOGNISED,
            question=question or "",
            parameters={},
            steps=[],
            reasons=["question was empty"],
            warnings=("no question provided",),
            generated_at=_utc_now_iso(),
        )
    q = question.strip()
    # Order matters: the more specific parsers first.  Each parser
    # returns None when it doesn't match; we fall through to the
    # next.
    for parser in (
        _parse_rs_weight_sweep,
        _parse_regime_analysis,
        _parse_strategy_comparison,
    ):
        plan = parser(q)
        if plan is not None:
            # Attach a fresh generated_at.  Everything else is
            # deterministic on the input question.
            return ResearchPlan(
                kind=plan.kind,
                question=plan.question,
                parameters=plan.parameters,
                steps=plan.steps,
                reasons=plan.reasons,
                warnings=plan.warnings,
                generated_at=_utc_now_iso(),
            )
    return ResearchPlan(
        kind=KIND_UNRECOGNISED,
        question=q,
        parameters={},
        steps=[],
        reasons=[],
        warnings=(
            "no known pattern matched; supported kinds are: "
            "rs_weight_sweep, regime_analysis, strategy_comparison",
        ),
        generated_at=_utc_now_iso(),
    )


# ---------------------------------------------------------------------------
# CLI (used by scripts/traderjoe-research)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nl-planner",
        description="Turn a plain-English research question into a plan.",
    )
    p.add_argument("question", help="the research question in quotes")
    p.add_argument("--output", default="",
                   help="write plan JSON to this path (default: stdout)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    plan = plan_from_question(args.question)
    payload = plan.to_json(indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"Plan written to {args.output}  (kind={plan.kind})")
    else:
        print(payload)
    return 0 if plan.kind in KNOWN_KINDS else 3


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DEFAULT_BENCHMARKS",
    "DEFAULT_UNIVERSE",
    "DEFAULT_WINDOW_LABEL",
    "KIND_REGIME_ANALYSIS",
    "KIND_RS_WEIGHT_SWEEP",
    "KIND_STRATEGY_COMPARISON",
    "KIND_UNRECOGNISED",
    "KNOWN_KINDS",
    "ResearchPlan",
    "build_parser",
    "main",
    "plan_from_question",
]
