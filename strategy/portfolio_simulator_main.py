"""CLI entry point for the portfolio simulator.

Invoked via ``scripts/traderjoe-simulate``.  Warehouse-first,
never touches a broker, refuses ``.env.production``.  Prints a
short human summary and writes the four artifacts under
``reports/portfolio_simulations/<run_id>/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from strategy.portfolio_simulator import (
    DEFAULT_REPORT_ROOT,
    PortfolioSimulationResult,
    PortfolioSimulatorConfig,
    PortfolioSimulatorError,
    write_simulation_report,
)
from strategy.portfolio_simulator_cli import (
    KNOWN_STRATEGIES,
    PRESET_WINDOWS,
    resolve_window,
    run_simulation,
)


def _refuse_production_env() -> None:
    """Guard: refuse to run if the process env looks like production.

    The simulator does no broker I/O so this is defense-in-depth,
    not a hard requirement.  Trader Joe convention: ``HERMES_CONTEXT
    == "production"`` or ``PAPER=False`` are treated as a hard
    stop even for research tools.
    """
    ctx = os.environ.get("HERMES_CONTEXT", "").strip().lower()
    if ctx == "production":
        raise PortfolioSimulatorError(
            "refusing to run simulator: HERMES_CONTEXT=production"
        )
    paper = os.environ.get("PAPER", "").strip().lower()
    if paper in ("false", "0", "no", "off"):
        raise PortfolioSimulatorError(
            "refusing to run simulator: PAPER is set to a false value"
        )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traderjoe-simulate",
        description=(
            "Portfolio simulator — replay a strategy over a historical "
            "window against a paper-sized account.  Warehouse-first; "
            "no broker calls; simulation only."
        ),
    )
    parser.add_argument(
        "--strategy",
        required=True,
        help=(
            "strategy key.  Known: "
            + ", ".join(KNOWN_STRATEGIES)
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="one or more equity symbols (e.g. AAPL MSFT NVDA)",
    )
    parser.add_argument(
        "--start",
        help="ISO date, inclusive.  Requires --end.",
    )
    parser.add_argument(
        "--end",
        help="ISO date, inclusive.  Requires --start.",
    )
    parser.add_argument(
        "--window",
        choices=sorted(PRESET_WINDOWS),
        help="preset lookback ending today (60d, 90d, 6mo, 1y)",
    )
    parser.add_argument(
        "--starting-cash",
        type=float,
        default=None,
        help="starting cash balance (default 100000)",
    )
    parser.add_argument(
        "--max-open-positions",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--max-dollars-per-trade",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--position-size-pct",
        type=float,
        default=None,
        help="target position size as a fraction of equity (0 < pct <= 1)",
    )
    parser.add_argument(
        "--commission-per-trade",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--slippage-bps",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--top-n-per-event",
        type=int,
        default=None,
    )
    parser.add_argument(
        "--score-entry-threshold",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--score-exit-threshold",
        type=float,
        default=None,
    )
    parser.add_argument(
        "--benchmarks",
        nargs="+",
        default=["SPY", "QQQ"],
        help="benchmark symbols for RS strategies (default: SPY QQQ)",
    )
    parser.add_argument(
        "--warehouse-root",
        default=None,
        help="override the warehouse root (default: WAREHOUSE_ROOT env)",
    )
    parser.add_argument(
        "--allow-provider-fallback",
        action="store_true",
        help=(
            "opt in to a provider fallback for uncovered symbols. "
            "Off by default; simulator is warehouse-first."
        ),
    )
    parser.add_argument(
        "--report-root",
        default=DEFAULT_REPORT_ROOT,
        help="report output root (default: reports/portfolio_simulations)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable summary as JSON",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="skip writing the report artifacts (test / dry-run only)",
    )
    parser.add_argument(
        "--asset-class",
        default="equity",
        help="asset class: equity|etf|crypto (default: equity)",
    )
    parser.add_argument(
        "--interval",
        default="1Day",
        help="bar interval: 1Day|1Hour|15Min|5Min|1Min (default: 1Day)",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> PortfolioSimulatorConfig:
    defaults = PortfolioSimulatorConfig()
    return PortfolioSimulatorConfig(
        starting_cash=(
            args.starting_cash
            if args.starting_cash is not None
            else defaults.starting_cash
        ),
        max_open_positions=(
            args.max_open_positions
            if args.max_open_positions is not None
            else defaults.max_open_positions
        ),
        max_dollars_per_trade=(
            args.max_dollars_per_trade
            if args.max_dollars_per_trade is not None
            else defaults.max_dollars_per_trade
        ),
        position_size_pct=(
            args.position_size_pct
            if args.position_size_pct is not None
            else defaults.position_size_pct
        ),
        commission_per_trade=(
            args.commission_per_trade
            if args.commission_per_trade is not None
            else defaults.commission_per_trade
        ),
        slippage_bps=(
            args.slippage_bps
            if args.slippage_bps is not None
            else defaults.slippage_bps
        ),
        top_n_per_event=(
            args.top_n_per_event
            if args.top_n_per_event is not None
            else defaults.top_n_per_event
        ),
        score_entry_threshold=(
            args.score_entry_threshold
            if args.score_entry_threshold is not None
            else defaults.score_entry_threshold
        ),
        score_exit_threshold=(
            args.score_exit_threshold
            if args.score_exit_threshold is not None
            else defaults.score_exit_threshold
        ),
    )


def _print_summary(
    result: PortfolioSimulationResult,
    paths: Optional[dict] = None,
    as_json: bool = False,
) -> None:
    if as_json:
        payload = result.to_dict()
        if paths:
            payload["artifacts"] = paths
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    m = result.metrics
    print(f"=== Portfolio Simulation — {result.run_id} ===")
    print(
        f"Strategy: {result.strategy_name}-v{result.strategy_version} "
        f"({result.strategy_id})"
    )
    print(f"Window:   {result.window_start} → {result.window_end}")
    print(f"Symbols:  {', '.join(result.symbols)}")
    print(f"Trading days: {m.trading_days}")
    print("")
    print(f"Starting cash:   ${m.starting_cash:,.2f}")
    print(f"Ending equity:   ${m.ending_equity:,.2f}")
    print(f"Net P/L:         ${m.net_pnl:+,.2f} ({m.total_return_pct:+.2f}%)")
    print(f"Realized P/L:    ${m.realized_pnl:+,.2f}")
    print(f"Unrealized P/L:  ${m.unrealized_pnl:+,.2f}")
    print(f"Max drawdown:    {m.max_drawdown_pct:.2f}%")
    print(f"Trades:          {m.trade_count} "
          f"(win {m.winning_trades} / loss {m.losing_trades})")
    win_rate = "—" if m.win_rate is None else f"{m.win_rate * 100:.2f}%"
    print(f"Win rate:        {win_rate}")
    avg_hold = (
        "—" if m.average_hold_days is None else f"{m.average_hold_days:.2f}"
    )
    print(f"Avg hold days:   {avg_hold}")

    def _fmt(v):
        return "—" if v is None else f"{v:.4f}"

    print(f"CAGR:            {_fmt(m.cagr)}")
    print(f"Sharpe:          {_fmt(m.sharpe)}")
    print(f"Sortino:         {_fmt(m.sortino)}")
    print(f"Calmar:          {_fmt(m.calmar)}")
    if paths:
        print("")
        print("Artifacts:")
        for key, value in sorted(paths.items()):
            print(f"  {key}: {value}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        _refuse_production_env()
        start, end = resolve_window(args.window, args.start, args.end)
        config = _config_from_args(args)
        result = run_simulation(
            strategy_key=args.strategy,
            symbols=args.symbols,
            start=start,
            end=end,
            config=config,
            warehouse_root=args.warehouse_root,
            allow_provider_fallback=args.allow_provider_fallback,
            benchmarks=args.benchmarks,
            asset_class=args.asset_class,
            interval=args.interval,
        )
        paths = None
        if not args.no_write:
            paths = write_simulation_report(result, root=args.report_root)
        _print_summary(result, paths=paths, as_json=args.json)
        return 0
    except PortfolioSimulatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
