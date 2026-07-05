"""Interval requirement declarations for lab strategies.

Strategies opt in to a supported-interval set by exposing a
``supported_intervals()`` tuple and (for intraday strategies) a
``requires_intraday_data()`` flag.  The portfolio simulator wiring
consults these before loading bars.

Read-only: no live-runner imports, no order-path references, no
credential env reads.
"""

from __future__ import annotations

from typing import Tuple

from strategy.market_data_provider import BarInterval


DAILY_ONLY: Tuple[BarInterval, ...] = (BarInterval.DAILY,)
INTRADAY_15M: Tuple[BarInterval, ...] = (BarInterval.MINUTE_15,)
INTRADAY_ANY: Tuple[BarInterval, ...] = (
    BarInterval.HOURLY,
    BarInterval.MINUTE_1,
    BarInterval.MINUTE_5,
    BarInterval.MINUTE_15,
    BarInterval.MINUTE_30,
)
DAILY_OR_INTRADAY: Tuple[BarInterval, ...] = (
    BarInterval.DAILY,
    BarInterval.HOURLY,
    BarInterval.MINUTE_15,
    BarInterval.MINUTE_30,
)


class IntervalMismatchError(RuntimeError):
    """Raised when a strategy is asked to run on unsupported bars."""


def assert_supported_interval(
    strategy_name: str,
    supported: Tuple[BarInterval, ...],
    requested: BarInterval,
) -> None:
    """Raise :class:`IntervalMismatchError` if ``requested`` is not
    in ``supported``.  Called by the runner before evaluating any
    bars, so the failure is loud and up-front.
    """
    if requested not in supported:
        supported_names = ", ".join(i.value for i in supported)
        raise IntervalMismatchError(
            f"strategy {strategy_name!r} requires bars at "
            f"[{supported_names}] but received {requested.value!r}"
        )


__all__ = [
    "DAILY_ONLY",
    "DAILY_OR_INTRADAY",
    "INTRADAY_15M",
    "INTRADAY_ANY",
    "IntervalMismatchError",
    "assert_supported_interval",
]
