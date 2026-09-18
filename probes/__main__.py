"""Run with uv run python -m probes."""

import argparse
import re
from pathlib import Path

from . import Recorder
from .matrix import FRESH, run_matrix
from .recorder import TESTNET


def address(value):
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise argparse.ArgumentTypeError("Expected a 20-byte 0x address")
    return value.lower()


def main():
    parser = argparse.ArgumentParser(
        description="Single-attempt testnet/loopback evidence probes"
    )
    parser.add_argument(
        "--root", required=True, type=Path, help="New or empty run directory"
    )
    parser.add_argument("--base-url", default=TESTNET)
    sub = parser.add_subparsers(dest="command", required=True)
    matrix = sub.add_parser("matrix", help="Read-only malformed/valid info matrix")
    matrix.add_argument("--user", required=True, type=address)
    matrix.add_argument("--fresh-user", type=address, default=FRESH)
    for command in ("send", "ioc"):
        signed = sub.add_parser(
            command, help="Plan signed cases; --execute permits mutation"
        )
        signed.add_argument("--execute", action="store_true")
        signed.add_argument(
            "--cases",
            default="fresh,existing"
            if command == "send"
            else "full,no-fill,minimum,size-precision,price-precision",
            help="Comma-separated subset; see docs/probe-harness.md",
        )
        signed.add_argument("--key-env", default="HYPERLIQUID_PRIVATE_KEY")
        signed.add_argument(
            "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
        )
        signed.add_argument(
            "--max-spend",
            default="20" if command == "send" else "60",
            help="Cumulative principal plus explicit fee reservation",
        )
        if command == "send":
            signed.add_argument(
                "--recipient", type=address, help="Defaults to HYPERLIQUID_B_ADDRESS"
            )
            signed.add_argument("--wrong-key-env", default="HYPERLIQUID_B_PRIVATE_KEY")
            signed.add_argument("--initial-amount", default="2")
            signed.add_argument("--amount", default="0.1")
            signed.add_argument(
                "--max-fee",
                default="1",
                help="Maximum observed preTransferCheck fee per send",
            )
        else:
            signed.add_argument(
                "--coin",
                help="Exact spot universe name; default first USDC-quoted market",
            )
            signed.add_argument("--side", choices=("buy", "sell"), default="buy")
            signed.add_argument("--notional", default="12")
    args = parser.parse_args()
    recorder = Recorder(args.root, args.base_url)
    try:
        if args.command == "matrix":
            run_matrix(recorder, args.user, args.fresh_user)
        else:
            from .scenarios import run_ioc, run_send

            (run_send if args.command == "send" else run_ioc)(recorder, args)
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {
                "state": "run_aborted",
                "command": args.command,
                "reason": str(exc) or "interrupted",
                "automatic_retry": False,
            }
        )
        raise
    print(f"Evidence: {recorder.root}")


if __name__ == "__main__":
    main()
