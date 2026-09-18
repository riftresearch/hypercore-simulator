"""Bounded balance-limited IOC, batch atomicity and stable-fee discrimination."""

import argparse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_l1_action, sign_send_asset_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET, create, encode
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
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cases", required=True, help="balance,batch,fee")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cases = args.cases.split(",")
    if (
        not args.execute
        or not cases
        or len(cases) != len(set(cases))
        or any(c not in ("balance", "batch", "fee") for c in cases)
    ):
        parser.error("--execute and unique cases balance,batch,fee required")
    recorder = Recorder(args.root, TESTNET)
    a, b, c = (
        wallet(name)
        for name in (
            "HYPERLIQUID_PRIVATE_KEY",
            "HYPERLIQUID_B_PRIVATE_KEY",
            "HYPERLIQUID_C_PRIVATE_KEY",
        )
    )
    findings = []
    try:
        meta = load_meta(recorder)
        tokens = {t["index"]: t for t in meta["tokens"]}
        purr = next(t for t in tokens.values() if t["name"] == "PURR")
        purr_pair = next(
            p for p in meta["universe"] if p["tokens"] == [purr["index"], 0]
        )

        def send(case, signer, destination, token, amount):
            if not Decimal(0) < amount <= 15:
                raise RuntimeError("Transfer outside15-token per-send bound")
            nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case)
            action = {
                "type": "sendAsset",
                "destination": destination.lower(),
                "sourceDex": "spot",
                "destinationDex": "spot",
                "token": token["name"] + ":" + token["tokenId"],
                "amount": wire(amount),
                "fromSubAccount": "",
                "nonce": nonce,
            }
            signature = sign_send_asset_action(signer, action, False)
            result = submit(
                recorder,
                case,
                {
                    "action": action,
                    "nonce": nonce,
                    "signature": signature,
                    "vaultAddress": None,
                    "expiresAfter": None,
                },
                {
                    "intended_mutation": "bounded controlled-account transfer",
                    "signer": signer.address,
                },
                True,
            )
            if result.json != {"status": "ok", "response": {"type": "default"}}:
                raise RuntimeError("Setup/return transfer rejected; halted")

        def order(case, signer, orders, details=None):
            before = snapshot(recorder, case + "-before", signer.address)
            nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case)
            copied = deepcopy(orders)
            for index, item in enumerate(copied):
                if not Decimal(0) <= Decimal(item["p"]) * Decimal(item["s"]) <= 25:
                    raise RuntimeError("Order notional outside25USDC bound")
                item["c"] = "0x" + f"{nonce << 32 | index:032x}"
            action = {"type": "order", "orders": copied, "grouping": "na"}
            signature = sign_l1_action(signer, action, None, nonce, None, False)
            result = submit(
                recorder,
                case,
                {
                    "action": action,
                    "nonce": nonce,
                    "signature": signature,
                    "vaultAddress": None,
                    "expiresAfter": None,
                },
                {
                    "intended_mutation": "bounded IOC (source balance may intentionally be insufficient)",
                    "signer": signer.address,
                    **(details or {}),
                },
                True,
            )
            if result.status == 429:
                raise RuntimeError("Rate limited; halted without retry")
            after = snapshot(recorder, case + "-after", signer.address)
            for index, item in enumerate(copied):
                info(
                    recorder,
                    f"{case}-status-{index}",
                    {"type": "orderStatus", "user": signer.address, "oid": item["c"]},
                )
            findings.append({"case": case, "response": result.json})
            return before, after

        base = {
            "a": 10000 + purr_pair["index"],
            "b": True,
            "p": "5",
            "s": "3",
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
        }
        if "balance" in cases:
            initial_a = snapshot(recorder, "balance-initial-a", a.address)
            initial_b = snapshot(recorder, "balance-initial-b", b.address)
            if initial_b["role"] != {"role": "user"} or any(
                free_balance(initial_b["balances"], i) for i in (0, purr["index"])
            ):
                raise RuntimeError(
                    "B must exist with zeroUSDC/PURR; never drain unrelated balances"
                )
            if free_balance(initial_a["balances"], 0) < 50:
                raise RuntimeError("A needs50USDC; maximumnewfunding38USDC")
            book = info(
                recorder,
                "balance-source-book",
                {"type": "l2Book", "coin": purr_pair["name"]},
            ).json
            if (
                not book
                or not book["levels"][1]
                or Decimal(book["levels"][1][0]["px"]) * 3 >= 14
            ):
                raise RuntimeError(
                    "Need captured3PURR cost below14USDC to distinguish limit reservation"
                )
            for label, funding, size in (
                ("actual-cost-covered", "14", "3"),
                ("two-lots-affordable", "10", "3"),
                ("three-lots-affordable", "14", "5"),
            ):
                send(label + "-fund", a, b.address, tokens[0], Decimal(funding))
                _, state = order(
                    label,
                    b,
                    [{**base, "s": size}],
                    {"funding": funding, "source_book": "balance-source-book"},
                )
                for token in (purr, tokens[0]):
                    amount = free_balance(state["balances"], token["index"])
                    if amount:
                        send(
                            label + "-return-" + token["name"].lower(),
                            b,
                            a.address,
                            token,
                            amount,
                        )
                snapshot(recorder, label + "-returned", b.address)
            snapshot(recorder, "balance-final-a", a.address)
        if "batch" in cases:
            state = snapshot(recorder, "batch-initial-c", c.address)
            if (
                state["role"] != {"role": "user"}
                or free_balance(state["balances"], 0) > 2
                or free_balance(state["balances"], purr["index"])
            ):
                raise RuntimeError(
                    "C must have<=2USDC and noPURR; total financialexposure<=2USDC"
                )
            good = {**base, "p": "0.1", "s": "100"}
            bad_size = {**good, "s": "100.1"}
            bad_price = {**good, "p": "0.100001"}
            for label, orders in (
                ("no-match-insufficient-balance", [good]),
                ("sell-match-insufficient-balance", [{**base, "b": False, "p": "4"}]),
                ("batch-size-last", [good, bad_size]),
                ("batch-size-first", [bad_size, good]),
                ("batch-price-last", [good, bad_price]),
                ("batch-reduce-last", [good, {**good, "r": True}]),
            ):
                order(label, c, orders)
        if "fee" in cases:
            nq = next(t for t in tokens.values() if t["name"] == "NQ")
            pair = next(p for p in meta["universe"] if p["tokens"] == [nq["index"], 0])
            for label, size in (
                ("stable-fee-exact", "12.5"),
                ("stable-volume-fraction", "12.05"),
            ):
                book = info(
                    recorder, label + "-book", {"type": "l2Book", "coin": pair["name"]}
                ).json
                if not book or not book["levels"][1]:
                    raise RuntimeError("NQ has noask")
                px = Decimal(book["levels"][1][0]["px"])
                if not Decimal(10) <= px * Decimal(size) <= 15:
                    raise RuntimeError(
                        "NQ candidate outside10-15USDC bound; no purchase"
                    )
                state = snapshot(recorder, label + "-funding", a.address)
                if free_balance(state["balances"], 0) < 15:
                    raise RuntimeError("Insufficient bounded purchase funding")
                order(
                    label,
                    a,
                    [{**base, "a": 10000 + pair["index"], "p": wire(px), "s": size}],
                    {
                        "fee_hypotheses": "directbase,quotefirst,integerprecision",
                        "source_book": label + "-book",
                    },
                )
        create(recorder.root / "results.json", encode(findings))
        recorder.event(
            {"state": "scenario_completed", "scenario": "matching-discrimination"}
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
