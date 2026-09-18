"""Find thin USDC spot depth and attempt one bounded IOC partial fill.

No promise that live depth remains stable; one submission, no retries. Every
scanned book and the exact precondition are recorded. Abort if none fits budget.
"""

import argparse
import hashlib
from decimal import ROUND_UP, Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_l1_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET
from .scenarios import (
    free_balance,
    load_meta,
    reserve_nonce,
    snapshot,
    submit,
    wallet,
    wire,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--max-notional", default="25")
    parser.add_argument("--max-markets", type=int, default=30)
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    cap = Decimal(args.max_notional)
    if not cap.is_finite() or not 10 <= cap <= 25 or not 1 <= args.max_markets <= 30:
        raise ValueError("Maximum25 test USDC and30 read-only book scans")
    recorder = Recorder(args.root, TESTNET)
    signer = wallet("HYPERLIQUID_PRIVATE_KEY")
    try:
        meta = load_meta(recorder)
        tokens = {token["index"]: token for token in meta["tokens"]}
        usdc = next(token for token in tokens.values() if token["name"] == "USDC")
        before = snapshot(recorder, "before", signer.address)
        if free_balance(before["balances"], usdc["index"]) < cap * Decimal("1.01"):
            raise RuntimeError(
                "Insufficient source balance for bounded partial-fill attempt"
            )
        markets = [
            market
            for market in sorted(meta["universe"], key=lambda market: market["index"])
            if market["tokens"][1] == usdc["index"]
        ][: args.max_markets]
        for market in markets:
            case = "book-" + str(market["index"])
            result = info(recorder, case, {"type": "l2Book", "coin": market["name"]})
            if result.transport_error or result.status != 200:
                raise RuntimeError("Book scan failed; no retry")
            if (
                not isinstance(result.json, dict)
                or not result.json.get("levels", [[], []])[1]
            ):
                continue
            best = result.json["levels"][1][0]
            price, available = Decimal(best["px"]), Decimal(best["sz"])
            step = Decimal(1).scaleb(-tokens[market["tokens"][0]]["szDecimals"])
            size = max(
                available + step,
                (Decimal(10) / price).quantize(step, rounding=ROUND_UP),
            )
            if available <= 0 or price * size > cap:
                continue
            nonce = reserve_nonce(
                args.nonce_file, signer.address, recorder.base_url, "partial-attempt"
            )
            cloid = (
                "0x"
                + hashlib.sha256(
                    f"partial:{nonce}:{signer.address}".encode()
                ).hexdigest()[:32]
            )
            order = {
                "a": 10000 + market["index"],
                "b": True,
                "p": wire(price),
                "s": wire(size),
                "r": False,
                "t": {"limit": {"tif": "Ioc"}},
                "c": cloid,
            }
            action = {"type": "order", "orders": [order], "grouping": "na"}
            signature = sign_l1_action(signer, action, None, nonce, None, False)
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": None,
            }
            result = submit(
                recorder,
                "partial-attempt",
                envelope,
                {
                    "intended_mutation": "one IOC buy bounded to25 test USDC",
                    "source_book": case,
                    "visible_at_limit": wire(available),
                    "requested_size": wire(size),
                    "limit_notional": wire(price * size),
                    "assumption": "live depth may move before matching",
                },
                args.execute,
            )
            if result is not None:
                info(
                    recorder,
                    "partial-status",
                    {"type": "orderStatus", "user": signer.address, "oid": cloid},
                )
                snapshot(recorder, "after", signer.address)
            recorder.event(
                {
                    "state": "scenario_completed",
                    "scenario": "partial-attempt",
                    "submitted": args.execute,
                }
            )
            return
        recorder.event(
            {
                "state": "precondition_unavailable",
                "reason": "No thin book within explicit scan/notional bounds; no action submitted",
            }
        )
        print("No suitable thin book found within bounds; no action submitted.")
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
