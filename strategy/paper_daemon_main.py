"""CLI entry point for ``scripts/traderjoe-paper-daemon``.

Dry-run by default; the auto-execute path additionally requires
``PAPER_AUTO_EXECUTE_EQUITIES=true`` in the resolved paper env.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from strategy.paper_daemon import (
    AUTO_EXECUTE_ENV_VAR,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_EMERGENCY_STOP_FILE,
    DEFAULT_LOG_ROOT,
    DEFAULT_MAX_PER_ORDER_NOTIONAL,
    DEFAULT_MAX_TOTAL_DAILY_NOTIONAL,
    DEFAULT_MAX_TRADES_PER_DAY,
    DEFAULT_PLAN_ROOT,
    DEFAULT_STATE_FILE,
    EquityPaperDaemon,
    EquityPaperDaemonConfig,
    PaperDaemonError,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traderjoe-paper-daemon",
        description=(
            "Equity paper-trading daemon (15-minute intraday tick).  "
            "Plan-only by default.  Auto-execute requires "
            f"{AUTO_EXECUTE_ENV_VAR}=true AND --execute-paper-orders."
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="allowlisted equity symbols (nothing else is considered)",
    )
    parser.add_argument(
        "--strategy",
        required=True,
        help="strategy key from portfolio_simulator_cli.KNOWN_STRATEGIES",
    )
    parser.add_argument(
        "--interval",
        default="15Min",
        help="BarInterval value (default 15Min)",
    )
    parser.add_argument(
        "--execute-paper-orders",
        action="store_true",
        help=(
            "attempt to submit orders via the paper bridge.  Also "
            f"requires {AUTO_EXECUTE_ENV_VAR}=true in the paper env."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=".env.paper",
    )
    parser.add_argument(
        "--emergency-stop-file",
        default=DEFAULT_EMERGENCY_STOP_FILE,
    )
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_FILE,
    )
    parser.add_argument(
        "--log-root",
        default=DEFAULT_LOG_ROOT,
    )
    parser.add_argument(
        "--plan-root",
        default=DEFAULT_PLAN_ROOT,
    )
    parser.add_argument(
        "--max-per-order-notional",
        type=float,
        default=DEFAULT_MAX_PER_ORDER_NOTIONAL,
    )
    parser.add_argument(
        "--max-total-daily-notional",
        type=float,
        default=DEFAULT_MAX_TOTAL_DAILY_NOTIONAL,
    )
    parser.add_argument(
        "--max-trades-per-day",
        type=int,
        default=DEFAULT_MAX_TRADES_PER_DAY,
    )
    parser.add_argument(
        "--cooldown-minutes",
        type=int,
        default=DEFAULT_COOLDOWN_MINUTES,
    )
    parser.add_argument(
        "--top-n-per-tick",
        type=int,
        default=3,
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=20,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the tick result as JSON",
    )
    return parser


def _print_summary(result) -> None:
    payload = result.to_dict()
    print(f"=== Equity Paper Daemon — tick {payload['tick_id']} ===")
    print(f"Generated at: {payload['generated_at']}")
    print(f"Session:      {payload['session_status']['reason']}")
    print(f"Reason:       {payload['reason']}")
    print(f"Executed:     {payload['executed']}")
    print(
        f"Would have executed (if env flag were set): "
        f"{payload['would_have_executed']}"
    )
    if payload["proposed_orders"]:
        print("")
        print("Proposed orders (accepted):")
        for order in payload["proposed_orders"]:
            print(
                f"  {order['symbol']:<6} qty={order['quantity']} "
                f"ref={order['reference_price']:.4f} "
                f"notional=${order['reference_notional']:.2f}  "
                f"{order['reason']}"
            )
    if payload["rejected_orders"]:
        print("")
        print("Rejected orders:")
        for order in payload["rejected_orders"]:
            print(
                f"  {order['symbol']:<6} qty={order.get('quantity','-')}  "
                f"{order.get('rejection','?')}"
            )
    if payload["plan_path"]:
        print("")
        print(f"Plan: {payload['plan_path']}")
    if payload["log_path"]:
        print(f"Log:  {payload['log_path']}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        config = EquityPaperDaemonConfig(
            symbol_allowlist=tuple(args.symbols),
            strategy_key=args.strategy,
            interval=args.interval,
            execute=bool(args.execute_paper_orders),
            max_per_order_notional=args.max_per_order_notional,
            max_total_daily_notional=args.max_total_daily_notional,
            max_trades_per_day=args.max_trades_per_day,
            cooldown_minutes=args.cooldown_minutes,
            top_n_per_tick=args.top_n_per_tick,
            env_file=args.env_file,
            emergency_stop_file=args.emergency_stop_file,
            state_file=args.state_file,
            log_root=args.log_root,
            plan_root=args.plan_root,
            lookback_days=args.lookback_days,
        )
        daemon = EquityPaperDaemon(config)
        result = daemon.run_tick()
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            _print_summary(result)
        return 0
    except PaperDaemonError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
