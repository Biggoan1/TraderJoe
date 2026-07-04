"""Regime tagger — `t_lab_regime_tagger`.

Slices an :class:`ExperimentBundle`'s per-event disagreement rows
by named regime tags derived from bar data.  Every tag is
deterministic given the warehouse contents and the requested
regime specs.

Supported regime modes:

* ``quantile`` — bucket the symbol's close series into empirical
  quantiles (default terciles).  Boundaries are computed from the
  full available history in the warehouse, so identical warehouse
  contents produce identical boundaries.
* ``ratio_quantile`` — same as ``quantile`` but the series is the
  ratio ``numerator_symbol / denominator_symbol`` (e.g., HYG/LQD).
* ``sma_trend`` — label days ``up`` when close > SMA(period),
  ``down`` otherwise.  Deterministic, no fitting.
* ``threshold`` — label days ``above`` / ``below`` a fixed cutoff.

Missing-data handling: if the requested symbol has no bar for an
event's date, the returned label is ``None``.  When computing
regime-conditioned metrics, ``None``-labeled rows are excluded
from every bucket AND surfaced as an ``unknown_count`` field.

Read-only.  No live trading path, no order construction, no
credential env reads, no feature-flag mutation.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
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


DEFAULT_WAREHOUSE_ROOT = Path("market_data")
DEFAULT_QUANTILES: Tuple[float, ...] = (1.0 / 3.0, 2.0 / 3.0)

MODE_QUANTILE = "quantile"
MODE_RATIO_QUANTILE = "ratio_quantile"
MODE_SMA_TREND = "sma_trend"
MODE_THRESHOLD = "threshold"
KNOWN_MODES = frozenset({
    MODE_QUANTILE, MODE_RATIO_QUANTILE, MODE_SMA_TREND, MODE_THRESHOLD,
})


class RegimeTaggerError(RuntimeError):
    """Base error for the regime tagger."""


# ---------------------------------------------------------------------------
# RegimeSpec — how to compute one named regime label
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeSpec:
    """One named regime derived from warehouse bar data.

    Prefer the factory classmethods (``quantile``, ``ratio_quantile``,
    ``sma_trend``, ``threshold``) over the raw constructor.
    """

    name: str
    mode: str
    dataset_id: str
    symbol: str = ""
    numerator_symbol: str = ""
    denominator_symbol: str = ""
    quantiles: Tuple[float, ...] = DEFAULT_QUANTILES
    labels: Tuple[str, ...] = ("low", "mid", "high")
    thresholds: Optional[Tuple[float, ...]] = None
    # NOTE: named 'cutoff' rather than 'threshold' to avoid shadowing
    # the RegimeSpec.threshold classmethod factory.
    cutoff: float = 0.0
    sma_period: int = 20

    def __post_init__(self) -> None:
        if not self.name:
            raise RegimeTaggerError("name must be non-empty")
        if self.mode not in KNOWN_MODES:
            raise RegimeTaggerError(
                f"unknown mode: {self.mode!r} (expected one of "
                f"{sorted(KNOWN_MODES)})"
            )
        if not self.dataset_id:
            raise RegimeTaggerError("dataset_id must be non-empty")
        if self.mode in {MODE_QUANTILE, MODE_SMA_TREND, MODE_THRESHOLD}:
            if not self.symbol:
                raise RegimeTaggerError(
                    f"mode={self.mode} requires 'symbol'"
                )
        if self.mode == MODE_RATIO_QUANTILE:
            if not (self.numerator_symbol and self.denominator_symbol):
                raise RegimeTaggerError(
                    "mode=ratio_quantile requires numerator_symbol "
                    "and denominator_symbol"
                )
        if self.mode in {MODE_QUANTILE, MODE_RATIO_QUANTILE}:
            if len(self.quantiles) + 1 != len(self.labels):
                raise RegimeTaggerError(
                    f"quantiles ({self.quantiles}) and labels "
                    f"({self.labels}) must be aligned "
                    f"(len(labels) = len(quantiles) + 1)"
                )
            for q in self.quantiles:
                if not 0.0 < q < 1.0:
                    raise RegimeTaggerError(
                        f"quantile {q} must be in (0, 1)"
                    )
            for a, b in zip(self.quantiles, self.quantiles[1:]):
                if a >= b:
                    raise RegimeTaggerError(
                        f"quantiles must be strictly ascending: "
                        f"{self.quantiles}"
                    )
        if self.mode == MODE_SMA_TREND and self.sma_period < 2:
            raise RegimeTaggerError("sma_period must be >= 2")

    # ------------------------------------------------------------------
    # Factory classmethods
    # ------------------------------------------------------------------

    @classmethod
    def quantile(
        cls,
        name: str,
        *,
        dataset_id: str,
        symbol: str,
        quantiles: Sequence[float] = DEFAULT_QUANTILES,
        labels: Sequence[str] = ("low", "mid", "high"),
        thresholds: Optional[Sequence[float]] = None,
    ) -> "RegimeSpec":
        return cls(
            name=name, mode=MODE_QUANTILE, dataset_id=dataset_id,
            symbol=symbol,
            quantiles=tuple(quantiles), labels=tuple(labels),
            thresholds=tuple(thresholds) if thresholds is not None else None,
        )

    @classmethod
    def ratio_quantile(
        cls,
        name: str,
        *,
        dataset_id: str,
        numerator_symbol: str,
        denominator_symbol: str,
        quantiles: Sequence[float] = DEFAULT_QUANTILES,
        labels: Sequence[str] = ("stress", "mid", "calm"),
        thresholds: Optional[Sequence[float]] = None,
    ) -> "RegimeSpec":
        return cls(
            name=name, mode=MODE_RATIO_QUANTILE, dataset_id=dataset_id,
            numerator_symbol=numerator_symbol,
            denominator_symbol=denominator_symbol,
            quantiles=tuple(quantiles), labels=tuple(labels),
            thresholds=tuple(thresholds) if thresholds is not None else None,
        )

    @classmethod
    def sma_trend(
        cls,
        name: str,
        *,
        dataset_id: str,
        symbol: str,
        sma_period: int = 20,
        labels: Sequence[str] = ("down", "up"),
    ) -> "RegimeSpec":
        if len(labels) != 2:
            raise RegimeTaggerError(
                "sma_trend labels must have exactly 2 entries"
            )
        return cls(
            name=name, mode=MODE_SMA_TREND, dataset_id=dataset_id,
            symbol=symbol,
            sma_period=int(sma_period),
            labels=tuple(labels),
        )

    @classmethod
    def threshold(
        cls,
        name: str,
        *,
        dataset_id: str,
        symbol: str,
        threshold: float,
        labels: Sequence[str] = ("below", "above"),
    ) -> "RegimeSpec":
        if len(labels) != 2:
            raise RegimeTaggerError(
                "threshold labels must have exactly 2 entries"
            )
        return cls(
            name=name, mode=MODE_THRESHOLD, dataset_id=dataset_id,
            symbol=symbol, cutoff=float(threshold),
            labels=tuple(labels),
        )

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "dataset_id": self.dataset_id,
            "symbol": self.symbol,
            "numerator_symbol": self.numerator_symbol,
            "denominator_symbol": self.denominator_symbol,
            "quantiles": list(self.quantiles),
            "labels": list(self.labels),
            "thresholds": list(self.thresholds) if self.thresholds is not None else None,
            "cutoff": self.cutoff,
            "sma_period": self.sma_period,
        }


# ---------------------------------------------------------------------------
# RegimeTag — one event's labels across all specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeTag:
    """Immutable labels for one event across every regime spec."""

    event_date: str  # YYYY-MM-DD
    labels: Mapping[str, Optional[str]]

    def get(self, regime_name: str) -> Optional[str]:
        return self.labels.get(regime_name)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_date": self.event_date,
            "labels": dict(self.labels),
        }


# ---------------------------------------------------------------------------
# RegimeMetrics — one regime bucket's stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeMetrics:
    """Per-bucket statistics for a regime slice."""

    regime_name: str
    label: str
    n_events_tagged: int  # events whose date got this label
    n_disagreements: int  # disagreement rows falling in this bucket
    disagreement_rate: float  # n_disagreements / total_rows_in_bucket
    total_rows_in_bucket: int  # (event, symbol) rows carried by tagged events
    mean_score_delta: float  # over disagreements with numeric score_delta
    positive_pct: float  # fraction of disagreements with Δ > 0
    negative_pct: float  # fraction with Δ < 0
    max_abs_score_delta: float
    selection_agreement_rate: float  # fraction of rows where champ_sel==chal_sel

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime_name": self.regime_name,
            "label": self.label,
            "n_events_tagged": self.n_events_tagged,
            "n_disagreements": self.n_disagreements,
            "disagreement_rate": self.disagreement_rate,
            "total_rows_in_bucket": self.total_rows_in_bucket,
            "mean_score_delta": self.mean_score_delta,
            "positive_pct": self.positive_pct,
            "negative_pct": self.negative_pct,
            "max_abs_score_delta": self.max_abs_score_delta,
            "selection_agreement_rate": self.selection_agreement_rate,
        }


# ---------------------------------------------------------------------------
# Warehouse bar loading
# ---------------------------------------------------------------------------


def _load_closes(
    dataset_id: str,
    symbol: str,
    warehouse_root: Path,
) -> Dict[str, float]:
    """Return dict[YYYY-MM-DD] = close for the given symbol.

    Reads every Parquet file under
    ``<root>/equities/daily/<dataset_id>/<symbol>/``.  Empty dict
    when the symbol is not present (missing-data handling defers
    to the caller).
    """
    try:
        import pyarrow.parquet as pq  # local import so tests can monkey-patch
    except ImportError as exc:
        raise RegimeTaggerError(
            "pyarrow is required to read warehouse Parquet files"
        ) from exc
    directory = (
        Path(warehouse_root) / "equities" / "daily"
        / dataset_id / symbol
    )
    result: Dict[str, float] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.parquet")):
        try:
            data = pq.read_table(str(path)).to_pydict()
        except Exception:
            continue
        timestamps = data.get("timestamp") or []
        closes = data.get("close") or []
        for ts, c in zip(timestamps, closes):
            try:
                result[str(ts)[:10]] = float(c)
            except (TypeError, ValueError):
                continue
    return result


# ---------------------------------------------------------------------------
# Series + boundary construction
# ---------------------------------------------------------------------------


def _series_for_spec(
    spec: RegimeSpec,
    warehouse_root: Path,
) -> Dict[str, float]:
    """Return dict[date -> value] for the spec's underlying indicator."""
    if spec.mode == MODE_RATIO_QUANTILE:
        num = _load_closes(spec.dataset_id, spec.numerator_symbol,
                           warehouse_root)
        den = _load_closes(spec.dataset_id, spec.denominator_symbol,
                           warehouse_root)
        return {
            d: num[d] / den[d]
            for d in num
            if d in den and den[d] != 0.0
        }
    return _load_closes(spec.dataset_id, spec.symbol, warehouse_root)


def _compute_quantile_boundaries(
    values: Sequence[float],
    quantiles: Sequence[float],
) -> List[float]:
    """Deterministic empirical quantile boundaries.

    Uses the nearest-rank definition on the sorted series so
    identical inputs always produce identical outputs (Python's
    ``statistics.quantiles`` performs linear interpolation which
    can produce tiny FP jitter across platforms; we prefer the
    stable sort-rank here).
    """
    if not values:
        return []
    ordered = sorted(values)
    n = len(ordered)
    boundaries: List[float] = []
    for q in quantiles:
        rank = min(n - 1, max(0, int(q * n)))
        boundaries.append(float(ordered[rank]))
    return boundaries


def _label_for(
    value: Optional[float],
    spec: RegimeSpec,
    boundaries: Sequence[float],
) -> Optional[str]:
    """Return the label for ``value`` under ``spec``.

    ``boundaries`` is the (mode-specific) precomputed boundary
    list.  For ``sma_trend`` the boundary list is
    ``[sma_at_that_date]`` and the caller sets it per-day.
    """
    if value is None:
        return None
    if spec.mode in {MODE_QUANTILE, MODE_RATIO_QUANTILE}:
        for i, boundary in enumerate(boundaries):
            if value < boundary:
                return spec.labels[i]
        return spec.labels[-1]
    if spec.mode == MODE_SMA_TREND:
        # boundaries is a single-element list [sma_value_at_date]
        if not boundaries:
            return None
        sma = boundaries[0]
        return spec.labels[1] if value > sma else spec.labels[0]
    if spec.mode == MODE_THRESHOLD:
        return spec.labels[1] if value > spec.cutoff else spec.labels[0]
    return None  # unreachable — mode validated at construction


# ---------------------------------------------------------------------------
# tag_events
# ---------------------------------------------------------------------------


def tag_events(
    event_timestamps: Sequence[str],
    specs: Sequence[RegimeSpec],
    warehouse_root: Path = DEFAULT_WAREHOUSE_ROOT,
) -> Dict[str, RegimeTag]:
    """Label each event timestamp under every regime spec.

    Returns dict mapping ``YYYY-MM-DD`` (extracted from the event
    timestamp) to a :class:`RegimeTag`.  When multiple event
    timestamps share the same date, the returned dict has one
    entry per unique date (labels are date-level, not event-level).

    Missing-data policy: a spec whose underlying symbol has no bar
    on a given date yields ``None`` for that (date, regime_name).
    """
    event_dates = sorted({str(ts)[:10] for ts in event_timestamps if ts})
    if not event_dates:
        return {}

    # Precompute per-spec context: series + quantile boundaries.
    spec_ctx: Dict[str, Dict[str, Any]] = {}
    for spec in specs:
        series = _series_for_spec(spec, warehouse_root)
        ctx: Dict[str, Any] = {"series": series}
        if spec.mode in {MODE_QUANTILE, MODE_RATIO_QUANTILE}:
            if spec.thresholds is not None:
                ctx["boundaries"] = list(spec.thresholds)
            else:
                ctx["boundaries"] = _compute_quantile_boundaries(
                    list(series.values()), spec.quantiles,
                )
        spec_ctx[spec.name] = ctx

    # Precompute SMA lookup for sma_trend specs (over the full series)
    for spec in specs:
        if spec.mode != MODE_SMA_TREND:
            continue
        series = spec_ctx[spec.name]["series"]
        dates = sorted(series.keys())
        sma_by_date: Dict[str, float] = {}
        for i in range(spec.sma_period - 1, len(dates)):
            window = dates[i - spec.sma_period + 1: i + 1]
            sma_by_date[dates[i]] = (
                sum(series[d] for d in window) / spec.sma_period
            )
        spec_ctx[spec.name]["sma_by_date"] = sma_by_date

    result: Dict[str, RegimeTag] = {}
    for date in event_dates:
        labels: Dict[str, Optional[str]] = {}
        for spec in specs:
            ctx = spec_ctx[spec.name]
            series = ctx["series"]
            value = series.get(date)
            if spec.mode == MODE_SMA_TREND:
                sma = ctx["sma_by_date"].get(date)
                if sma is None or value is None:
                    labels[spec.name] = None
                else:
                    labels[spec.name] = _label_for(value, spec, [sma])
            else:
                labels[spec.name] = _label_for(
                    value, spec, ctx.get("boundaries", []),
                )
        result[date] = RegimeTag(event_date=date, labels=labels)
    return result


# ---------------------------------------------------------------------------
# regime_conditioned_metrics
# ---------------------------------------------------------------------------


def regime_conditioned_metrics(
    bundle: Any,
    specs: Sequence[RegimeSpec],
    warehouse_root: Path = DEFAULT_WAREHOUSE_ROOT,
    *,
    exclude_champion_rejected: bool = True,
) -> Dict[str, Dict[str, RegimeMetrics]]:
    """Compute per-regime metrics for one experiment bundle.

    ``bundle`` may be an :class:`ExperimentBundle` OR any object
    whose ``.validation_bundle.comparison`` exposes
    ``score_tables`` and ``disagreements``.  Deterministic given
    the same warehouse contents.

    ``exclude_champion_rejected`` (default True) drops
    disagreements where the champion's explanation contains
    ``insufficient_history`` — those are RS-firing-on-rejected-base
    rows that inflate the disagreement count without representing
    real strategy disagreement (per the executive report finding).

    Return shape:
    ``{regime_name: {label: RegimeMetrics, ..., "unknown": RegimeMetrics}}``
    where ``unknown`` collects rows whose date could not be tagged
    (missing warehouse data).
    """
    comparison = bundle.validation_bundle.comparison
    tables = list(comparison.score_tables)
    disagreements = list(comparison.disagreements)

    # Tag every event
    event_timestamps = [t.event_timestamp for t in tables]
    tags = tag_events(event_timestamps, specs, warehouse_root)

    # Row-level bucketing for the total_rows_in_bucket denominator
    rows_by_regime: Dict[str, Dict[str, int]] = {
        spec.name: {} for spec in specs
    }
    events_by_regime: Dict[str, Dict[str, int]] = {
        spec.name: {} for spec in specs
    }
    row_agreements_by_regime: Dict[str, Dict[str, List[bool]]] = {
        spec.name: {} for spec in specs
    }
    for table in tables:
        date = table.event_timestamp[:10]
        tag = tags.get(date)
        for spec in specs:
            label = tag.get(spec.name) if tag else None
            key = label if label is not None else "unknown"
            events_by_regime[spec.name][key] = (
                events_by_regime[spec.name].get(key, 0) + 1
            )
            for row in table.rows:
                rows_by_regime[spec.name][key] = (
                    rows_by_regime[spec.name].get(key, 0) + 1
                )
                row_agreements_by_regime[spec.name].setdefault(key, []).append(
                    bool(row.champion_selected) == bool(row.challenger_selected)
                )

    # Disagreement bucketing
    disagreement_stats: Dict[str, Dict[str, Dict[str, Any]]] = {
        spec.name: {} for spec in specs
    }
    for d in disagreements:
        if exclude_champion_rejected:
            if "insufficient_history" in (d.champion_explanation or ""):
                # Still counts against total (for disagreement_rate)
                # via row_by_regime, but we skip its delta in the
                # score-delta aggregate.
                continue
        event_date = str(d.event_timestamp)[:10]
        tag = tags.get(event_date)
        for spec in specs:
            label = tag.get(spec.name) if tag else None
            key = label if label is not None else "unknown"
            bucket = disagreement_stats[spec.name].setdefault(
                key, {"n": 0, "deltas": [], "abs_max": 0.0}
            )
            bucket["n"] += 1
            sd = d.score_delta
            if isinstance(sd, (int, float)):
                bucket["deltas"].append(float(sd))
                if abs(float(sd)) > bucket["abs_max"]:
                    bucket["abs_max"] = abs(float(sd))

    result: Dict[str, Dict[str, RegimeMetrics]] = {}
    for spec in specs:
        per_label: Dict[str, RegimeMetrics] = {}
        # union of every seen label
        all_labels = set(rows_by_regime[spec.name]) | set(
            disagreement_stats[spec.name]
        )
        for label in sorted(all_labels):
            row_count = rows_by_regime[spec.name].get(label, 0)
            n_events = events_by_regime[spec.name].get(label, 0)
            bucket = disagreement_stats[spec.name].get(label, {
                "n": 0, "deltas": [], "abs_max": 0.0,
            })
            n_dis = bucket["n"]
            deltas: List[float] = bucket["deltas"]
            mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
            positive_pct = (
                sum(1 for x in deltas if x > 0) / len(deltas)
                if deltas else 0.0
            )
            negative_pct = (
                sum(1 for x in deltas if x < 0) / len(deltas)
                if deltas else 0.0
            )
            agreements = row_agreements_by_regime[spec.name].get(label, [])
            sel_agree = (
                sum(1 for a in agreements if a) / len(agreements)
                if agreements else 1.0
            )
            per_label[label] = RegimeMetrics(
                regime_name=spec.name,
                label=label,
                n_events_tagged=n_events,
                n_disagreements=n_dis,
                disagreement_rate=(n_dis / row_count) if row_count else 0.0,
                total_rows_in_bucket=row_count,
                mean_score_delta=mean_delta,
                positive_pct=positive_pct,
                negative_pct=negative_pct,
                max_abs_score_delta=bucket["abs_max"],
                selection_agreement_rate=sel_agree,
            )
        result[spec.name] = per_label
    return result


# ---------------------------------------------------------------------------
# Default regime specs — the three the user asked for
# ---------------------------------------------------------------------------


def default_regime_specs(
    regime_dataset_id: str = "regime-pack-2016",
) -> List[RegimeSpec]:
    """Ready-to-use regime pack: VXX terciles, HYG/LQD credit
    terciles, TLT SMA20 trend.
    """
    return [
        RegimeSpec.quantile(
            "vol",
            dataset_id=regime_dataset_id,
            symbol="VXX",
            labels=("low_vol", "mid_vol", "high_vol"),
        ),
        RegimeSpec.ratio_quantile(
            "credit",
            dataset_id=regime_dataset_id,
            numerator_symbol="HYG",
            denominator_symbol="LQD",
            labels=("credit_stress", "credit_mid", "credit_calm"),
        ),
        RegimeSpec.sma_trend(
            "bond_trend",
            dataset_id=regime_dataset_id,
            symbol="TLT",
            sma_period=20,
            labels=("tlt_down", "tlt_up"),
        ),
    ]


__all__ = [
    "DEFAULT_QUANTILES",
    "DEFAULT_WAREHOUSE_ROOT",
    "KNOWN_MODES",
    "MODE_QUANTILE",
    "MODE_RATIO_QUANTILE",
    "MODE_SMA_TREND",
    "MODE_THRESHOLD",
    "RegimeMetrics",
    "RegimeSpec",
    "RegimeTag",
    "RegimeTaggerError",
    "default_regime_specs",
    "regime_conditioned_metrics",
    "tag_events",
]
