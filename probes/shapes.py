"""Repeatable unsigned transport/schema probes; zero signature cannot authorize actions."""

import argparse
from copy import deepcopy
from pathlib import Path

from . import Recorder
from .matrix import report
from .recorder import TESTNET


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base-url", default=TESTNET)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    recorder = Recorder(args.root, args.base_url)
    for case, path, body, content_type in [
        ("exchange-empty", "/exchange", {}, "application/json"),
        ("exchange-invalid-json", "/exchange", b"{", "application/json"),
        (
            "info-json-suffix",
            "/info",
            {"type": "userRole", "user": args.user},
            "application/vnd.api+json",
        ),
        (
            "info-two-json-values",
            "/info",
            '{"type":"userRole","user":"' + args.user + '"}{}',
            "application/json",
        ),
    ]:
        report(case, recorder.request(case, path, body, content_type=content_type))
    base = {
        "action": {
            "type": "order",
            "orders": [
                {
                    "a": 10000,
                    "b": True,
                    "p": "5",
                    "s": "3",
                    "r": False,
                    "t": {"limit": {"tif": "Ioc"}},
                }
            ],
            "grouping": "na",
        },
        "nonce": 0,
        "signature": {"r": "0x0", "s": "0x0", "v": 27},
    }
    for field in ("p", "s"):
        for name, value in [
            ("nine-decimals", "1.000000001"),
            ("nine-zero-decimals", "1.000000000"),
            ("scientific", "1e1"),
            ("negative", "-1"),
            ("zero", "0"),
            ("nan", "NaN"),
            ("empty", ""),
            ("numeric", 3),
            ("overflow", "100000000000000000000"),
        ]:
            body = deepcopy(base)
            body["action"]["orders"][0][field] = value
            case = f"order-{field}-{name}"
            report(
                case,
                recorder.request(
                    case,
                    "/exchange",
                    body,
                    metadata={
                        "intended_mutation": "none; invalid zero signature",
                        "changed_field": field,
                    },
                ),
            )
    recorder.event({"state": "scenario_completed", "scenario": "unsigned-shapes"})


if __name__ == "__main__":
    main()
