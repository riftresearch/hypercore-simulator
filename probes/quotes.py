"""Find registered quote assets and test one non-USDC activation payment."""

import argparse
import os
from decimal import ROUND_UP, Decimal
from pathlib import Path

from eth_account import Account
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
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--quote-index", type=int)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--recipient-key-env", default="HYPERLIQUID_D_PRIVATE_KEY")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    recorder = Recorder(args.root, TESTNET)
    try:
        meta = load_meta(recorder)
        tokens = {t["index"]: t for t in meta["tokens"]}
        quotes = sorted({p["tokens"][1] for p in meta["universe"]} - {0})
        candidates = []
        for index in quotes:
            if args.quote_index is not None and index != args.quote_index:
                continue
            pair = next(
                (p for p in meta["universe"] if p["tokens"] == [index, 0]), None
            )
            if pair is None:
                continue
            result = info(
                recorder,
                f"quote-book-{index}",
                {"type": "l2Book", "coin": pair["name"]},
            )
            if result.transport_error or result.status != 200:
                raise RuntimeError("Required book request failed; no retry")
            asks = (result.json or {}).get("levels", [[], []])[1]
            if not asks:
                continue
            token = tokens[index]
            px, depth = Decimal(asks[0]["px"]), Decimal(asks[0]["sz"])
            if not Decimal("0.95") <= px <= Decimal("1.05"):
                continue
            quantity = (Decimal(12) / px).quantize(
                Decimal(1).scaleb(-token["szDecimals"]), rounding=ROUND_UP
            )
            if quantity > depth or quantity * px > 15:
                continue
            candidates.append(
                {"token": token, "pair": pair, "price": str(px), "size": str(quantity)}
            )
        create(recorder.root / "candidates.json", encode(candidates))
        if not args.execute:
            print(
                f"Registered non-USDC quote tokens={len(quotes)}, liquid bounded candidates={len(candidates)}",
                flush=True,
            )
            recorder.event(
                {
                    "state": "scenario_completed",
                    "scenario": "quote-scan",
                    "candidates": len(candidates),
                }
            )
            return
        if not candidates:
            raise RuntimeError(
                "No registered non-USDC quote asset purchasable within15USDC"
            )
        chosen = candidates[0]
        token, pair = chosen["token"], chosen["pair"]
        a, b = wallet("HYPERLIQUID_PRIVATE_KEY"), wallet("HYPERLIQUID_B_PRIVATE_KEY")
        if os.environ.get(args.recipient_key_env):
            d = wallet(args.recipient_key_env)
        else:
            # Persist the new controlled wallet before any incoming transfer, without
            # exposing its key in command arguments, output, or capture provenance.
            if (
                args.env_file.resolve() != Path(".env").resolve()
                or args.env_file.stat().st_mode & 0o077
            ):
                raise RuntimeError(
                    "Fresh key storage must be ignored local .env with mode0600"
                )
            existing = args.env_file.read_text()
            if any(
                line.startswith(args.recipient_key_env + "=")
                for line in existing.splitlines()
            ):
                raise RuntimeError(
                    "Recipient key already stored: reload with uv --env-file; never replace it"
                )
            d = Account.create()
            address_name = (
                args.recipient_key_env.removesuffix("PRIVATE_KEY") + "ADDRESS"
            )
            with args.env_file.open("a") as stream:
                stream.write(
                    f"\n{args.recipient_key_env}=0x{d.key.hex()}\n{address_name}={d.address}\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
        before_a = snapshot(recorder, "before-a", a.address)
        before_b = snapshot(recorder, "before-b", b.address)
        before_d = snapshot(recorder, "before-d", d.address)
        if before_d["role"] != {"role": "missing"}:
            raise RuntimeError(
                "Recipient must be fresh; use another controlled key variable"
            )
        if before_b["role"] != {"role": "user"}:
            raise RuntimeError("B must already exist")
        if free_balance(before_a["balances"], 0) < 15:
            raise RuntimeError("A must have at least15USDC")
        b_usdc = free_balance(before_b["balances"], 0)
        if b_usdc > 10:
            raise RuntimeError("Refusing to drain more than10USDC from B")

        def send(case, signer, destination, asset, quantity):
            nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case)
            action = {
                "type": "sendAsset",
                "destination": destination.lower(),
                "sourceDex": "spot",
                "destinationDex": "spot",
                "token": asset["name"] + ":" + asset["tokenId"],
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
                    "intended_mutation": f"bounded controlled transfer of {wire(quantity)} {asset['name']}",
                },
                True,
            )
            if result.status == 429:
                raise RuntimeError("Rate limited; no retry")
            return result

        if b_usdc:
            result = send("drain-b-usdc", b, a.address, tokens[0], b_usdc)
            if result.json != {"status": "ok", "response": {"type": "default"}}:
                raise RuntimeError("USDC drain failed")
        nonce = reserve_nonce(args.nonce_file, a.address, TESTNET, "buy-quote")
        action = {
            "type": "order",
            "orders": [
                {
                    "a": pair["index"] + 10000,
                    "b": True,
                    "p": chosen["price"],
                    "s": chosen["size"],
                    "r": False,
                    "t": {"limit": {"tif": "Ioc"}},
                }
            ],
            "grouping": "na",
        }
        signature = sign_l1_action(a, action, None, nonce, None, False)
        envelope = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        result = submit(
            recorder,
            "buy-quote",
            envelope,
            {
                "signer": a.address.lower(),
                "intended_mutation": "one IOC purchase; maximum15USDC",
                "candidate": chosen,
            },
            True,
        )
        after_buy = snapshot(recorder, "after-buy-a", a.address)
        if (
            result.status != 200
            or free_balance(after_buy["balances"], token["index"]) < 3
        ):
            raise RuntimeError("Need3 acquired quote tokens; no further mutation")
        result = send("fund-b-quote", a, b.address, token, Decimal(3))
        if result.json != {"status": "ok", "response": {"type": "default"}}:
            raise RuntimeError("Funding alternate quote failed")
        before_activation = snapshot(recorder, "funded-b", b.address)
        if free_balance(before_activation["balances"], 0) != 0:
            raise RuntimeError("B must have exactlyzeroUSDC")
        send("activate-with-alternate-quote", b, d.address, token, Decimal(1))
        after_b = snapshot(recorder, "after-b", b.address)
        after_d = snapshot(recorder, "after-d", d.address)
        for label, signer, state in (("b", b, after_b), ("d", d, after_d)):
            quantity = free_balance(state["balances"], token["index"])
            if quantity:
                if quantity > 3:
                    raise RuntimeError(
                        "Refusing to return more than3 probe quote tokens"
                    )
                send("return-quote-" + label, signer, a.address, token, quantity)
                snapshot(recorder, "returned-" + label, signer.address)
        snapshot(recorder, "returned-a", a.address)
        recorder.event(
            {
                "state": "scenario_completed",
                "scenario": "alternate-quote-activation",
                "candidate": chosen,
            }
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
