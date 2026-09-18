"""Explicitly replay captured signed envelopes; never retry automatically.

Useful for failed-action nonce consumption and JSON-field-order experiments.
Execution is opt-in; each source envelope remains immutable in its original run.
"""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from . import Recorder
from .recorder import TESTNET
from .scenarios import snapshot, submit


def reverse_objects(value):
    if isinstance(value, dict):
        return {
            key: reverse_objects(item) for key, item in reversed(list(value.items()))
        }
    if isinstance(value, list):
        return [reverse_objects(item) for item in value]
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--base-url", default=TESTNET)
    parser.add_argument(
        "--user", required=True, help="Account whose before/after state to observe"
    )
    parser.add_argument("--source", type=Path, action="append", required=True)
    parser.add_argument("--reverse-object-keys", action="store_true")
    parser.add_argument("--pad-order-decimals", action="store_true")
    parser.add_argument("--uppercase-cloid", action="store_true")
    parser.add_argument("--omit-reduce-only", action="store_true")
    parser.add_argument("--omit-grouping", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--max-notional",
        default="20",
        help="Per-envelope upper bound in token units for sends; quote notional for orders",
    )
    args = parser.parse_args()
    recorder = Recorder(args.root, args.base_url)
    cap = Decimal(args.max_notional)
    if not cap.is_finite() or cap <= 0:
        raise ValueError("Positive finite max notional required")
    try:
        snapshot(recorder, "before", args.user)
        for index, source in enumerate(args.source):
            raw = source.read_bytes()
            envelope = json.loads(raw)
            action = envelope["action"]
            if action["type"] == "sendAsset":
                amount = Decimal(action["amount"])
                if not amount.is_finite() or not 0 < amount <= cap:
                    raise ValueError("Captured transfer exceeds explicit replay bound")
            elif action["type"] == "order":
                notional = Decimal(0)
                for order in action["orders"]:
                    if order["t"] != {"limit": {"tif": "Ioc"}}:
                        raise ValueError("Only captured IOC orders may be replayed")
                    size, price = Decimal(order["s"]), Decimal(order["p"])
                    if (
                        not size.is_finite()
                        or not price.is_finite()
                        or min(size, price) <= 0
                    ):
                        raise ValueError("Invalid replay size/price")
                    notional += size * price
                if notional > cap:
                    raise ValueError("Captured order exceeds explicit replay bound")
            else:
                raise ValueError("Only order/sendAsset replay is supported")
            if (
                args.pad_order_decimals
                or args.uppercase_cloid
                or args.omit_reduce_only
                or args.omit_grouping
            ):
                if action["type"] != "order":
                    raise ValueError(
                        "Wire-representation transformations require an order"
                    )
                for order in action["orders"]:
                    if args.pad_order_decimals:
                        order["p"] = format(Decimal(order["p"]), ".9f")
                        order["s"] = format(Decimal(order["s"]), ".9f")
                    if args.uppercase_cloid and "c" in order:
                        order["c"] = "0x" + order["c"][2:].upper()
                    if args.omit_reduce_only:
                        if order.get("r") is not False:
                            raise ValueError("Only the false default may be omitted")
                        del order["r"]
                if args.omit_grouping:
                    if action.get("grouping") != "na":
                        raise ValueError("Only the na default may be omitted")
                    del action["grouping"]
            if args.reverse_object_keys:
                envelope = reverse_objects(envelope)
            case = f"replay-{index:02d}"
            submit(
                recorder,
                case,
                envelope,
                {
                    "intended_mutation": "explicit replay; nonce rejection is measured, not assumed",
                    "source": str(source),
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "reversed_object_keys": args.reverse_object_keys,
                    "padded_order_decimals": args.pad_order_decimals,
                    "uppercase_cloid": args.uppercase_cloid,
                    "omitted_reduce_only": args.omit_reduce_only,
                    "omitted_grouping": args.omit_grouping,
                    "observed_user": args.user,
                },
                args.execute,
            )
            snapshot(recorder, f"after-{index:02d}", args.user)
        recorder.event({"state": "scenario_completed", "scenario": "explicit-replay"})
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
