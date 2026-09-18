"""Signed IOC rejections and equivalent signature forms; no crossing orders."""

import argparse
from copy import deepcopy
from pathlib import Path

from hyperliquid.utils.signing import sign_l1_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET
from .scenarios import reserve_nonce, snapshot, submit, wallet

SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error("--execute required")
    signer = wallet("HYPERLIQUID_C_PRIVATE_KEY")
    recorder = Recorder(args.root, TESTNET)
    try:
        before = snapshot(recorder, "before", signer.address)
        if before["role"] != {"role": "user"}:
            raise RuntimeError("C must already be activated")
        # C has only2USDC and noPURR. Every possible accepted action is further
        # bounded by its available quote balance, with no resting order type.
        amounts = {row["coin"]: row["total"] for row in before["balances"]["balances"]}
        from decimal import Decimal

        if (
            Decimal(amounts.get("USDC", "0")) > 2
            or Decimal(amounts.get("PURR", "0")) != 0
        ):
            raise RuntimeError("Requires controlled C<=2USDC and exactlyzeroPURR")
        base = {
            "a": 10000,
            "b": True,
            "p": "5",
            "s": "4",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
        }
        variants = (
            ("insufficient-quote", {}),
            ("insufficient-base", {"b": False}),
            ("zero-size", {"s": "0"}),
            ("zero-price", {"p": "0"}),
            ("too-many-significant", {"p": "5.00001"}),
            ("invalid-size", {"s": "0.1"}),
            ("reduce-only", {"r": True}),
            ("unknown-asset", {"a": 4_000_000_000}),
            ("minimum-before-balance", {"s": "1"}),
            ("price-before-size", {"p": "5.00001", "s": "0.1"}),
            ("zero-before-price", {"p": "5.00001", "s": "0"}),
        )
        for index, (case, changes) in enumerate(variants):
            nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case)
            order = {
                **deepcopy(base),
                **changes,
                "c": "0x" + f"{nonce << 32 | index:032x}",
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
                case,
                envelope,
                {
                    "signer": signer.address.lower(),
                    "intended_mutation": "bounded rejection probe with <=2USDC source",
                },
                True,
            )
            if result.status == 429:
                raise RuntimeError("Rate limited; halt without retry")
            info(
                recorder,
                case + "-status",
                {"type": "orderStatus", "user": signer.address, "oid": order["c"]},
            )
            if case == "invalid-size":
                for label, mutate in (
                    ("v-zero-one", "v"),
                    ("high-s", "s"),
                    ("unprefixed-r", "r"),
                ):
                    changed = deepcopy(envelope)
                    sig = changed["signature"]
                    if mutate == "v":
                        sig["v"] -= 27
                    elif mutate == "s":
                        sig["s"] = hex(SECP256K1_ORDER - int(sig["s"], 16))
                        sig["v"] = 55 - sig["v"]
                    else:
                        sig["r"] = sig["r"][2:]
                    submit(
                        recorder,
                        "signature-" + label,
                        changed,
                        {
                            "intended_mutation": "equivalent-signature explicit replay of invalid-size order"
                        },
                        True,
                    )
        for label, expiry_offset in (("expired", -60_000), ("future", 60_000)):
            nonce = reserve_nonce(
                args.nonce_file, signer.address, TESTNET, "expiry-" + label
            )
            expiry = nonce + expiry_offset
            action = {
                "type": "order",
                "orders": [{**base, "s": "0.1"}],
                "grouping": "na",
            }
            signature = sign_l1_action(signer, action, None, nonce, expiry, False)
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": expiry,
            }
            submit(
                recorder,
                "expiry-" + label,
                envelope,
                {"intended_mutation": "expiry admission of invalid-size order"},
                True,
            )
            submit(
                recorder,
                "expiry-" + label + "-replay",
                deepcopy(envelope),
                {"intended_mutation": "explicit replay measures nonce consumption"},
                True,
            )
        snapshot(recorder, "after", signer.address)
        recorder.event(
            {"state": "scenario_completed", "scenario": "signed-order-boundaries"}
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
