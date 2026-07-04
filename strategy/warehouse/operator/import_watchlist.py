"""Populate the warehouse for the default research universe.

Launched by ``scripts/research-import-watchlist`` after sourcing
``.env.research``.  Uses the existing warehouse write path:
``AlpacaProvider`` (wrapping ``ResearchAccountClient``) →
``import_bars`` → ``validate_dataset`` → ``apply_validation_report``.

Never prints credentials.  Refuses to run if required env vars
are missing.  Never sources ``.env.production``.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from strategy.local_warehouse import (
    STATUS_VALIDATED,
    WarehouseLayout,
    read_manifest,
)
from strategy.market_data_provider import AssetClass, BarInterval
from strategy.warehouse.import_pipeline import (
    ImportPipelineError,
    ImportReport,
    PendingDownloadsQueue,
    import_bars,
)
from strategy.warehouse.validation import (
    ValidationReport,
    apply_validation_report,
    validate_dataset,
)


DEFAULT_SYMBOLS: Sequence[str] = ("AAPL", "MSFT", "NVDA", "SPY", "QQQ")
DEFAULT_START = "2020-01-01"
DEFAULT_INTERVAL = BarInterval.DAILY
DEFAULT_ASSET_CLASS = AssetClass.EQUITY
DEFAULT_DATASET_ID_TEMPLATE = "watchlist-{start}-{end}"

REQUIRED_ENV_VARS: Sequence[str] = (
    "RESEARCH_ALPACA_API_KEY",
    "RESEARCH_ALPACA_SECRET_KEY",
    "RESEARCH_ALPACA_ENDPOINT",
    "RESEARCH_ALPACA_DATA_ENDPOINT",
)


class ImportCommandError(RuntimeError):
    """Raised when the operator command cannot proceed."""


def _today_iso() -> str:
    return date.today().isoformat()


def _iter_missing(env: Dict[str, str]) -> List[str]:
    return [name for name in REQUIRED_ENV_VARS if not env.get(name)]


def _default_dataset_id(start: str, end: str) -> str:
    return DEFAULT_DATASET_ID_TEMPLATE.format(start=start, end=end)


def _redacted_env_dump(env: Dict[str, str]) -> Dict[str, str]:
    """Mask credential values before any logging path."""
    result: Dict[str, str] = {}
    for name in REQUIRED_ENV_VARS:
        value = env.get(name, "")
        if not value:
            result[name] = "<UNSET>"
        elif "KEY" in name or "SECRET" in name:
            result[name] = f"<set, len={len(value)}>"
        else:
            result[name] = value
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="research-import-watchlist",
        description="Populate the warehouse for the default research universe.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=list(DEFAULT_SYMBOLS),
        help="symbols to import (default: %(default)s)",
    )
    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help="ISO 8601 start date (default: %(default)s)",
    )
    parser.add_argument(
        "--end",
        default="",
        help="ISO 8601 end date (default: today)",
    )
    parser.add_argument(
        "--dataset-id",
        default="",
        help="dataset id (default: watchlist-<start>-<end>)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=50,
        help="symbols per provider call (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-validate",
        action="store_true",
        help="skip validate/apply steps after import",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="force overwrite of an existing validated manifest",
    )
    return parser


def _make_provider(env: Dict[str, str]) -> Any:
    """Build an ``AlpacaProvider`` from the research env namespace.

    Imported lazily so `--help` works without live credentials.
    """
    from strategy.providers.alpaca import AlpacaProvider
    from strategy.research_account import (
        ResearchAccountClient,
        ResearchAccountConfig,
    )

    config = ResearchAccountConfig.from_env(env)
    client = ResearchAccountClient(config=config, env=env)
    return AlpacaProvider(client=client)


def run(
    args: argparse.Namespace,
    env: Optional[Dict[str, str]] = None,
    provider: Any = None,
    layout: Optional[WarehouseLayout] = None,
    now_iso: Optional[str] = None,
    printer=print,
) -> ImportReport:
    """Execute the import command.

    ``provider`` and ``layout`` are dependency-injected so tests can
    exercise the flow without touching the network or the repo's
    ``market_data/`` tree.
    """
    env = env if env is not None else dict(os.environ)
    now = now_iso or datetime.now(timezone.utc).isoformat()

    # Refuse to load .env.production even by accident.
    forbidden_files: List[str] = []
    for env_var in ("HERMES_ENV_FILE", "DOTENV_FILE"):
        value = env.get(env_var, "")
        if value and Path(value).name == ".env.production":
            forbidden_files.append(value)
    if forbidden_files:
        raise ImportCommandError(
            f"refusing to run against forbidden env files: {forbidden_files}"
        )

    missing = _iter_missing(env)
    if missing:
        raise ImportCommandError(
            f"missing required env vars: {missing}"
        )

    symbols: List[str] = list(args.symbols)
    if not symbols:
        raise ImportCommandError("--symbols must not be empty")

    start = args.start
    end = args.end or _today_iso()
    if start > end:
        raise ImportCommandError(
            f"--start ({start}) must be <= --end ({end})"
        )

    dataset_id = args.dataset_id or _default_dataset_id(start, end)

    if layout is None:
        layout = WarehouseLayout.from_env(env)
    layout.create()

    if provider is None:
        provider = _make_provider(env)

    redacted = _redacted_env_dump(env)
    printer("=== research-import-watchlist ===")
    printer(f"generated_at: {now}")
    printer(f"warehouse root: {layout.root}")
    for name in REQUIRED_ENV_VARS:
        printer(f"  {name}={redacted[name]}")
    printer(f"symbols: {symbols}")
    printer(f"window: {start} .. {end}")
    printer(f"interval: {DEFAULT_INTERVAL.value}")
    printer(f"dataset_id: {dataset_id}")
    printer(f"chunk_size: {args.chunk_size}")

    queue_path = layout.root / "queue" / f"{dataset_id}.db"
    printer(f"queue: {queue_path}")

    printer("\n--- import ---")
    with PendingDownloadsQueue(queue_path) as queue:
        report = import_bars(
            layout=layout,
            provider=provider,
            dataset_id=dataset_id,
            symbols=symbols,
            start=start,
            end=end,
            asset_class=DEFAULT_ASSET_CLASS,
            interval=DEFAULT_INTERVAL,
            chunk_size=args.chunk_size,
            queue=queue,
            force=args.force,
        )
    printer(f"symbols_ok: {list(report.symbols_ok)}")
    printer(f"symbols_empty: {list(report.symbols_empty)}")
    printer(f"total_bars: {report.total_bars}")
    printer(f"files_written: {len(report.files)}")
    printer(f"manifest: {report.manifest_path}")

    # Checksum summary — first 12 chars of each file's sha256
    printer("\nchecksum summary:")
    for f in report.files[:5]:
        printer(f"  {f.relative_path} sha={f.sha256[:12]}... rows={f.row_count}")
    if len(report.files) > 5:
        printer(f"  ... and {len(report.files) - 5} more files")

    if not args.skip_validate:
        printer("\n--- validate ---")
        validation = validate_dataset(layout, dataset_id)
        printer(
            f"validation ok: {validation.ok} "
            f"(files_checked={validation.files_checked}, "
            f"rows_checked={validation.rows_checked}, "
            f"errors={validation.error_count}, "
            f"warnings={validation.warning_count})"
        )
        if validation.error_count:
            for f in list(validation.findings)[:10]:
                printer(f"  ERROR: {f.kind} — {f.message}")
        apply_validation_report(layout, validation)
        final_status = read_manifest(layout, dataset_id)[
            "validation_status"
        ]
        printer(f"validation_status: {final_status}")
    else:
        printer("\n--- validate skipped ---")

    printer("\n=== IMPORT COMPLETE ===")
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except ImportCommandError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    except ImportPipelineError as exc:
        print(f"FAIL: import pipeline: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
