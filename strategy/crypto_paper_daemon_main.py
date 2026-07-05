"""CLI entry point for ``scripts/traderjoe-crypto-paper-daemon``.

Dry-run by default; auto-execute requires
``PAPER_AUTO_EXECUTE_CRYPTO=true`` in ``.env.crypto`` and
``--execute-paper-orders`` on the command line.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from strategy.crypto_paper_daemon import (
    AUTO_EXECUTE_ENV_VAR,
    CryptoPaperDaemon,
    CryptoPaperDaemonConfig,
    CryptoPaperDaemonError,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_EMERGENCY_STOP_FILE,
    DEFAULT_LOG_ROOT,
    DEFAULT_MAX_PER_ORDER_NOTIONAL,
    DEFAULT_MAX_TOTAL_DAILY_NOTIONAL,
    DEFAULT_MAX_TRADES_PER_DAY,
    DEFAULT_PLAN_ROOT,
    DEFAULT_STATE_FILE,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traderjoe-crypto-paper-daemon",
        description=(
            "Crypto 24/7 paper daemon (one tick).  Plan-only by "
            f"default.  Auto-execute requires {AUTO_EXECUTE_ENV_VAR}"
            "=true AND --execute-paper-orders."
        ),
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        required=True,
        help="crypto symbols to consider (e.g. BTC/USD ETH/USD)",
    )
    parser.add_argument(
        "--strategy",
        default="momentum-v0.1.0",
        help="strategy key (default momentum-v0.1.0)",
    )
    parser.add_argument(
        "--execute-paper-orders",
        action="store_true",
    )
    parser.add_argument(
        "--env-file",
        default=".env.crypto",
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
        default=2,
    )
    parser.add_argument(
        "--json",
        action="store_true",
    )
    return parser


def _print_summary(result) -> None:
    payload = result.to_dict()
    print(f"=== Crypto Paper Daemon — tick {payload['tick_id']} ===")
    print(f"Generated at: {payload['generated_at']}")
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
                f"  {order['symbol']:<10} qty={order['quantity']:.6f} "
                f"ref={order['reference_price']:.4f} "
                f"notional=${order['reference_notional']:.2f}  "
                f"{order['reason']}"
            )
    if payload["rejected_orders"]:
        print("")
        print("Rejected orders:")
        for order in payload["rejected_orders"]:
            print(
                f"  {order['symbol']:<10} "
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
        config = CryptoPaperDaemonConfig(
            symbol_allowlist=tuple(args.symbols),
            strategy_key=args.strategy,
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
        )
        daemon = CryptoPaperDaemon(config)
        # The default production daemon requires a caller-supplied
        # bar_loader; when invoked from the CLI without one, exit
        # loudly rather than silently no-op.  The daemon still writes
        # a log entry recording the state.
        try:
            result = daemon.run_tick()
        except CryptoPaperDaemonError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            _print_summary(result)
        return 0
    except CryptoPaperDaemonError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
