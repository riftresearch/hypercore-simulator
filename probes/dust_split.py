"""One bounded, minute-aligned dust split; reconcile rounding before returning inventory."""

import argparse
import math
import time
from decimal import Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_send_asset_action

from .dust_lifecycle import HaltRecorder
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
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--a-dust", default="0.25")
    parser.add_argument("--d-dust", default="0.75")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    portions = {"a": Decimal(args.a_dust), "d": Decimal(args.d_dust)}
    if (
        not args.execute
        or any(not x.is_finite() or not 0 < x < 1 for x in portions.values())
        or sum(portions.values()) != 1
    ):
        parser.error(
            "Explicit --execute and positive sub-lot A/D portions summing to one are required"
        )
    actors = {
        name: wallet(key)
        for name, key in (
            ("a", "HYPERLIQUID_PRIVATE_KEY"),
            ("b", "HYPERLIQUID_B_PRIVATE_KEY"),
            ("d", "HYPERLIQUID_D_PRIVATE_KEY"),
        )
    }
    recorder = HaltRecorder(args.root, TESTNET)
    result = {
        "status": "aborted",
        "automatic_retry": False,
        "automatic_cleanup_on_failure": False,
    }
    try:
        meta = load_meta(recorder)
        purr = next(
            t for t in meta["tokens"] if t["name"] == "PURR" and t["isCanonical"]
        )
        usdc = next(
            t for t in meta["tokens"] if t["name"] == "USDC" and t["isCanonical"]
        )
        assert purr["szDecimals"] == 0
        assert all(
            q.normalize().as_tuple().exponent >= -purr["weiDecimals"]
            for q in portions.values()
        )
        initial = {
            name: snapshot(recorder, "initial-" + name, actor.address)
            for name, actor in actors.items()
        }
        assert all(state["role"] == {"role": "user"} for state in initial.values())
        inventory = free_balance(initial["a"]["balances"], purr["index"])
        assert 3 <= inventory <= 20
        assert all(
            free_balance(initial[name]["balances"], purr["index"]) == 0
            for name in ("b", "d")
        )
        result["initial_inventory"] = wire(inventory)
        result["portions"] = {name: wire(qty) for name, qty in portions.items()}

        def send(case, source, destination, token, quantity):
            nonce = reserve_nonce(
                args.nonce_file, actors[source].address, TESTNET, case
            )
            action = {
                "type": "sendAsset",
                "destination": actors[destination].address.lower(),
                "sourceDex": "spot",
                "destinationDex": "spot",
                "token": token["name"] + ":" + token["tokenId"],
                "amount": wire(quantity),
                "fromSubAccount": "",
                "nonce": nonce,
            }
            signature = sign_send_asset_action(actors[source], action, False)
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None,
            }
            response = submit(
                recorder,
                case,
                envelope,
                {
                    "signer": actors[source].address,
                    "intended_mutation": "bounded owned dust experiment",
                    "principal": wire(quantity),
                },
                True,
            )
            assert response.json == {"status": "ok", "response": {"type": "default"}}

        contexts = info(
            recorder, "source-contexts", {"type": "spotMetaAndAssetCtxs"}
        ).json
        assert isinstance(contexts, list)
        book = info(
            recorder, "source-book", {"type": "l2Book", "coin": "PURR/USDC"}
        ).json
        assert all(book["levels"]) and Decimal(book["levels"][0][0]["sz"]) >= 1
        price = Decimal(book["levels"][0][0]["px"])
        assert 0 < price <= 20
        phase = time.time() % 60
        delay = 0 if 2 <= phase <= 7 else (2 - phase) % 60
        recorder.event(
            {
                "state": "minute_alignment",
                "delay_seconds": delay,
                "reason": "Complete both dependent transfers before the next minute boundary",
            }
        )
        time.sleep(delay)
        send("setup-a-to-b", "a", "b", purr, inventory - portions["a"])
        send("setup-b-to-d", "b", "d", purr, portions["d"])
        started = time.monotonic()
        after = None
        for index in range(25):
            observed = {
                name: info(
                    recorder,
                    f"poll-{index:02d}-{name}",
                    {"type": "spotClearinghouseState", "user": actor.address},
                ).json
                for name, actor in actors.items()
            }
            if all(
                free_balance(observed[name], purr["index"]) == 0 for name in portions
            ):
                after = observed
                break
            if time.monotonic() - started > 100:
                break
            time.sleep(max(0, started + 5 * (index + 1) - time.monotonic()))
        assert after is not None, (
            "Both automatic conversions were not observed; inventory retained, no blind cleanup"
        )
        final = {
            name: snapshot(recorder, "converted-" + name, actor.address)
            for name, actor in actors.items()
        }
        new = {}
        for name, portion in portions.items():
            new[name] = [
                fill
                for fill in final[name]["fills"]
                if fill not in initial[name]["fills"]
            ]
            assert len(new[name]) == 1 and new[name][0]["dir"] == "Spot Dust Conversion"
            assert Decimal(new[name][0]["sz"]) == portion
        assert new["a"][0]["oid"] == new["d"][0]["oid"]
        credits = {
            name: free_balance(final[name]["balances"], usdc["index"])
            - free_balance(initial[name]["balances"], usdc["index"])
            for name in portions
        }
        unit = Decimal(10) ** usdc["weiDecimals"]
        rate = Decimal(initial["a"]["fees"]["feeSchedule"]["spotCross"])
        net = price - Decimal(math.floor(float(price * unit) * float(rate))) / unit
        expected = {
            name: (net * qty).quantize(1 / unit, rounding="ROUND_DOWN")
            for name, qty in portions.items()
        }
        largest = min(
            portions, key=lambda name: (-portions[name], actors[name].address.lower())
        )
        expected[largest] += net - sum(expected.values())
        result.update(
            credits={name: wire(x) for name, x in credits.items()},
            expected_largest_holder_residual={
                name: wire(x) for name, x in expected.items()
            },
            fee_net=wire(net),
            allocation_matches=credits == expected,
            source_book_time=book["time"],
            fills=new,
            external_context="Independent pre-setup book; other network dust or liquidity changes remain observable confounders",
        )
        remaining = free_balance(final["b"]["balances"], purr["index"])
        assert remaining == inventory - 1
        assert 0 <= credits["d"] <= price
        send("return-b-purr", "b", "a", purr, remaining)
        if credits["d"]:
            send("return-d-credit", "d", "a", usdc, credits["d"])
        for name, actor in actors.items():
            snapshot(recorder, "completed-" + name, actor.address)
        result["status"] = "completed"
        recorder.event(
            {
                "state": "scenario_completed",
                "scenario": "dust-split",
                "allocation_matches": result["allocation_matches"],
            }
        )
    except (Exception, KeyboardInterrupt) as exc:
        result["reason"] = str(exc)
        recorder.event({"state": "run_aborted", "reason": str(exc)})
        raise
    finally:
        create(recorder.root / "results.json", encode(result))


if __name__ == "__main__":
    main()
