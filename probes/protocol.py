"""Deterministic, unsigned /info and /exchange parser evidence campaign."""

import argparse
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from . import Recorder
from .matrix import info, report
from .recorder import TESTNET, encode

U64_MAX = (1 << 64) - 1
MISSING = object()


@dataclass(frozen=True)
class Case:
    name: str
    suite: str
    body: dict | bytes
    changed_field: str
    hypothesis: str
    content_type: str = "application/json"

    @property
    def metadata(self):
        return {
            "campaign": "unsigned-protocol",
            "suite": self.suite,
            "changed_field": self.changed_field,
            "hypothesis": self.hypothesis,
            "intended_mutation": "none; read-only query"
            if self.suite == "info"
            else "none; signature absent, malformed, or at least one scalar is zero",
        }


def cases(user: str) -> list[Case]:
    """Return fresh, ordered definitions without network, credentials, or signing."""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", user):
        raise ValueError("--user must be a public 20-byte 0x hexadecimal address")
    result = []

    def add(suite, name, body, field, hypothesis, content_type="application/json"):
        result.append(
            Case(f"{suite}-{name}", suite, body, field, hypothesis, content_type)
        )

    def variants(suite, base, prefix, path, values):
        for label, value, hypothesis in values:
            body = deepcopy(base)
            parent = body
            for key in path[:-1]:
                parent = parent[key]
            if value is MISSING:
                del parent[path[-1]]
            else:
                parent[path[-1]] = value
            add(
                suite,
                f"{prefix}-{label}",
                body,
                "/" + "/".join(map(str, path)),
                hypothesis,
            )

    role = {"type": "userRole", "user": user}
    order_status = {"type": "orderStatus", "user": user, "oid": 0}
    ledger = {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": 0}
    book = {"type": "l2Book", "coin": "@0"}
    fills = {"type": "userFills", "user": user}
    for name, body in [
        ("spot-meta", {"type": "spotMeta"}),
        ("spot-state", {"type": "spotClearinghouseState", "user": user}),
        ("pre-transfer", {"type": "preTransferCheck", "user": user}),
        ("book", book),
        ("order-status", order_status),
        ("fills", fills),
        ("ledger", ledger),
        ("fees", {"type": "userFees", "user": user}),
        ("role", role),
    ]:
        add(
            "info",
            f"valid-{name}",
            body,
            "/type",
            "Recognized query with nominal field shapes",
        )
    variants(
        "info",
        role,
        "type",
        ("type",),
        [
            ("missing", MISSING, "Query discriminator is required"),
            ("null", None, "Null differs from an absent discriminator"),
            (
                "integer",
                42,
                "Numeric discriminator may select a different enum representation",
            ),
            (
                "unknown",
                "notAnInfoType",
                "Unknown named query is rejected before execution",
            ),
        ],
    )
    for name, body, field, hypothesis in [
        ("root-null", b"null", "/", "Null cannot supply query fields"),
        (
            "root-array",
            encode(["userRole", user]),
            "/",
            "Sequence deserialization may differ from object deserialization",
        ),
        (
            "invalid-json",
            b'{"type":',
            "/",
            "Truncated JSON fails before typed decoding",
        ),
        (
            "trailing-input",
            encode(role) + b"{}",
            "/",
            "A second JSON value is not silently ignored",
        ),
        (
            "duplicate-type",
            b'{"type":"spotMeta","type":"userRole","user":' + encode(user) + b"}",
            "/type",
            "Duplicate discriminator handling differs from ordinary unknown fields",
        ),
        (
            "duplicate-user",
            b'{"type":"userRole","user":' + encode(user) + b',"user":null}',
            "/user",
            "Duplicate required field is rejected or resolved in a defined order",
        ),
    ]:
        add("info", name, body, field, hypothesis)
    for name, content_type in [
        ("json-suffix", "application/vnd.api+json"),
        ("text-content-type", "text/plain"),
        ("absent-content-type", ""),
    ]:
        add(
            "info",
            name,
            role,
            "header:Content-Type",
            "Media type controls whether JSON decoding runs",
            content_type,
        )
    variants(
        "info",
        role,
        "user",
        ("user",),
        [
            ("missing", MISSING, "Required address cannot be omitted"),
            ("null", None, "Required address cannot be null"),
            ("number", 0, "Address is not a numeric JSON value"),
            (
                "no-prefix",
                user[2:],
                "Address parser may require the hexadecimal prefix",
            ),
            ("short", "0x" + "1" * 38, "Address length below 20 bytes is invalid"),
            ("long", "0x" + "1" * 42, "Address length above 20 bytes is invalid"),
            (
                "nonhex",
                "0x" + "g" * 40,
                "Correct address length does not imply valid hex",
            ),
        ],
    )
    variants(
        "info",
        order_status,
        "oid",
        ("oid",),
        [
            ("missing", MISSING, "Order lookup identifier is required"),
            ("null", None, "Identifier union does not necessarily admit null"),
            ("boolean", True, "Boolean is distinct from an integer order ID"),
            ("negative", -1, "Integer order IDs have an unsigned lower bound"),
            ("u64-max", U64_MAX, "Largest unsigned 64-bit order ID is representable"),
            ("u64-overflow", U64_MAX + 1, "Order ID overflow is a schema boundary"),
            ("fractional", 1.5, "Fractional order IDs are not integers"),
            (
                "decimal-string",
                "1",
                "Numeric strings may enter the cloid branch rather than integer branch",
            ),
            (
                "cloid",
                "0x" + "1" * 32,
                "A 128-bit cloid is the second supported identifier shape",
            ),
            ("short-cloid", "0x12", "Cloid branch enforces fixed width"),
        ],
    )
    variants(
        "info",
        ledger,
        "start-time",
        ("startTime",),
        [
            (
                "missing",
                MISSING,
                "Start time may be optional despite documented request examples",
            ),
            ("null", None, "Explicit null may differ from missing time"),
            (
                "u64-max",
                U64_MAX,
                "Time accepts the maximum unsigned representation independently of useful range",
            ),
            ("negative", -1, "Negative epoch time tests signedness"),
            ("u64-overflow", U64_MAX + 1, "Time overflow is rejected at decoding"),
            ("string", "0", "Time strings are not coerced to integers"),
        ],
    )
    variants(
        "info",
        ledger,
        "end-time",
        ("endTime",),
        [
            ("null", None, "Optional end time permits or rejects explicit null"),
            ("fractional", 1.5, "Optional time still requires an integer"),
        ],
    )
    variants(
        "info",
        fills,
        "aggregate",
        ("aggregateByTime",),
        [
            ("true", True, "Optional aggregation boolean is recognized"),
            ("null", None, "Optional aggregation may distinguish null from absence"),
            ("number", 1, "Optional boolean rejects numeric coercion"),
        ],
    )
    variants(
        "info",
        book,
        "coin",
        ("coin",),
        [
            ("missing", MISSING, "Book requires a market identifier"),
            ("null", None, "Market identifier null differs from missing"),
            ("number", 0, "Market index is not a numeric JSON coin"),
        ],
    )
    variants(
        "info",
        book,
        "sigfigs",
        ("nSigFigs",),
        [
            (
                "null",
                None,
                "Null aggregation requests unaggregated depth or fails decoding",
            ),
            ("string", "5", "Aggregation count rejects string coercion"),
        ],
    )
    add(
        "info",
        "mantissa-without-sigfigs",
        {**book, "mantissa": 2},
        "/mantissa",
        "Mantissa may require a compatible nSigFigs",
    )
    add(
        "info",
        "mantissa-valid",
        {**book, "nSigFigs": 5, "mantissa": 2},
        "/nSigFigs,/mantissa",
        "Known supported aggregation combination reaches the book handler",
    )
    add(
        "info",
        "unknown-field",
        {**role, "protocolExtra": 1},
        "/protocolExtra",
        "Unknown query fields may be ignored",
    )

    order = {
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
        "vaultAddress": None,
        "expiresAfter": None,
    }
    send = {
        "action": {
            "type": "sendAsset",
            "destination": user.lower(),
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": "USDC:0x00000000000000000000000000000000",
            "amount": "1",
            "fromSubAccount": "",
            "nonce": 0,
            "signatureChainId": "0x66eee",
            "hyperliquidChain": "Testnet",
        },
        "nonce": 0,
        "signature": {"r": "0x0", "s": "0x0", "v": 27},
        "vaultAddress": None,
        "expiresAfter": None,
    }
    for name, body in [("order", order), ("send", send)]:
        add(
            "exchange",
            f"zero-signature-{name}",
            body,
            "/signature",
            "Nominal schema reaches signature rejection without authorization",
        )
    for field in ("action", "nonce", "signature"):
        variants(
            "exchange",
            order,
            f"envelope-{field}",
            (field,),
            [
                ("missing", MISSING, "Required envelope field cannot be omitted"),
                ("null", None, "Required envelope field cannot be null"),
            ],
        )
    variants(
        "exchange",
        order,
        "nonce",
        ("nonce",),
        [
            (
                "u64-max",
                U64_MAX,
                "Envelope nonce upper bound is representable before signature recovery",
            ),
            (
                "u64-overflow",
                U64_MAX + 1,
                "Envelope nonce overflow is a parser boundary",
            ),
            ("negative", -1, "Envelope nonce is unsigned"),
            ("string", "0", "Nonce does not coerce decimal strings"),
            ("fractional", 0.5, "Nonce requires integral JSON representation"),
        ],
    )
    variants(
        "exchange",
        order,
        "vault",
        ("vaultAddress",),
        [
            ("wrong-type", [], "Optional vault address still has a typed shape"),
        ],
    )
    variants(
        "exchange",
        order,
        "expiry",
        ("expiresAfter",),
        [
            ("u64-max", U64_MAX, "Optional expiry permits unsigned 64-bit maximum"),
            (
                "u64-overflow",
                U64_MAX + 1,
                "Optional expiry is not an arbitrary-precision integer",
            ),
        ],
    )
    variants(
        "exchange",
        order,
        "action-type",
        ("action", "type"),
        [
            ("unknown", "notAnAction", "Action discriminator rejects unknown variants"),
        ],
    )
    for scalar in ("r", "s"):
        variants(
            "exchange",
            order,
            f"signature-{scalar}",
            ("signature", scalar),
            [
                ("missing", MISSING, "Signature requires both scalars"),
                ("null", None, "Null is not a signature scalar"),
                (
                    "number",
                    0,
                    "Signature scalar JSON numeric representation may differ from hex string",
                ),
                ("empty", "", "Empty scalar differs from hexadecimal zero"),
                ("no-prefix", "0", "Scalar hex prefix may be required"),
                (
                    "full-width-zero",
                    "0x" + "0" * 64,
                    "Zero scalar padding tests fixed versus variable width parsing",
                ),
                (
                    "overflow",
                    "0x1" + "0" * 64,
                    "Scalar exceeding 256 bits is malformed; other scalar stays zero",
                ),
            ],
        )
    variants(
        "exchange",
        order,
        "signature-v",
        ("signature", "v"),
        [
            ("missing", MISSING, "Recovery ID is required"),
            ("string", "27", "Recovery ID rejects string coercion"),
            ("negative", -1, "Recovery ID signedness is a schema boundary"),
            ("overflow", 256, "Recovery ID may be constrained to an unsigned byte"),
            (
                "zero",
                0,
                "Zero recovery ID tests 0/1 versus 27/28 conventions with zero scalars",
            ),
        ],
    )
    order_fields = {
        "a": [
            ("negative", -1, "Asset index is unsigned"),
            ("overflow", 1 << 32, "Asset index may have a 32-bit bound"),
            ("string", "10000", "Asset index string is not an integer"),
        ],
        "b": [
            ("missing", MISSING, "Order side is required"),
            ("number", 1, "Order side must be a boolean"),
        ],
        "p": [
            ("number", 5, "Price is a decimal string, not a JSON number"),
            ("negative", "-1", "Negative decimal price may be rejected before crypto"),
            (
                "scientific",
                "1e1",
                "Scientific notation may not satisfy wire decimal grammar",
            ),
            (
                "nine-decimals",
                "1.000000001",
                "Price scale beyond eight decimals tests parser precision",
            ),
        ],
        "s": [
            ("null", None, "Size is a required decimal string"),
            ("zero", "0", "Zero size may parse before business validation"),
            ("nan", "NaN", "Nonfinite decimal text cannot become a finite order size"),
            (
                "nine-decimals",
                "1.000000001",
                "Size precision has its own wire boundary",
            ),
        ],
        "r": [
            ("missing", MISSING, "Reduce-only flag is required"),
            ("string", "false", "Reduce-only does not coerce strings"),
        ],
        "t": [
            ("missing", MISSING, "Order type union is required"),
            ("null", None, "Order type union cannot be null"),
            (
                "unknown-tif",
                {"limit": {"tif": "IOC"}},
                "Time-in-force enum is case sensitive",
            ),
            (
                "both-variants",
                {
                    "limit": {"tif": "Ioc"},
                    "trigger": {"isMarket": True, "triggerPx": "5", "tpsl": "tp"},
                },
                "Order type union cannot select two alternatives",
            ),
        ],
        "c": [
            ("null", None, "Optional cloid may accept explicit null"),
            ("valid", "0x" + "1" * 32, "Fixed-width cloid can reach crypto"),
            ("short", "0x12", "Order cloid enforces its byte width"),
            ("number", 1, "Order cloid rejects numeric IDs"),
        ],
    }
    for field, values in order_fields.items():
        variants(
            "exchange", order, f"order-{field}", ("action", "orders", 0, field), values
        )
    variants(
        "exchange",
        order,
        "grouping",
        ("action", "grouping"),
        [
            ("missing", MISSING, "Grouping is required"),
            ("unknown", "invalid", "Grouping must be a known enum value"),
        ],
    )
    variants(
        "exchange",
        order,
        "orders",
        ("action", "orders"),
        [
            ("empty", [], "Empty batch may deserialize before business rejection"),
            ("object", {}, "Order collection must be an array"),
        ],
    )
    send_fields = {
        "destination": [
            ("missing", MISSING, "Transfer destination is required"),
            ("number", 0, "Transfer destination has string or address shape"),
            (
                "short",
                "0x12",
                "Destination validation may occur before or after crypto",
            ),
        ],
        "token": [
            ("null", None, "Token identifier cannot be null"),
            ("number", 0, "Token identifier is not a numeric JSON index"),
            (
                "bare-name",
                "USDC",
                "Token symbol-only syntax may parse before token resolution",
            ),
        ],
        "sourceDex": [
            (
                "missing",
                MISSING,
                "Source DEX field is required even if empty represents perps",
            ),
            ("empty", "", "Empty source DEX is structurally distinct from spot"),
            ("number", 0, "Source DEX must be a string"),
        ],
        "destinationDex": [
            ("null", None, "Destination DEX cannot be null"),
            ("unknown", "protocolInvalidDex", "Unknown DEX name may survive parsing"),
        ],
        "nonce": [
            (
                "missing",
                MISSING,
                "Action nonce is distinct from envelope nonce and required",
            ),
            (
                "u64-max",
                U64_MAX,
                "Action nonce u64 maximum also differs from envelope nonce",
            ),
            (
                "u64-overflow",
                U64_MAX + 1,
                "Action nonce overflow is independently typed",
            ),
            ("string", "0", "Action nonce rejects numeric string coercion"),
        ],
        "fromSubAccount": [
            (
                "missing",
                MISSING,
                "Subaccount field may be required despite empty nominal value",
            ),
            ("null", None, "Null subaccount is distinct from empty string"),
            (
                "address",
                user,
                "A nonempty subaccount address tests its wire representation",
            ),
        ],
        "signatureChainId": [
            (
                "missing",
                MISSING,
                "Signature chain ID is required for typed-data recovery",
            ),
            (
                "number",
                421614,
                "Signature chain ID is hexadecimal text rather than JSON integer",
            ),
            (
                "invalid-hex",
                "0xgg",
                "Chain ID hex parsing may be deferred until recovery",
            ),
        ],
        "hyperliquidChain": [
            ("null", None, "Environment chain discriminator cannot be null"),
            ("unknown", "testnet", "Environment chain names may be case sensitive"),
        ],
        "amount": [
            ("number", 1, "Transfer amount is decimal text rather than JSON number")
        ],
    }
    for field, values in send_fields.items():
        variants("exchange", send, f"send-{field}", ("action", field), values)
    for name, path in [
        ("envelope-extra", ("protocolExtra",)),
        ("action-extra", ("action", "protocolExtra")),
        ("order-extra", ("action", "orders", 0, "protocolExtra")),
    ]:
        variants(
            "exchange",
            order,
            name,
            path,
            [
                (
                    "field",
                    1,
                    "Unknown fields may be ignored or denied at this nesting level",
                )
            ],
        )
    add(
        "exchange",
        "root-array",
        encode([order["action"], 0, order["signature"], None, None]),
        "/",
        "Envelope sequence decoding may differ from named object decoding",
    )
    add(
        "exchange",
        "duplicate-nonce",
        encode(order)[:-1] + b',"nonce":1}',
        "/nonce",
        "Duplicate envelope field may fail typed decoding rather than use last value",
    )
    return result


def observed_class(suite, result):
    if result.transport_error:
        return "transport-error"
    if result.status == 400:
        return "http-400-parser-candidate"
    if result.status == 422:
        return "http-422-schema-candidate"
    if result.status == 200 and suite == "exchange":
        return "http-200-crypto-candidate"
    return f"http-{result.status or 'unknown'}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--base-url", default=TESTNET)
    parser.add_argument("--suite", choices=("info", "exchange", "all"), default="all")
    parser.add_argument(
        "--user", required=True, help="Public 20-byte address; never a private key"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print exact case definitions; no network or capture files",
    )
    args = parser.parse_args()
    try:
        selected = [
            case
            for case in cases(args.user)
            if args.suite == "all" or args.suite == case.suite
        ]
    except ValueError as exc:
        parser.error(str(exc))
    if args.list:
        print(
            json.dumps(
                [
                    {
                        "case_id": case.name,
                        "path": "/" + case.suite,
                        "content_type": case.content_type,
                        "parameters": case.metadata,
                        "request_body": (
                            encode(case.body)
                            if isinstance(case.body, dict)
                            else case.body
                        ).decode(),
                    }
                    for case in selected
                ],
                indent=2,
            )
        )
        return
    recorder = Recorder(args.root, args.base_url)
    recorder.event(
        {
            "state": "scenario_started",
            "scenario": "unsigned-protocol",
            "suite": args.suite,
            "case_count": len(selected),
            "user": args.user,
        }
    )
    for case in selected:
        kwargs = {"metadata": case.metadata, "content_type": case.content_type}
        if case.suite == "info":
            result = info(recorder, case.name, case.body, **kwargs)
        else:
            result = recorder.request(case.name, "/exchange", case.body, **kwargs)
            report(case.name, result)
        recorder.event(
            {
                "state": "protocol_observation",
                "case_id": case.name,
                "response_class": observed_class(case.suite, result),
                "status": result.status,
                "classification_note": "Status-based candidate only; raw response determines the actual rejection stage",
            }
        )
    recorder.event(
        {
            "state": "scenario_completed",
            "scenario": "unsigned-protocol",
            "suite": args.suite,
            "case_count": len(selected),
        }
    )


if __name__ == "__main__":
    main()
