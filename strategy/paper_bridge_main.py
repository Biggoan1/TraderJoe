"""CLI entry point for ``scripts/traderjoe-paper-execute``.

Dry-run by default.  Refuses to place real paper orders unless
``--execute-paper-orders`` is explicitly present in argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from strategy.paper_bridge import (
    DEFAULT_LOG_ROOT,
    DEFAULT_MAX_ORDER_QUANTITY,
    DEFAULT_MAX_PER_ORDER_NOTIONAL,
    DEFAULT_MAX_TOTAL_NOTIONAL,
    DEFAULT_PAPER_ENV_FILE,
    PaperBridgeConfig,
    PaperBridgeError,
    PaperBridgeResult,
    PaperTradingBridge,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="traderjoe-paper-execute",
        description=(
            "Paper-trading bridge.  Reads an orders.json plan "
            "produced by traderjoe-simulate (or a research plan of "
            "the same shape) and previews the resulting paper "
            "orders.  Refuses production env.  Dry-run by default."
        ),
    )
    parser.add_argument(
        "--from-simulation",
        required=True,
        help="path to a simulator or research orders.json plan",
    )
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "explicit dry-run (this is also the default; the flag "
            "exists so scripted callers can be unambiguous)."
        ),
    )
    execution.add_argument(
        "--execute-paper-orders",
        action="store_true",
        help=(
            "ACTUALLY submit these orders to the paper Alpaca "
            "account.  Without this flag the bridge previews the "
            "plan and exits."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_PAPER_ENV_FILE,
        help=(
            "paper env file (default .env.paper).  The bridge "
            "refuses .env.production and requires the basename to "
            "start with '.env.paper'."
        ),
    )
    parser.add_argument(
        "--symbol-allowlist",
        nargs="+",
        default=None,
        help=(
            "restrict submission to these symbols.  Anything else "
            "is rejected before any broker call."
        ),
    )
    parser.add_argument(
        "--max-per-order-notional",
        type=float,
        default=DEFAULT_MAX_PER_ORDER_NOTIONAL,
    )
    parser.add_argument(
        "--max-total-notional",
        type=float,
        default=DEFAULT_MAX_TOTAL_NOTIONAL,
    )
    parser.add_argument(
        "--max-order-quantity",
        type=float,
        default=DEFAULT_MAX_ORDER_QUANTITY,
    )
    parser.add_argument(
        "--log-root",
        default=DEFAULT_LOG_ROOT,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the result as JSON",
    )
    return parser


def _print_summary(result: PaperBridgeResult, executed: bool) -> None:
    mode = "EXECUTE" if executed else "DRY-RUN"
    print(f"=== Paper Bridge — {mode} ===")
    print(f"Plan:        {result.plan_path}")
    print(f"Generated at: {result.generated_at}")
    print(f"Log:         {result.log_path or '(no log written)'}")
    print("")
    accepted = [o for o in result.orders if o.accepted]
    rejected = [o for o in result.orders if not o.accepted]
    print(f"Accepted: {len(accepted)}  |  Rejected: {len(rejected)}")
    print(f"Total accepted notional: ${result.total_accepted_notional:,.2f}")
    print("")
    if result.orders:
        print("Proposed orders:")
        print(
            f"  {'symbol':<8} {'side':<5} {'qty':>10} "
            f"{'ref_price':>12} {'notional':>14} {'status':<10} reason"
        )
        for order in result.orders:
            status = "OK" if order.accepted else "REJECTED"
            print(
                f"  {order.symbol:<8} {order.side:<5} "
                f"{order.quantity:>10.4f} {order.reference_price:>12.4f} "
                f"{order.reference_notional:>14.2f} {status:<10} "
                f"{order.rejection or order.reason}"
            )
    else:
        print("(plan contained no orders)")
    if executed:
        print("")
        print("Submitted:")
        for row in result.submitted:
            print(f"  {row}")
    else:
        print("")
        print(
            "This was a dry-run.  Re-run with "
            "--execute-paper-orders to submit these orders to the "
            "paper Alpaca account."
        )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        config = PaperBridgeConfig(
            execute=bool(args.execute_paper_orders),
            symbol_allowlist=tuple(args.symbol_allowlist or ()),
            max_per_order_notional=args.max_per_order_notional,
            max_total_notional=args.max_total_notional,
            max_order_quantity=args.max_order_quantity,
            env_file=args.env_file,
            log_root=args.log_root,
        )
        bridge = PaperTradingBridge(config)
        result = bridge.run(plan_path=args.from_simulation)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        else:
            _print_summary(result, executed=result.executed)
        return 0
    except PaperBridgeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
