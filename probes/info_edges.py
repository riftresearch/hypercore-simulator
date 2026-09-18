"""Read-only sequence, timestamp, market-alias and transfer valuation context probes."""

import argparse
import os
from pathlib import Path

from . import Recorder
from .matrix import info
from .recorder import TESTNET, encode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    user = "0x111122223333444455556666777788889999aaaa"
    recorder = Recorder(args.root, TESTNET)
    # Runs safely beside the low-weight nonce campaign; at most15 queries/minute.
    recorder.minimum_interval = 4
    try:
        for name, body in (
            ("array-meta", ["spotMeta"]),
            ("array-role-short", ["userRole"]),
            ("array-role-extra", ["userRole", user, 1]),
            ("array-state", ["spotClearinghouseState", user]),
            ("array-pretransfer", ["preTransferCheck", user]),
            ("array-fees", ["userFees", user]),
            ("array-oid", ["orderStatus", user, 0]),
            ("array-fills-default", ["userFills", user]),
            ("array-fills-false", ["userFills", user, False]),
            ("array-ledger-default", ["userNonFundingLedgerUpdates", user]),
            ("array-ledger-range", ["userNonFundingLedgerUpdates", user, 0, None]),
            ("array-book-default", ["l2Book", "PURR/USDC"]),
            ("array-book-optional", ["l2Book", "PURR/USDC", None, None]),
        ):
            info(
                recorder,
                name,
                encode(body),
                metadata={
                    "hypothesis": "Typed enum/struct sequence field ordering and defaults"
                },
            )
        for field in ("startTime", "endTime"):
            for label, value in (
                ("year-9999", 253402300799999),
                ("chrono-max", 8210266876799999),
                ("chrono-overflow", 8210266876800000),
                ("i64-max", (1 << 63) - 1),
                ("u64-max", (1 << 64) - 1),
            ):
                info(
                    recorder,
                    f"{field}-{label}",
                    {"type": "userNonFundingLedgerUpdates", "user": user, field: value},
                )
        for label, address in (
            ("uppercase-prefix", "0X" + user[2:]),
            ("uppercase-digits", "0x" + user[2:].upper()),
        ):
            info(recorder, "address-" + label, {"type": "userRole", "user": address})
        for label, coin in (
            ("canonical", "PURR/USDC"),
            ("at-zero", "@0"),
            ("symbol", "PURR"),
            ("lowercase", "purr/usdc"),
        ):
            info(recorder, "market-" + label, {"type": "l2Book", "coin": coin})
        # Context endpoint is diagnostic only, not added to the simulator surface.
        info(
            recorder,
            "spot-mark-context",
            {"type": "spotMetaAndAssetCtxs"},
            metadata={
                "purpose": "Discriminate mark-price versus last-trade transfer valuation"
            },
        )
        a = os.environ.get("HYPERLIQUID_ADDRESS")
        if a:
            info(
                recorder,
                "account-mode",
                {"type": "userAbstraction", "user": a},
                metadata={
                    "purpose": "Diagnose whether account mode explains missing dust; read-only, outside simulator surface"
                },
            )
        recorder.event({"state": "scenario_completed", "scenario": "info-boundaries"})
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
