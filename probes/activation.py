"""Controlled non-USDC transfer to a fresh recipient with no activation USDC.

Requires A to own >=1 PURR, B to be existing with <=5 USDC, and a fresh C.
Moves B's USDC back to A, sends 1 PURR A->B, attempts B->C, then explicitly
replays that failed/successful envelope to measure nonce consumption. Testnet only.
"""

import argparse
import os
from decimal import Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_send_asset_action

from . import Recorder
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
    parser.add_argument("--recipient", default=os.environ.get("HYPERLIQUID_C_ADDRESS"))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    if not args.execute:
        raise ValueError(
            "This stateful scenario requires explicit --execute; read its setup contract first"
        )
    a, b = wallet("HYPERLIQUID_PRIVATE_KEY"), wallet("HYPERLIQUID_B_PRIVATE_KEY")
    c = args.recipient
    if not c or len(c) != 42 or c.lower() in (a.address.lower(), b.address.lower()):
        raise ValueError("A distinct controlled fresh recipient C is required")
    recorder = Recorder(args.root, TESTNET)
    try:
        meta = load_meta(recorder)
        usdc = next(token for token in meta["tokens"] if token["name"] == "USDC")
        purr = next(token for token in meta["tokens"] if token["name"] == "PURR")
        before_a = snapshot(recorder, "before-a", a.address)
        before_b = snapshot(recorder, "before-b", b.address)
        before_c = snapshot(recorder, "before-c", c)
        if before_c["role"] != {"role": "missing"}:
            raise RuntimeError(
                "C must be fresh; generate another controlled wallet for a repeat"
            )
        if before_b["role"] != {"role": "user"}:
            raise RuntimeError("B must be an existing user")
        if free_balance(before_a["balances"], purr["index"]) < 1:
            raise RuntimeError("A must already own at least 1 PURR")
        amount = free_balance(before_b["balances"], usdc["index"])
        if amount > 5:
            raise RuntimeError("Refusing to drain more than 5 test USDC from B")

        def send(case, signer, destination, token, quantity):
            nonce = reserve_nonce(
                args.nonce_file, signer.address, recorder.base_url, case
            )
            action = {
                "type": "sendAsset",
                "destination": destination.lower(),
                "sourceDex": "spot",
                "destinationDex": "spot",
                "token": token["name"] + ":" + token["tokenId"],
                "amount": wire(quantity),
                "fromSubAccount": "",
                "nonce": nonce,
            }
            signature = sign_send_asset_action(signer, action, False)
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": None,
            }
            result = submit(
                recorder,
                case,
                envelope,
                {
                    "signer": signer.address.lower(),
                    "intended_mutation": f"transfer {wire(quantity)} {token['name']} to controlled account",
                },
                True,
            )
            return envelope, result

        if amount:
            _, drained = send("return-b-usdc", b, a.address, usdc, amount)
            if drained.json != {"status": "ok", "response": {"type": "default"}}:
                raise RuntimeError("USDC drain failed; no further mutation")
        drained_b = snapshot(recorder, "drained-b", b.address)
        if free_balance(drained_b["balances"], usdc["index"]) != 0:
            raise RuntimeError("B must have zero USDC for the activation probe")
        _, credited = send("fund-b-purr", a, b.address, purr, Decimal(1))
        if credited.json != {"status": "ok", "response": {"type": "default"}}:
            raise RuntimeError("PURR setup failed; no further mutation")
        snapshot(recorder, "funded-b", b.address)
        envelope, _ = send("no-activation-usdc", b, c, purr, Decimal(1))
        submit(
            recorder,
            "replay-activation-attempt",
            envelope,
            {
                "intended_mutation": "explicit replay to measure failed-action nonce consumption"
            },
            True,
        )
        snapshot(recorder, "after-a", a.address)
        after_b = snapshot(recorder, "after-b", b.address)
        snapshot(recorder, "after-c", c)
        if free_balance(after_b["balances"], purr["index"]) >= 1:
            send("return-probe-purr", b, a.address, purr, Decimal(1))
            snapshot(recorder, "returned-a", a.address)
            snapshot(recorder, "returned-b", b.address)
        recorder.event(
            {"state": "scenario_completed", "scenario": "activation-refusal"}
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
