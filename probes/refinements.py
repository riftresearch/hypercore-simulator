"""Small, separately selectable parser, nonce-reuse, and token-alias observations."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from hyperliquid.utils.signing import (
    SEND_ASSET_SIGN_TYPES,
    recover_user_from_user_signed_action,
    sign_send_asset_action,
)

from . import Recorder
from .matrix import report
from .recorder import TESTNET, create, encode, timestamp
from .scenarios import free_balance, load_meta, reserve_nonce, snapshot, submit, wallet

SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
DEFAULT_NONCE_SOURCE = Path(
    "captures/testnet/accounts-nonce-floor/nonce-floor-seed-000.signed.json"
)
PUBLIC_USER = "0x111122223333444455556666777788889999aaaa"


class ObservationRecorder(Recorder):
    """Include helper-generated snapshots in results and halt on any 429."""

    def __init__(self, root, suite):
        super().__init__(root, TESTNET)
        self.suite = suite
        self.observations = []

    def request(self, case_id, path, body, **kwargs):
        raw = (
            encode(body)
            if isinstance(body, dict)
            else (body.encode() if isinstance(body, str) else body)
        )
        metadata = {
            "suite": self.suite,
            "automatic_retry": False,
            "intended_mutation": "none"
            if path == "/info"
            else "zero-r/s unsigned request",
            **kwargs.pop("metadata", {}),
        }
        result = super().request(case_id, path, raw, metadata=metadata, **kwargs)
        self.observations.append(
            {
                "case": case_id,
                "path": path,
                "metadata": metadata,
                "request_body": raw.decode(),
                "request_sha256": hashlib.sha256(raw).hexdigest(),
                "status": result.status,
                "response": result.json,
                "response_body": result.body.decode(errors="replace"),
                "capture_directory": str(result.directory),
                "transport_error": result.transport_error,
            }
        )
        if result.status == 429 or result.transport_error:
            raise RuntimeError("Rate limit or ambiguous transport; halt without retry")
        return result


def unsigned_cases(
    user, source_address, now_ms, existing_address, funded_source_address
):
    """Exact-byte definitions only: no wallet, capture writes, or transport."""
    for label, address in (
        ("a", source_address),
        ("b", existing_address),
        ("c", funded_source_address),
    ):
        yield (
            "pretransfer-context-balances-" + label,
            "/info",
            encode(
                {
                    "type": "spotClearinghouseState",
                    "user": address,
                }
            ),
            {"context_actor": label, "public_address": address},
        )
    yield (
        "pretransfer-context-destination-role",
        "/info",
        encode(
            {
                "type": "userRole",
                "user": user,
            }
        ),
        {"purpose": "Observe destination role without assuming freshness"},
    )
    for label, destination, source in (
        ("fresh-source-a", user, source_address),
        ("fresh-source-zero", user, "0x" + "0" * 40),
        ("fresh-source-c", user, funded_source_address),
        ("existing-b-source-a", existing_address, source_address),
        ("fresh-source-null", user, None),
        ("fresh-source-boolean", user, True),
    ):
        yield (
            "pretransfer-map-" + label,
            "/info",
            encode(
                {
                    "type": "preTransferCheck",
                    "user": destination,
                    "sourceAddress": source,
                }
            ),
            {
                "destination": destination,
                "sourceAddress": source,
                "purpose": "Observe source-aware fee and nullable-field behavior",
            },
        )
    ledger = {"type": "userNonFundingLedgerUpdates", "user": user}
    for label, end in (
        ("zero", 0),
        ("current", now_ms),
        ("future", now_ms + 86_400_000),
        ("day", 86_400_000),
        ("year9999", 253402300799999),
    ):
        yield (
            "ledger-end-only-" + label,
            "/info",
            encode({**ledger, "endTime": end}),
            {
                "changed_field": "endTime",
                "endTime": end,
                "startTime_present": False,
                "reference_now_ms": now_ms,
            },
        )
    yield (
        "ledger-start-zero-end-year9999",
        "/info",
        encode(
            {
                **ledger,
                "startTime": 0,
                "endTime": 253402300799999,
            }
        ),
        {"changed_field": "startTime", "startTime_present": True},
    )
    for label, fields in (
        ("null", [user, None]),
        ("true", [user, True]),
        ("source-address", [user, source_address]),
        ("two-nulls", [user, None, None]),
    ):
        yield (
            "pretransfer-array-" + label,
            "/info",
            encode(
                [
                    "preTransferCheck",
                    *fields,
                ]
            ),
            {"sequence_fields": fields},
        )
    yield (
        "duplicate-unknown-info",
        "/info",
        (
            encode({"type": "userRole", "user": user})[:-1]
            + b',"refinementIgnored":1,"refinementIgnored":2}'
        ),
        {"duplicate_field": "refinementIgnored", "location": "info root"},
    )
    order = (
        b'{"a":10000,"b":true,"p":"5","s":"3","r":false,"t":{"limit":{"tif":"Ioc"}}}'
    )
    variants = (
        (
            "unknown-order",
            order[:-1] + b',"refinementIgnored":1,"refinementIgnored":2}',
        ),
        ("known-order-p-same", order.replace(b'"p":"5"', b'"p":"5","p":"5"')),
        (
            "unknown-nested-ignored",
            order[:-1] + b',"refinementIgnored":{"nested":1,"nested":2}}',
        ),
    )
    for label, raw_order in variants:
        raw = (
            b'{"action":{"type":"order","orders":['
            + raw_order
            + b'],"grouping":"na"},"nonce":0,"signature":{"r":"0x0","s":"0x0","v":27},'
            b'"vaultAddress":null,"expiresAfter":null}'
        )
        yield (
            "duplicate-" + label,
            "/exchange",
            raw,
            {
                "changed_field": label,
                "signature_r": "0x0",
                "signature_s": "0x0",
            },
        )


def field_cases(user, source_address, existing_address):
    yield "field-destination-role", "/info", {"type": "userRole", "user": user}, {}
    yield (
        "field-destination-absent",
        "/info",
        {"type": "preTransferCheck", "user": user},
        {},
    )
    for field in ("sourceDex", "dex", "token", "coin", "sourceAddressForFee"):
        for label, value in (("address", source_address), ("boolean", True)):
            yield (
                f"field-{field}-{label}",
                "/info",
                {
                    "type": "preTransferCheck",
                    "user": user,
                    field: value,
                },
                {"candidate_optional_field": field},
            )
    for label, source in (
        ("zero", "0x" + "0" * 40),
        ("missing", user),
        ("existing", existing_address),
        ("spot", "spot"),
        ("empty", ""),
        ("token", "USDC"),
        ("invalid-address", "x"),
    ):
        yield (
            "array-source-" + label,
            "/info",
            encode(["preTransferCheck", user, source]),
            {},
        )
    yield (
        "array-existing-destination",
        "/info",
        encode(["preTransferCheck", existing_address, source_address]),
        {},
    )


def journal_reuse(path, signer, case_id, envelope, source):
    """Record deliberate reuse without reserving or substituting a fresh nonce."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(
            encode(
                {
                    "signer": signer.lower(),
                    "base_url": TESTNET,
                    "nonce": envelope["nonce"],
                    "requested_nonce": envelope["nonce"],
                    "case_id": case_id,
                    "reserved_at": timestamp(),
                    "intentional_reuse": True,
                    "source": str(source),
                    "envelope_sha256": hashlib.sha256(encode(envelope)).hexdigest(),
                }
            ).decode()
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())


def seed(path, signer):
    raw = path.read_bytes()
    envelope = json.loads(raw)
    action = envelope["action"]
    if not (
        action["type"] == "sendAsset"
        and action["amount"] == "0"
        and action["sourceDex"] == action["destinationDex"] == "spot"
        and action["fromSubAccount"] == ""
        and action["hyperliquidChain"] == "Testnet"
        and action["nonce"] == envelope["nonce"]
        and envelope.get("vaultAddress") is None
        and envelope.get("expiresAfter") is None
    ):
        raise ValueError(
            "Nonce source must be a captured zero-amount testnet spot send"
        )
    recovered = recover_user_from_user_signed_action(
        deepcopy(action),
        envelope["signature"],
        SEND_ASSET_SIGN_TYPES,
        "HyperliquidTransaction:SendAsset",
        False,
    )
    if recovered.lower() != signer.address.lower():
        raise ValueError("Nonce source is not signed by configured wallet C")
    return envelope, {
        "nonce_source": str(path),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def send(recorder, args, case_id, signer, destination, token, amount, metadata):
    nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case_id)
    action = {
        "type": "sendAsset",
        "destination": destination.lower(),
        "sourceDex": "spot",
        "destinationDex": "spot",
        "token": token,
        "amount": amount,
        "fromSubAccount": "",
        "nonce": nonce,
    }
    signature = sign_send_asset_action(signer, action, False)
    return submit(
        recorder,
        case_id,
        {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        },
        {
            "signer": signer.address.lower(),
            "recipient": destination.lower(),
            **metadata,
        },
        True,
    )


def run_signed(recorder, args):
    c = wallet("HYPERLIQUID_C_PRIVATE_KEY")
    old, old_metadata = seed(args.nonce_source, c)
    latest_path = args.nonce_source.with_name("nonce-floor-seed-104.signed.json")
    latest, latest_metadata = seed(latest_path, c)
    if latest["nonce"] <= old["nonce"]:
        raise ValueError("Retained source nonce must be newer than pruned source nonce")
    meta = load_meta(recorder)
    usdc = next(token for token in meta["tokens"] if token["name"] == "USDC")
    before = snapshot(recorder, "signed-before-c", c.address)
    if before["role"] != {"role": "user"} or free_balance(
        before["balances"], usdc["index"]
    ) < Decimal(2):
        raise RuntimeError(
            "Signed refinements require existing C funded with at least 2 USDC; no automatic funding"
        )
    variants = [
        (
            "nonce-pruned-exact-original-control",
            deepcopy(old),
            {
                **old_metadata,
                "changed_fields": [],
                "same_action": True,
                "same_nonce": True,
                "same_signature": True,
                "purpose": "Contemporaneous original replay before changed-payload reuse",
            },
        )
    ]
    for label, original, metadata in (
        ("pruned", old, old_metadata),
        ("retained-104", latest, latest_metadata),
    ):
        envelope = deepcopy(original)
        envelope["action"]["amount"] = "+0.0"
        envelope["signature"] = sign_send_asset_action(c, envelope["action"], False)
        variants.append(
            (
                "nonce-" + label + "-new-zero-text",
                envelope,
                {
                    **metadata,
                    "changed_fields": ["action.amount", "signature"],
                    "original_amount": original["action"]["amount"],
                    "new_amount": "+0.0",
                    "same_nonce": True,
                },
            )
        )
    high_s = deepcopy(old)
    sig = high_s["signature"]
    if sig["v"] not in (27, 28) or not 0 < int(sig["s"], 16) <= SECP256K1_ORDER // 2:
        raise ValueError("Expected original low-s signature with v 27/28")
    sig["s"] = hex(SECP256K1_ORDER - int(sig["s"], 16))
    sig["v"] = 55 - sig["v"]
    variants.append(
        (
            "nonce-pruned-original-payload-high-s",
            high_s,
            {
                **old_metadata,
                "changed_fields": ["signature.s", "signature.v"],
                "same_action": True,
                "same_nonce": True,
            },
        )
    )
    for case_id, envelope, metadata in variants:
        journal_reuse(
            args.nonce_file, c.address, case_id, envelope, metadata["nonce_source"]
        )
        submit(
            recorder,
            case_id,
            envelope,
            {
                **metadata,
                "signer": c.address.lower(),
                "intended_mutation": "zero-amount financial no-op; nonce processing observed",
                "maximum_token_quantity": "0",
                "nonce_age_ms": time.time_ns() // 1_000_000 - envelope["nonce"],
            },
            True,
        )
    send(
        recorder,
        args,
        "transfer-c-sub-wei-usdc",
        c,
        old["action"]["destination"],
        usdc["name"] + ":" + usdc["tokenId"],
        "0.000000001",
        {
            "intended_mutation": "at most 0.000000001 USDC transfer to existing seed recipient",
            "maximum_token_quantity": "0.000000001",
            "wei_decimals": usdc["weiDecimals"],
        },
    )
    snapshot(recorder, "signed-after-c", c.address)


def run_aliases(recorder, args):
    a = wallet("HYPERLIQUID_PRIVATE_KEY")
    b = wallet("HYPERLIQUID_B_PRIVATE_KEY")
    if a.address.lower() == b.address.lower():
        raise ValueError("Alias source A and existing recipient B must differ")
    meta = load_meta(recorder)
    before_a = snapshot(recorder, "aliases-before-a", a.address)
    before_b = snapshot(recorder, "aliases-before-b", b.address)
    if before_a["role"] != {"role": "user"} or before_b["role"] != {"role": "user"}:
        raise RuntimeError(
            "Aliases require existing A and B; never activate a recipient"
        )
    cases = []
    for name in ("PURR", "NQ"):
        token = next(item for item in meta["tokens"] if item["name"] == name)
        wei = Decimal(1).scaleb(-token["weiDecimals"])
        quantity = max(Decimal("0.00000001"), wei)
        bound = Decimal("0.00001") if name == "PURR" else wei
        if quantity > bound:
            raise RuntimeError(
                "Token precision is incompatible with bounded alias quantity"
            )
        if free_balance(before_a["balances"], token["index"]) < quantity * 3:
            raise RuntimeError(
                "Insufficient visible A balance for three bounded alias sends"
            )
        token_id = token["tokenId"]
        if not token_id.startswith("0x"):
            raise ValueError("Expected hexadecimal token ID for leading-zero variant")
        for label, wire_token in (
            ("bare", name),
            ("name-case-wrong", name.lower() + ":" + token_id),
            ("leading-zero-token-id", name + ":0x0" + token_id[2:]),
        ):
            cases.append(
                (
                    "alias-" + name.lower() + "-" + label,
                    wire_token,
                    format(quantity, "f"),
                    {
                        "intended_mutation": "bounded token transfer A to existing B if accepted",
                        "canonical_token": name + ":" + token_id,
                        "token_index": token["index"],
                        "wei_decimals": token["weiDecimals"],
                        "one_wei": format(wei, "f"),
                        "maximum_token_quantity": format(quantity, "f"),
                        "suite_maximum_token_quantity": format(quantity * 3, "f"),
                    },
                )
            )
    usdc = next(item for item in meta["tokens"] if item["name"] == "USDC")
    usdc_amounts = ("0.0000001", "0.00000019")
    usdc_total = sum(map(Decimal, usdc_amounts))
    if free_balance(before_a["balances"], usdc["index"]) < usdc_total:
        raise RuntimeError("Insufficient A USDC for bounded ledger precision sends")
    for label, amount in zip(
        ("one-tenth-micro", "nineteen-hundredths-micro"), usdc_amounts
    ):
        cases.append(
            (
                "ledger-usdc-value-" + label,
                usdc["name"] + ":" + usdc["tokenId"],
                amount,
                {
                    "intended_mutation": "bounded USDC transfer A to existing B",
                    "purpose": "Observe transfer amount versus ledger usdcValue precision",
                    "token_index": usdc["index"],
                    "wei_decimals": usdc["weiDecimals"],
                    "maximum_token_quantity": amount,
                    "suite_maximum_token_quantity": format(usdc_total, "f"),
                },
            )
        )
    for case_id, wire_token, amount, metadata in cases:
        send(recorder, args, case_id, a, b.address, wire_token, amount, metadata)
    snapshot(recorder, "aliases-post-probes-a", a.address)
    credited_b = snapshot(recorder, "aliases-post-probes-b", b.address)
    bounds = {}
    for _, _, amount, metadata in cases:
        index = metadata["token_index"]
        bounds[index] = bounds.get(index, Decimal(0)) + Decimal(amount)

    def total(state, index):
        return sum(
            (
                Decimal(item["total"])
                for item in state["balances"]["balances"]
                if item["token"] == index
            ),
            Decimal(0),
        )

    returns = []
    for index, bound in bounds.items():
        baseline = total(before_b, index)
        delta = total(credited_b, index) - baseline
        recorder.event(
            {
                "state": "alias_credit_observed",
                "token_index": index,
                "initial_b_total": str(baseline),
                "credited_delta": str(delta),
                "maximum_probe_credit": str(bound),
            }
        )
        if delta < 0 or delta > bound:
            raise RuntimeError(
                "B balance delta outside probe bounds; do not drain initial balances"
            )
        if delta == 0:
            continue
        if free_balance(credited_b["balances"], index) < delta:
            raise RuntimeError(
                "Newly credited B quantity is not available for bounded return"
            )
        token = next(item for item in meta["tokens"] if item["index"] == index)
        returns.append((token, delta, baseline))
    for token, delta, baseline in returns:
        send(
            recorder,
            args,
            "alias-return-new-credit-" + token["name"].lower(),
            b,
            a.address,
            token["name"] + ":" + token["tokenId"],
            format(delta, "f"),
            {
                "intended_mutation": "Return only newly observed probe credit B to A",
                "token_index": token["index"],
                "maximum_token_quantity": str(delta),
                "preserved_initial_b_total": str(baseline),
            },
        )
    snapshot(recorder, "aliases-after-a", a.address)
    final_b = snapshot(recorder, "aliases-after-b", b.address)
    for index in bounds:
        if total(final_b, index) != total(before_b, index):
            raise RuntimeError(
                "Alias return did not restore observed initial B total; inspect captures before another campaign"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "--suite", choices=("unsigned", "fields", "signed", "aliases"), required=True
    )
    parser.add_argument(
        "--execute", action="store_true", help="Required for signed and aliases"
    )
    parser.add_argument(
        "--user", default=PUBLIC_USER, help="Public address for unsigned reads"
    )
    parser.add_argument(
        "--source-address",
        default=os.environ.get("HYPERLIQUID_ADDRESS", ""),
        help="Public A address; defaults to HYPERLIQUID_ADDRESS",
    )
    parser.add_argument(
        "--existing-address",
        default=os.environ.get("HYPERLIQUID_B_ADDRESS", ""),
        help="Public existing B address; defaults to HYPERLIQUID_B_ADDRESS",
    )
    parser.add_argument(
        "--funded-source-address",
        default=os.environ.get("HYPERLIQUID_C_ADDRESS", ""),
        help="Public C address; defaults to HYPERLIQUID_C_ADDRESS",
    )
    parser.add_argument(
        "--nonce-source",
        type=Path,
        default=DEFAULT_NONCE_SOURCE,
        help="Seed 000 signed envelope; sibling seed 104 is used as retained source",
    )
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    if args.suite not in ("unsigned", "fields") and not args.execute:
        parser.error("Signed and aliases suites require explicit --execute")
    if args.suite in ("unsigned", "fields"):
        for address in (
            args.user,
            args.source_address,
            args.existing_address,
            args.funded_source_address,
        ):
            if not re.fullmatch(r"0x[0-9a-fA-F]{40}", address):
                parser.error(
                    "Unsigned suite requires public --source-address A, --existing-address B, "
                    "--funded-source-address C (or corresponding ADDRESS environment variables)"
                )
    recorder = ObservationRecorder(args.root, args.suite)
    recorder.event(
        {
            "state": "scenario_started",
            "scenario": "refinements",
            "suite": args.suite,
            "automatic_retry": False,
            "serial_execution_required": True,
            "omitted": {
                "huge_transfer": "Not needed for bounded refinements; no huge signed sends"
            },
        }
    )
    outcome = "aborted"
    try:
        if args.suite == "unsigned":
            now_ms = time.time_ns() // 1_000_000
            for case_id, path, body, metadata in unsigned_cases(
                args.user,
                args.source_address,
                now_ms,
                args.existing_address,
                args.funded_source_address,
            ):
                report(
                    case_id, recorder.request(case_id, path, body, metadata=metadata)
                )
        elif args.suite == "fields":
            for case_id, path, body, metadata in field_cases(
                args.user, args.source_address, args.existing_address
            ):
                report(
                    case_id, recorder.request(case_id, path, body, metadata=metadata)
                )
        elif args.suite == "signed":
            run_signed(recorder, args)
        else:
            run_aliases(recorder, args)
        outcome = "completed"
        recorder.event(
            {
                "state": "scenario_completed",
                "scenario": "refinements",
                "suite": args.suite,
            }
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise
    finally:
        create(
            recorder.root / "results.json",
            encode(
                {
                    "suite": args.suite,
                    "outcome": outcome,
                    "observations": recorder.observations,
                }
            ),
        )


if __name__ == "__main__":
    main()
