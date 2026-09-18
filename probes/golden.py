"""Replay captured IOC, nonce, activation, transfer, and sell sequences locally.

Compare exact HTTP/errors, complete balances, fills, order statuses and transfer
deltas. Only chain-generated IDs, hashes, and timestamps are normalized. No keys.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from . import Recorder
from .recorder import TESTNET, create, encode, run_root

DYNAMIC = {"oid", "tid", "hash", "time", "timestamp", "statusTimestamp"}


def normalize(value):
    if isinstance(value, dict):
        return {
            key: normalize(item) for key, item in value.items() if key not in DYNAMIC
        }
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Evidence directory; defaults to a fresh temporary directory")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--source", type=Path, default=Path("captures/testnet"))
    args = parser.parse_args()
    args.root = run_root(args.root, "golden")
    recorder = Recorder(args.root, args.base_url)
    if recorder.base_url == TESTNET:
        raise ValueError("Golden sequence replay is loopback-only")
    checks = 0
    sequence = 0

    def check(name, condition):
        nonlocal checks
        recorder.event({"state": "comparison", "name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)
        checks += 1

    def call(case, path, body):
        nonlocal sequence
        sequence += 1
        result = recorder.request(f"{sequence:03d}-{case}", path, body)
        check(case + " has no transport error", result.transport_error is None)
        return result

    def original(run, case, filename="response.body"):
        return (args.source / run / case / filename).read_bytes()

    def set_clock(run, case):
        metadata = json.loads(original(run, case, "metadata.json"))
        now = int(datetime.fromisoformat(metadata["started_at"]).timestamp() * 1000)
        check(
            "set captured clock",
            call(case + "-clock", "/_test/time", {"now_ms": now}).status == 200,
        )

    def compare_snapshot(run, case):
        observed = call(case, "/info", original(run, case, "request.body"))
        check(
            case + " matches capture",
            observed.status == 200
            and normalize(observed.json) == normalize(json.loads(original(run, case))),
        )

    def submit_captured(run, case):
        result = call(case, "/exchange", original(run, case, "request.body"))
        expected_metadata = json.loads(original(run, case, "metadata.json"))
        check(case + " HTTP status", result.status == expected_metadata["status"])
        if result.status == 200:
            check(
                case + " JSON result",
                normalize(result.json) == normalize(json.loads(original(run, case))),
            )
        else:
            check(case + " exact error bytes", result.body == original(run, case))

    def replay_order(run, prefix):
        set_clock(run, prefix)
        book = json.loads(original(run, prefix + "-book"))
        check(
            "set captured depth",
            call(
                prefix + "-book",
                "/_test/book",
                {
                    "coin": book["coin"],
                    "bids": book["levels"][0],
                    "asks": book["levels"][1],
                },
            ).status
            == 200,
        )
        submit_captured(run, prefix)
        for suffix in ("after-balances", "after-fills", "status-cloid"):
            compare_snapshot(run, prefix + "-" + suffix)

    def replay_send(run, case):
        set_clock(run, case)
        submit_captured(run, case)

    check("reset", call("reset", "/_test/reset", {}).status == 200)
    for run, case in (
        ("ioc-contract", "ioc-full-before-balances"),
        ("nonce-interior", "send-old-interior-before-recipient-balances"),
    ):
        state = json.loads(original(run, case))
        user = json.loads(original(run, case, "request.body"))["user"]
        for balance in state["balances"]:
            check(
                "seed balance",
                call(
                    "seed-" + str(balance["token"]),
                    "/_test/fund",
                    {
                        "address": user,
                        "token": str(balance["token"]),
                        "amount": balance["total"],
                        "mode": "transfer",
                    },
                ).status
                == 200,
            )
    for case in ("full", "no-fill", "minimum", "size-precision", "price-precision"):
        replay_order("ioc-contract", "ioc-" + case)
    for case in ("send-old-interior", "send-future-interior"):
        replay_send("nonce-interior", case)
        for side in ("sender", "recipient"):
            compare_snapshot("nonce-interior", case + "-after-" + side + "-balances")
    replay_send("activation-refusal", "return-b-usdc")
    compare_snapshot("activation-refusal", "drained-b-balances")
    replay_send("activation-refusal", "fund-b-purr")
    compare_snapshot("activation-refusal", "funded-b-balances")
    ledger_request = original("activation-refusal", "funded-b-ledger", "request.body")
    ledger = call("transfer-ledger", "/info", ledger_request)
    check(
        "non-USDC transfer delta matches",
        ledger.status == 200
        and ledger.json[-1]["delta"]
        == json.loads(original("activation-refusal", "funded-b-ledger"))[-1]["delta"],
    )
    replay_send("activation-refusal", "no-activation-usdc")
    replay_send("activation-refusal", "replay-activation-attempt")
    for side in ("a", "b", "c"):
        compare_snapshot("activation-refusal", "after-" + side + "-balances")
    compare_snapshot("activation-refusal", "after-c-role")
    replay_send("activation-refusal", "return-probe-purr")
    for side in ("a", "b"):
        compare_snapshot("activation-refusal", "returned-" + side + "-balances")
    replay_order("ioc-sell", "ioc-full")
    replay_send("omitted-order-defaults", "replay-00")
    report = {
        "checks_passed": checks,
        "requests_recorded": sequence,
        "source": str(args.source),
        "normalized_dynamic_fields": sorted(DYNAMIC),
        "compared": [
            "HTTP status",
            "exact non-JSON error bytes",
            "JSON exchange results",
            "complete balance rows",
            "fills and cost basis",
            "cloid order status",
            "transfer ledger delta",
            "activation refusal",
            "failed-action nonce replay",
            "omitted-default canonical signature and original admitted nonce replay",
        ],
    }
    create(recorder.root / "golden.json", encode(report))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
