"""Official SDK signing only; raw Recorder remains the sole HTTP transport.

All decimal wires are constructed without binary floating point. No wallet or
private-key value is persisted; keys must be supplied via process environment.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from eth_account import Account
from eth_utils.exceptions import ValidationError
from hyperliquid.utils.signing import sign_l1_action, sign_send_asset_action

from .matrix import info, report
from .recorder import create, encode, timestamp


def decimal(value: str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Decimal must be finite")
    return result


def wire(value: Decimal) -> str:
    raw = format(value, "f")
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def wallet(variable: str):
    value = os.environ.get(variable)
    if not value:
        raise ValueError(
            f"Missing environment variable {variable}; export it without logging"
        )
    try:
        return Account.from_key(value)
    except (ValueError, TypeError, ValidationError):
        raise ValueError(f"Invalid private key in {variable}") from None


def reserve_nonce(
    path: Path, signer: str, base_url: str, case_id: str, offset=0
) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        used = {
            row["nonce"]
            for line in stream
            if (row := json.loads(line))
            and row["signer"] == signer.lower()
            and row["base_url"] == base_url
        }
        requested = time.time_ns() // 1_000_000 + offset
        nonce = requested
        while nonce in used:
            nonce += 1
        stream.write(
            json.dumps(
                {
                    "signer": signer.lower(),
                    "base_url": base_url,
                    "nonce": nonce,
                    "requested_nonce": requested,
                    "offset_ms": offset,
                    "case_id": case_id,
                    "reserved_at": timestamp(),
                }
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())
        return nonce


def snapshot(recorder, prefix, user):
    result = {}
    for name, body in (
        ("balances", {"type": "spotClearinghouseState", "user": user}),
        ("role", {"type": "userRole", "user": user}),
        ("pretransfer", {"type": "preTransferCheck", "user": user}),
        (
            "ledger",
            {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": 0},
        ),
        ("fills", {"type": "userFills", "user": user}),
        ("fees", {"type": "userFees", "user": user}),
    ):
        response = info(recorder, f"{prefix}-{name}", body)
        if response.transport_error or (
            response.status != 200 and name != "pretransfer"
        ):
            raise RuntimeError(
                f"Required snapshot {prefix}-{name} failed; inspect evidence"
            )
        result[name] = response.json
    return result


def free_balance(state, token_index: int) -> Decimal:
    if not isinstance(state, dict) or not isinstance(state.get("balances"), list):
        raise TypeError("Balance response shape unavailable")
    for item in state["balances"]:
        if item["token"] == token_index:
            return decimal(item["total"]) - decimal(item["hold"])
    return Decimal(0)


def load_meta(recorder):
    result = info(recorder, "setup-spot-meta", {"type": "spotMeta"})
    if (
        result.transport_error
        or not isinstance(result.json, dict)
        or "tokens" not in result.json
    ):
        raise RuntimeError("Required spotMeta unavailable")
    create(recorder.root / "spotMeta.json", result.body)
    return result.json


def submit(recorder, case_id, envelope, metadata, execute):
    reject_path = recorder.root / (case_id + ".signed.json")
    create(reject_path, encode(envelope))
    recorder.event(
        {
            "state": "signed_case_generated",
            "case_id": case_id,
            "envelope_file": reject_path.name,
            "nonce": envelope["nonce"],
            "execute": execute,
            **metadata,
        }
    )
    if not execute:
        print(f"{case_id}: signed plan only", flush=True)
        return None
    result = recorder.request(case_id, "/exchange", envelope, metadata=metadata)
    report(case_id, result)
    if result.transport_error:
        raise RuntimeError(
            "Ambiguous exchange submission; halted without retry. Resolve with read-only queries."
        )
    return result


def choose_cases(value, allowed):
    cases = value.split(",")
    if (
        not cases
        or len(set(cases)) != len(cases)
        or any(case not in allowed for case in cases)
    ):
        raise ValueError("Cases must be a unique subset of: " + ",".join(allowed))
    return cases


def run_send(recorder, args):
    allowed = (
        "fresh",
        "existing",
        "replay",
        "tamper",
        "wrong-key",
        "insufficient",
        "bad-token",
        "bad-source-dex",
        "bad-destination-dex",
        "bad-amount",
        "zero-amount",
        "negative-amount",
        "old-outside",
        "old-inside",
        "future-inside",
        "future-outside",
        "old-interior",
        "future-interior",
    )
    cases = choose_cases(args.cases, allowed)
    sender = wallet(args.key_env)
    recipient = args.recipient or os.environ.get("HYPERLIQUID_B_ADDRESS", "")
    if (
        not re.fullmatch(r"0x[0-9a-fA-F]{40}", recipient)
        or recipient.lower() == sender.address.lower()
    ):
        raise ValueError(
            "Recipient must be a distinct explicit address or HYPERLIQUID_B_ADDRESS"
        )
    initial, amount, budget, max_fee = map(
        decimal, (args.initial_amount, args.amount, args.max_spend, args.max_fee)
    )
    if min(initial, amount, budget) <= 0 or max_fee < 0:
        raise ValueError("Amounts/budget must be positive and max fee nonnegative")
    meta = load_meta(recorder)
    usdc = next(token for token in meta["tokens"] if token["name"] == "USDC")
    token_wire = usdc["name"] + ":" + usdc["tokenId"]
    consumed = Decimal(0)
    previous = None
    planned_fresh = False
    offsets = {
        "old-outside": -172_801_000,
        "old-inside": -172_799_000,
        "future-inside": 86_399_000,
        "future-outside": 86_401_000,
        "old-interior": -172_500_000,
        "future-interior": 86_100_000,
    }
    for case in cases:
        prefix = "send-" + case
        signer = (
            wallet(args.wrong_key_env)
            if case in ("wrong-key", "insufficient")
            else sender
        )
        destination = (
            sender.address if case in ("wrong-key", "insufficient") else recipient
        )
        before_sender = snapshot(recorder, prefix + "-before-sender", signer.address)
        before_recipient = snapshot(recorder, prefix + "-before-recipient", destination)
        check = before_recipient["pretransfer"]
        role = before_recipient["role"]
        if not isinstance(role, dict) or not isinstance(role.get("role"), str):
            raise TypeError("Cannot establish recipient role")
        exists = role["role"] != "missing"
        if case == "fresh" and exists:
            raise RuntimeError(
                "Fresh-recipient precondition failed; create a new controlled account, never silently reuse"
            )
        if (
            case == "existing"
            and not exists
            and not (not args.execute and planned_fresh)
        ):
            raise RuntimeError(
                "Existing-recipient precondition failed; execute fresh first"
            )
        if isinstance(check, dict) and "fee" in check:
            fee = decimal(check["fee"])
            if fee < 0 or fee > max_fee:
                raise RuntimeError("Observed activation fee exceeds explicit fee bound")
        else:
            recorder.event(
                {
                    "state": "pretransfer_unavailable",
                    "case_id": prefix,
                    "authoritative_role": role,
                    "reserved_fee_bound": wire(max_fee),
                    "assumption": "fee bound explicitly supplied; preTransferCheck did not establish fee",
                }
            )
        value = initial if case == "fresh" else amount
        if case == "insufficient":
            value = free_balance(before_sender["balances"], usdc["index"]) + Decimal(1)
        if case == "replay" and previous is not None:
            value = decimal(previous["action"]["amount"])
        # Even malformed/replayed payloads are charged against the hypothetical spend budget.
        count = 2 if case == "replay" and previous is None else 1
        if consumed + (value + max_fee) * count > budget:
            raise RuntimeError(
                "Worst-case send principal plus fee reservation exceeds --max-spend"
            )
        consumed += (value + max_fee) * count
        nonce = reserve_nonce(
            args.nonce_file,
            signer.address,
            recorder.base_url,
            prefix,
            offsets.get(case, 0),
        )
        action = {
            "type": "sendAsset",
            "destination": destination.lower(),
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": token_wire,
            "amount": wire(value),
            "fromSubAccount": "",
            "nonce": nonce,
        }
        if case == "bad-token":
            action["token"] = "INVALID:0x" + "0" * 32
        elif case == "bad-source-dex":
            action["sourceDex"] = "invalid-dex"
        elif case == "bad-destination-dex":
            action["destinationDex"] = "invalid-dex"
        elif case == "bad-amount":
            action["amount"] = "not-a-decimal"
        elif case == "zero-amount":
            action["amount"] = "0"
        elif case == "negative-amount":
            action["amount"] = "-" + wire(value)
        signature = sign_send_asset_action(signer, action, False)
        envelope = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        if case == "tamper":
            envelope["action"]["amount"] = wire(value / 2)
        description = {
            "signer": signer.address.lower(),
            "intended_mutation": "spot USDC transfer if accepted",
            "scenario": case,
            "recipient": destination.lower(),
            "worst_case_reserved": wire(consumed),
            "setup_requirement": "recipient fresh"
            if case == "fresh"
            else "recipient existing"
            if case == "existing"
            else "see pre-state",
            "nonce_offset_ms": offsets.get(case, 0),
            "assumption": "nonce edge offsets target documented windows; wall-clock and transit affect exact boundary",
        }
        if case == "replay":
            if previous is None:
                submit(recorder, prefix + "-seed", envelope, description, args.execute)
                previous = deepcopy(envelope)
            envelope = deepcopy(previous)
            description["signer"] = sender.address.lower()
            description["intended_mutation"] = (
                "exact payload replay; duplicate rejection is a hypothesis, not a guarantee"
            )
        submit(recorder, prefix, envelope, description, args.execute)
        if case in ("fresh", "existing"):
            previous = deepcopy(envelope)
        if case == "fresh":
            planned_fresh = True
        if args.execute:
            snapshot(recorder, prefix + "-after-sender", signer.address)
            snapshot(recorder, prefix + "-after-recipient", destination)
    recorder.event(
        {
            "state": "scenario_completed",
            "scenario": "send",
            "execute": args.execute,
            "reserved_principal_and_fees": wire(consumed),
        }
    )


def price_wire(value: Decimal, sz_decimals: int, rounding) -> Decimal:
    # Spot allows 8-szDecimals decimal places and 5 significant figures; integer prices are allowed.
    places = max(0, 8 - sz_decimals)
    quantum = Decimal(1).scaleb(max(-places, value.adjusted() - 4))
    return value.quantize(quantum, rounding=rounding)


def run_ioc(recorder, args):
    cases = choose_cases(
        args.cases,
        (
            "full",
            "no-fill",
            "minimum",
            "size-precision",
            "price-precision",
            "partial-attempt",
        ),
    )
    signer = wallet(args.key_env)
    target, budget = map(decimal, (args.notional, args.max_spend))
    if min(target, budget) <= 0:
        raise ValueError("Notional/budget must be positive")
    meta = load_meta(recorder)
    tokens = {token["index"]: token for token in meta["tokens"]}
    universe = sorted(meta["universe"], key=lambda market: market["index"])
    market = next(
        (
            market
            for market in universe
            if (
                market["name"] == args.coin
                if args.coin
                else tokens[market["tokens"][1]]["name"] == "USDC"
            )
        ),
        None,
    )
    if market is None or tokens[market["tokens"][1]]["name"] != "USDC":
        raise ValueError("Choose a captured USDC-quoted spot market")
    base, quote = (tokens[index] for index in market["tokens"])
    step = Decimal(1).scaleb(-base["szDecimals"])
    consumed = Decimal(0)
    buy = args.side == "buy"
    for case in cases:
        prefix = "ioc-" + case
        before = snapshot(recorder, prefix + "-before", signer.address)
        book_result = info(
            recorder, prefix + "-book", {"type": "l2Book", "coin": market["name"]}
        )
        book = book_result.json
        if (
            not isinstance(book, dict)
            or not book.get("levels")
            or not all(book["levels"])
        ):
            raise RuntimeError("Two-sided depth required to construct bounded IOC")
        bids, asks = book["levels"]
        best = decimal((asks if buy else bids)[0]["px"])
        price = price_wire(
            best * (Decimal("1.005") if buy else Decimal("0.995")),
            base["szDecimals"],
            ROUND_UP if buy else ROUND_DOWN,
        )
        if case == "no-fill":
            price = price_wire(
                decimal(bids[0]["px"]) * Decimal("0.5")
                if buy
                else decimal(asks[0]["px"]) * Decimal(2),
                base["szDecimals"],
                ROUND_DOWN if buy else ROUND_UP,
            )
        case_target = min(target, Decimal(1)) if case == "minimum" else target
        size = (case_target / price).quantize(step, rounding=ROUND_DOWN)
        if case == "minimum" and size == 0 and step * price < Decimal(10):
            size = step
        if case == "partial-attempt":
            size = (decimal((asks if buy else bids)[0]["sz"]) + step).quantize(
                step, rounding=ROUND_UP
            )
        if case == "size-precision":
            size = max(Decimal(0), size - step) + step / 10
        if case == "price-precision":
            price += Decimal(1).scaleb(-(9 - base["szDecimals"]))
            size = (case_target / price).quantize(step, rounding=ROUND_DOWN)
        if size <= 0 or price <= 0:
            raise RuntimeError(
                "Notional too small for selected market's size increment"
            )
        notional = price * size
        fees = before["fees"]
        if not isinstance(fees, dict) or "userSpotCrossRate" not in fees:
            raise RuntimeError(
                "Spot taker fee rate unavailable; cannot establish fee headroom"
            )
        taker_rate = decimal(fees["userSpotCrossRate"])
        if taker_rate < 0 or taker_rate > Decimal("0.01"):
            raise RuntimeError(
                "Observed spot taker rate exceeds conservative 1% reservation"
            )
        reservation = notional * Decimal("1.01")
        if notional > target or consumed + reservation > budget:
            raise RuntimeError(
                "IOC notional plus 1% fee reservation exceeds per-order or cumulative bound"
            )
        consumed += reservation
        source_index = quote["index"] if buy else base["index"]
        required = notional if buy else size
        # Reserve 1% source-token headroom rather than assuming fees always debit the received token.
        if free_balance(before["balances"], source_index) < required * Decimal("1.01"):
            raise RuntimeError(
                "Insufficient owned source balance including conservative 1% fee headroom"
            )
        nonce = reserve_nonce(
            args.nonce_file, signer.address, recorder.base_url, prefix
        )
        cloid = (
            "0x"
            + hashlib.sha256(f"{signer.address}:{nonce}:{prefix}".encode()).hexdigest()[
                :32
            ]
        )
        order = {
            "a": 10000 + market["index"],
            "b": buy,
            "p": wire(price),
            "s": wire(size),
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
            "c": cloid,
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
        description = {
            "signer": signer.address.lower(),
            "scenario": case,
            "coin": market["name"],
            "cloid": cloid,
            "limit_notional": wire(notional),
            "cumulative_reserved_notional": wire(consumed),
            "intended_mutation": "bounded spot IOC; debit owned source token if matched",
            "setup_requirement": "two-sided book and owned source balance with 1% fee headroom",
            "assumption": "full/no/partial labels describe intent only; live depth and price can change",
        }
        result = submit(recorder, prefix, envelope, description, args.execute)
        if result is not None:
            info(
                recorder,
                prefix + "-status-cloid",
                {"type": "orderStatus", "user": signer.address, "oid": cloid},
            )
            payload = result.json
            statuses = (
                payload.get("response", {}).get("data", {}).get("statuses", [])
                if isinstance(payload, dict)
                and isinstance(payload.get("response"), dict)
                else []
            )
            for index, status in enumerate(statuses):
                if isinstance(status, dict):
                    detail = status.get("filled", status.get("resting", {}))
                    if isinstance(detail, dict) and "oid" in detail:
                        info(
                            recorder,
                            f"{prefix}-status-oid-{index}",
                            {
                                "type": "orderStatus",
                                "user": signer.address,
                                "oid": detail["oid"],
                            },
                        )
            snapshot(recorder, prefix + "-after", signer.address)
    recorder.event(
        {
            "state": "scenario_completed",
            "scenario": "ioc",
            "execute": args.execute,
            "cumulative_reserved_notional": wire(consumed),
        }
    )
