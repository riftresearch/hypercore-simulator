"""Serial, single-attempt late parity discriminators; observations are not conclusions."""

import argparse
import hashlib
import json
from copy import deepcopy
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_l1_action, sign_send_asset_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET, create, encode
from .scenarios import (
    choose_cases,
    free_balance,
    load_meta,
    price_wire,
    reserve_nonce,
    snapshot,
    submit,
    wallet,
    wire,
)

D = Decimal
ORIGINALS = {
    "rounding": Path("captures/testnet/rounding-canonical/rounding-buy"),
    "quote": Path("captures/testnet/quote-activation/buy-quote"),
}
FEE_HYPOTHESES = {
    "quote_fee_units": "floor((qty * px * 10**quoteWeiDecimals) * f64(rate))",
    "buy_base_fee_units": "floor((quote_fee_units / 10**quoteWeiDecimals / px) * 10**baseWeiDecimals)",
    "classification": "hypotheses only; compare actual fills, feeToken, userFees and balance deltas",
}


class CampaignRecorder(Recorder):
    """Stop even inside snapshot() when a pre-transfer read is rate limited."""

    def request(self, case_id, path, body, **kwargs):
        self.last_request = case_id
        result = super().request(case_id, path, body, **kwargs)
        if (
            result.transport_error
            or result.status is None
            or result.status == 429
            or result.status >= 500
        ):
            raise RuntimeError(
                f"{case_id}: transport/rate-limit/server failure; no further requests or retries"
            )
        return result


def checked_info(recorder, case, body):
    result = info(recorder, case, body)
    if result.status != 200 or result.json is None:
        raise RuntimeError(f"{case}: required read unavailable")
    return result.json


def total(state, token):
    return sum(
        (
            D(row["total"])
            for row in state["balances"]["balances"]
            if row["token"] == token
        ),
        D(0),
    )


def originals(recorder):
    loaded = {}
    for label, path in ORIGINALS.items():
        values = {}
        for filename in ("request.body", "response.body", "pending.json"):
            raw = (path / filename).read_bytes()
            create(recorder.root / f"original-{label}-{filename}", raw)
            values[filename] = json.loads(raw)
            recorder.event(
                {
                    "state": "original_evidence_loaded",
                    "source": str(path / filename),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        loaded[label] = values
    return loaded


def identifiers(envelope, response):
    ids = {order["c"] for order in envelope["action"]["orders"] if order.get("c")}
    payload = response.get("response")
    data = payload.get("data") if isinstance(payload, dict) else None
    order_statuses = data.get("statuses", []) if isinstance(data, dict) else []
    for status in order_statuses:
        for name in ("filled", "resting"):
            if name in status:
                ids.add(status[name]["oid"])
                if status[name].get("cloid"):
                    ids.add(status[name]["cloid"])
    return sorted(ids, key=str)


def market(recorder, case, pair, both_sides=True):
    payload = checked_info(
        recorder, case + "-contexts", {"type": "spotMetaAndAssetCtxs"}
    )
    if not isinstance(payload, list) or len(payload) != 2:
        raise RuntimeError("Unexpected context shape")
    live_pair = next(p for p in payload[0]["universe"] if p["index"] == pair["index"])
    if live_pair != pair:
        raise RuntimeError("Market metadata changed during campaign")
    context = next(row for row in payload[1] if row["coin"] == pair["name"])
    book = checked_info(
        recorder, case + "-book", {"type": "l2Book", "coin": pair["name"]}
    )
    if (
        not book.get("levels")
        or (both_sides and not all(book["levels"]))
        or not book["levels"][1]
    ):
        raise RuntimeError("Required market book sides unavailable")
    return context, book


def c_guard(state, purr, book=None):
    quote = free_balance(state["balances"], 0)
    if (
        state["role"] != {"role": "user"}
        or not D(0) <= quote <= total(state, 0) <= 2
        or total(state, purr["index"]) != 0
    ):
        raise RuntimeError(
            "C must already exist, hold <=2USDC total and zero total PURR"
        )
    if total(state, 0) != quote:
        raise RuntimeError("C must have no held USDC")
    if book is not None and D(book["levels"][1][0]["px"]) <= quote:
        raise RuntimeError(
            "C can afford one PURR at captured ask; no-fill guard failed"
        )


def order_wire(pair, buy, price, size):
    return {
        "a": 10000 + pair["index"],
        "b": buy,
        "p": wire(price),
        "s": wire(size),
        "r": False,
        "t": {"limit": {"tif": "Ioc"}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--cases",
        default="recovery,batch,price,fee,decimal",
        help="Ordered unique subset of recovery,batch,price,reference,minimum,minimum-nq,minimum-funded,band-nq,band-test8,valuation,fee,decimal; additional cases are opt-in",
    )
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    parser.add_argument(
        "--band-edges",
        default="upper-equal,upper-outside,lower-equal,lower-outside",
        help="Ordered subset of upper-equal,upper-inside,upper-outside,lower-equal,lower-inside,lower-outside",
    )
    args = parser.parse_args()
    try:
        cases = choose_cases(
            args.cases,
            (
                "recovery",
                "batch",
                "price",
                "reference",
                "minimum",
                "minimum-nq",
                "minimum-funded",
                "band-nq",
                "band-test8",
                "valuation",
                "fee",
                "decimal",
            ),
        )
        band_edges = choose_cases(
            args.band_edges,
            (
                "upper-equal",
                "upper-inside",
                "upper-outside",
                "lower-equal",
                "lower-inside",
                "lower-outside",
            ),
        )
    except ValueError as exc:
        parser.error(str(exc))
    if any(case != "recovery" for case in cases) and not args.execute:
        parser.error("--execute required for signed cases; recovery alone is read-only")
    recorder = CampaignRecorder(args.root, TESTNET)
    report = {
        "scenario": "late-contract",
        "cases": cases,
        "completed_cases": [],
        "actions": [],
        "automatic_retry": False,
        "bounds": {
            "fee_buy_limit_notional": "15",
            "fee_sell_captured_limit_notional": "30",
            "decimal_forward": "1.1000001",
            "decimal_return": "observed credited delta only, <= each forward cap",
        },
        "fee_hypotheses": FEE_HYPOTHESES,
    }
    active = "setup"
    try:
        source = originals(recorder)
        a_address = source["rounding"]["pending.json"]["parameters"]["signer"].lower()
        if source["quote"]["pending.json"]["parameters"]["signer"].lower() != a_address:
            raise RuntimeError("Original controlled A identities disagree")
        a = (
            wallet("HYPERLIQUID_PRIVATE_KEY")
            if any(
                c in cases for c in ("fee", "decimal", "valuation", "minimum-funded")
            )
            else None
        )
        b = (
            wallet("HYPERLIQUID_B_PRIVATE_KEY")
            if any(
                case in cases
                for case in (
                    "valuation",
                    "minimum-nq",
                    "minimum-funded",
                    "band-nq",
                    "band-test8",
                )
            )
            else None
        )
        c = (
            wallet("HYPERLIQUID_C_PRIVATE_KEY")
            if any(
                case in cases
                for case in ("batch", "price", "reference", "minimum", "decimal")
            )
            else None
        )
        if a is not None and a.address.lower() != a_address:
            raise RuntimeError(
                "A signer differs from original controlled inventory owner"
            )
        if c is not None and c.address.lower() == a_address:
            raise RuntimeError("A and C must be distinct")
        if b is not None and b.address.lower() == a_address:
            raise RuntimeError("A and B must be distinct")
        meta = load_meta(recorder)
        tokens = {row["index"]: row for row in meta["tokens"]}
        purr = next(row for row in tokens.values() if row["name"] == "PURR")
        pairs = {row["index"]: row for row in meta["universe"]}
        purr_pair = next(
            row for row in pairs.values() if row["tokens"] == [purr["index"], 0]
        )
        if purr["szDecimals"] != 0:
            raise RuntimeError("Discriminators require whole-PURR lots")

        def statuses(case, user, envelope, response):
            observed = []
            for index, oid in enumerate(identifiers(envelope, response)):
                observed.append(
                    {
                        "oid_or_cloid": oid,
                        "response": checked_info(
                            recorder,
                            f"{case}-status-{index}",
                            {"type": "orderStatus", "user": user, "oid": oid},
                        ),
                    }
                )
            return observed

        def order(case, signer, pair, build):
            before = snapshot(recorder, case + "-before", signer.address)
            context, book = market(
                recorder, case, pair, both_sides=not active.startswith("band-")
            )
            candidate = build(before, context, book)
            if candidate is None:
                recorder.event(
                    {
                        "state": "precondition_unavailable",
                        "case": case,
                        "reason": "No distinct legal midpoint between fresh candidate reference thresholds",
                    }
                )
                return None
            orders, details = candidate
            nonce = reserve_nonce(args.nonce_file, signer.address, TESTNET, case)
            copied = deepcopy(orders)
            for index, item in enumerate(copied):
                item["c"] = "0x" + f"{nonce << 32 | index:032x}"
            action = {"type": "order", "orders": copied, "grouping": "na"}
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": sign_l1_action(signer, action, None, nonce, None, False),
                "vaultAddress": None,
                "expiresAfter": None,
            }
            row = {
                "case": case,
                "state": "submission_pending",
                "cloids": [item["c"] for item in copied],
                **details,
            }
            report["actions"].append(row)
            result = submit(
                recorder,
                case,
                envelope,
                {
                    "signer": signer.address,
                    "intended_mutation": "bounded IOC; admission and execution are observations, not assumptions",
                    "source_contexts": case + "-contexts",
                    "source_book": case + "-book",
                    **details,
                },
                True,
            )
            row.update(
                state="response_observed",
                http_status=result.status,
                response=result.json,
            )
            if active in (
                "reference",
                "minimum",
                "minimum-nq",
                "minimum-funded",
                "band-nq",
                "band-test8",
            ):
                post_context, post_book = market(
                    recorder,
                    case + "-post",
                    pair,
                    both_sides=not active.startswith("band-"),
                )
                row["reference_after"] = {
                    "markPx": post_context["markPx"],
                    "midPx": post_context["midPx"],
                    "book_time": post_book["time"],
                }
            after = snapshot(recorder, case + "-after", signer.address)
            row["statuses"] = statuses(
                case,
                signer.address,
                envelope,
                result.json if isinstance(result.json, dict) else {},
            )
            row["state"] = "observed"
            return before, after, row

        for active in cases:
            if active == "recovery":
                snapshot(recorder, "recovery-a", a_address)
                report["recovery"] = {}
                for label, values in source.items():
                    report["recovery"][label] = statuses(
                        "recovery-" + label,
                        a_address,
                        values["request.body"],
                        values["response.body"],
                    )
                for pair in (purr_pair, pairs[49], pairs[1302]):
                    market(recorder, "recovery-market-" + str(pair["index"]), pair)
            elif active == "batch":
                for label, variant in (
                    ("valid-first-invalid-last", 0),
                    ("invalid-first-valid-last", 1),
                    ("two-runtime-rejects", 2),
                ):

                    def build_batch(state, context, book, variant=variant):
                        c_guard(state, purr, book)
                        ask = D(book["levels"][1][0]["px"])
                        references = [D(context["markPx"]), D(context["midPx"]), ask]
                        if not D(4) < ask <= D(5) or any(
                            not D("0.2") * ref <= D(4) <= D(5) <= D("1.8") * ref
                            for ref in references
                        ):
                            raise RuntimeError(
                                "Batch anchors must be in all candidate bands, 5 crossing and 4 noncrossing"
                            )
                        good = order_wire(purr_pair, True, D(5), D(3))
                        bad = {**good, "s": "3.1"}
                        orders = (
                            [good, bad]
                            if variant == 0
                            else [bad, good]
                            if variant == 1
                            else [good, {**good, "p": "4"}]
                        )
                        return orders, {
                            "hypothesis": "all-batch prevalidation versus sequential per-order runtime rejection",
                            "no_fill_guard": "C total USDC <=2 and below captured price of one whole PURR",
                        }

                    order("batch-" + label, c, purr_pair, build_batch)
            elif active == "price":
                for reference in ("mark", "mid", "ask"):
                    for side in ("below", "above"):

                        def build_price(
                            state, context, book, reference=reference, side=side
                        ):
                            c_guard(state, purr, book)
                            ask = D(book["levels"][1][0]["px"])
                            ref = {
                                "mark": D(context["markPx"]),
                                "mid": D(context["midPx"]),
                                "ask": ask,
                            }[reference]
                            threshold = D("1.8") * ref
                            rounding = ROUND_FLOOR if side == "below" else ROUND_CEILING
                            px = price_wire(threshold, purr["szDecimals"], rounding)
                            if px == threshold:
                                quantum = D(1).scaleb(
                                    max(
                                        -(8 - purr["szDecimals"]),
                                        threshold.adjusted() - 4,
                                    )
                                )
                                px = price_wire(
                                    px + (-quantum if side == "below" else quantum),
                                    purr["szDecimals"],
                                    rounding,
                                )
                            if (
                                px <= ask
                                or px * 2 < 10
                                or len(px.normalize().as_tuple().digits) > 5
                            ):
                                raise RuntimeError(
                                    "Price candidate fails crossing/minimum/significant-digit guard"
                                )
                            return [order_wire(purr_pair, True, px, D(2))], {
                                "reference": reference,
                                "reference_price": wire(ref),
                                "threshold_1_8": wire(threshold),
                                "side": side,
                                "no_fill_guard": "C cannot afford one whole PURR at captured ask",
                            }

                        order(f"price-{reference}-{side}", c, purr_pair, build_price)

                def build_lower(state, context, book):
                    c_guard(state, purr, book)
                    if any(
                        D("0.1") >= D("0.2") * value
                        for value in (
                            D(context["markPx"]),
                            D(context["midPx"]),
                            D(book["levels"][1][0]["px"]),
                        )
                    ):
                        raise RuntimeError(
                            "0.1 is not below every candidate lower band"
                        )
                    return [order_wire(purr_pair, True, D("0.1"), D(100))], {
                        "hypothesis": "below-lower-band 80-percent error precedence"
                    }

                order("price-below-lower-band", c, purr_pair, build_lower)
            elif active in ("reference", "minimum"):
                hypothesis = "Minimum may use min(limit, reference), reference alone, or a side-dependent cap; candidate reference and price-band methods are hypotheses, not conclusions"
                fixed = (
                    (
                        ("buy-three-100", True, D(3), D(100)),
                        ("sell-three-100", False, D(3), D(100)),
                        ("minimum-buy-two-5", True, D(2), D(5)),
                        ("minimum-buy-three-3_4", True, D(3), D("3.4")),
                        ("minimum-sell-two-100", False, D(2), D(100)),
                    )
                    if active == "reference"
                    else (
                        ("minimum-buy-three-3", True, D(3), D(3)),
                        ("minimum-sell-three-3", False, D(3), D(3)),
                        ("minimum-sell-two-6", False, D(2), D(6)),
                        ("minimum-buy-two-6", True, D(2), D(6)),
                    )
                )
                for label, buy, qty, px in fixed:

                    def build_fixed(
                        state,
                        context,
                        book,
                        buy=buy,
                        qty=qty,
                        px=px,
                        hypothesis=hypothesis,
                    ):
                        c_guard(state, purr, book)
                        return [order_wire(purr_pair, buy, px, qty)], {
                            "hypothesis": hypothesis,
                            "limit_notional": wire(px * qty),
                            "no_fill_guard": "C has zero PURR and cannot afford one whole PURR at the captured ask; existing quote total <=2USDC",
                        }

                    order("reference-" + label, c, purr_pair, build_fixed)
                bands = (
                    (("upper", D(5)), ("lower", D("0.2")))
                    if active == "reference"
                    else ()
                )
                for band, factor in bands:
                    for left, right in (
                        ("mark", "mid"),
                        ("mark", "ask"),
                        ("mid", "ask"),
                    ):

                        def build_interval(
                            state,
                            context,
                            book,
                            band=band,
                            factor=factor,
                            left=left,
                            right=right,
                            hypothesis=hypothesis,
                        ):
                            c_guard(state, purr, book)
                            ask = D(book["levels"][1][0]["px"])
                            refs = {
                                "mark": D(context["markPx"]),
                                "mid": D(context["midPx"]),
                                "ask": ask,
                            }
                            if any(value <= 0 for value in refs.values()):
                                raise RuntimeError(
                                    "Positive mark, mid and ask required"
                                )
                            low, high = sorted(
                                (factor * refs[left], factor * refs[right])
                            )
                            px = price_wire(
                                (low + high) / 2, purr["szDecimals"], ROUND_FLOOR
                            )
                            if not low < px < high:
                                return None
                            if len(px.normalize().as_tuple().digits) > 5 or (
                                band == "lower" and px >= ask
                            ):
                                raise RuntimeError(
                                    "Reference midpoint fails canonical/noncrossing guard"
                                )
                            qty = (
                                D(3)
                                if band == "upper"
                                else (D(12) / px).to_integral_value(
                                    rounding=ROUND_CEILING
                                )
                            )
                            return [order_wire(purr_pair, True, px, qty)], {
                                "hypothesis": hypothesis,
                                "band": band,
                                "factor": wire(factor),
                                "between": [left, right],
                                "reference_prices": {
                                    name: wire(value) for name, value in refs.items()
                                },
                                "threshold_interval": [wire(low), wire(high)],
                                "limit_notional": wire(px * qty),
                                "no_fill_guard": "C cannot afford one captured whole-PURR lot; lower probe is also noncrossing",
                            }

                        order(
                            f"reference-{band}-{left}-{right}",
                            c,
                            purr_pair,
                            build_interval,
                        )
            elif active in ("minimum-nq", "minimum-funded", "band-nq", "band-test8"):
                pair = pairs[36 if active == "band-test8" else 1302]
                token = tokens[pair["tokens"][0]]
                if (
                    token["name"] != ("TEST8" if active == "band-test8" else "NQ")
                    or pair["tokens"][1] != 0
                ):
                    raise RuntimeError("Discriminator market identity changed")
                seed = D("0.00000001") if active == "minimum-funded" else D(0)

                def seed_transfer(case, sender, destination, seed=seed):
                    nonce = reserve_nonce(
                        args.nonce_file, sender.address, TESTNET, case
                    )
                    action = {
                        "type": "sendAsset",
                        "destination": destination.address.lower(),
                        "sourceDex": "spot",
                        "destinationDex": "spot",
                        "token": "USDC:" + tokens[0]["tokenId"],
                        "amount": wire(seed),
                        "fromSubAccount": "",
                        "nonce": nonce,
                    }
                    signature = sign_send_asset_action(sender, action, False)
                    envelope = {
                        "action": action,
                        "nonce": nonce,
                        "signature": signature,
                        "vaultAddress": None,
                    }
                    result = submit(
                        recorder,
                        case,
                        envelope,
                        {
                            "signer": sender.address,
                            "intended_mutation": "one USDC wei between existing controlled accounts",
                            "amount_cap": wire(seed),
                        },
                        True,
                    )
                    if result.json != {"status": "ok", "response": {"type": "default"}}:
                        raise RuntimeError(
                            "One-wei transfer failed; halt without cleanup or retry"
                        )

                if seed:
                    initial_a = snapshot(
                        recorder, "minimum-funded-initial-a", a.address
                    )
                    initial_b = snapshot(
                        recorder, "minimum-funded-initial-b", b.address
                    )
                    if (
                        free_balance(initial_a["balances"], 0) < seed
                        or initial_b["role"] != {"role": "user"}
                        or total(initial_b, 0) != 0
                    ):
                        raise RuntimeError(
                            "One-wei experiment requires funded A and existing B with zero USDC"
                        )
                    seed_transfer("minimum-funded-seed", a, b)
                    report["bounds"]["minimum_seed"] = wire(seed)
                nq_cases = (
                    ("cross-buy-mark-below-ask-above", True, D("9.77"), D("1.1")),
                    ("cross-sell-bid-below-ask-above", False, D("9.79"), D("0.9")),
                    ("noncross-buy-below-minimum", True, D(1), D("0.9")),
                    ("noncross-sell-below-minimum", False, D(1), D("1.1")),
                )
                if active == "minimum-funded":
                    nq_cases = tuple(
                        ("funded-buy-" + qty, True, D(qty), D("1.1"))
                        for qty in ("9.77", "9.76", "9.7", "1")
                    )
                elif active.startswith("band-"):
                    nq_cases = tuple(
                        ("band-" + edge, True, D(20), None) for edge in band_edges
                    )
                for label, buy, qty, px in nq_cases:

                    def build_nq(
                        state,
                        context,
                        book,
                        buy=buy,
                        qty=qty,
                        px=px,
                        label=label,
                        seed=seed,
                        token=token,
                        pair=pair,
                    ):
                        if (
                            state["role"] != {"role": "user"}
                            or total(state, 0) != seed
                            or total(state, token["index"]) != 0
                        ):
                            raise RuntimeError(
                                f"B must have exactly the bounded USDC seed and zero {token['name']}; no fill is authorized"
                            )
                        if px is None:
                            threshold = D(context["markPx"]) * (
                                D(5) if "upper" in label else D("0.2")
                            )
                            px = price_wire(threshold, token["szDecimals"], ROUND_FLOOR)
                            if px != threshold:
                                return None
                            if "outside" in label or "inside" in label:
                                tick = D(1).scaleb(px.adjusted() - 4)
                                outward = tick if "upper" in label else -tick
                                px += outward if "outside" in label else -outward
                        if (
                            D(book["levels"][1][0]["px"])
                            * D(1).scaleb(-token["szDecimals"])
                            <= seed
                        ):
                            raise RuntimeError(
                                "B could afford one base lot; no-fill guard failed"
                            )
                        opposite = D(book["levels"][1 if buy else 0][0]["px"])
                        return [order_wire(pair, buy, px, qty)], {
                            "hypothesis": "Distinguish crossing, execution-price and mark-price minimum precedence",
                            "limit_notional": wire(px * qty),
                            "mark_notional": wire(D(context["markPx"]) * qty),
                            "opposite_notional": wire(opposite * qty),
                            "no_fill_guard": f"B has zero {token['name']} and at most one USDC wei, less than one base lot",
                        }

                    order(active + "-" + label, b, pair, build_nq)
                if seed:
                    end_b = snapshot(
                        recorder, "minimum-funded-return-before-b", b.address
                    )
                    if (
                        free_balance(end_b["balances"], 0) != seed
                        or total(end_b, token["index"]) != 0
                    ):
                        raise RuntimeError(
                            "One-wei experiment state did not reconcile; do not clean up"
                        )
                    seed_transfer("minimum-funded-return", b, a)
                    snapshot(recorder, "minimum-funded-final-a", a.address)
                    snapshot(recorder, "minimum-funded-final-b", b.address)
            elif active == "valuation":
                nq_pair = pairs[1302]
                nq = tokens[nq_pair["tokens"][0]]
                if nq["name"] != "NQ" or nq_pair["tokens"][1] != 0:
                    raise RuntimeError("Valuation requires @1302=NQ/USDC")
                initial_a = snapshot(recorder, "valuation-initial-a", a.address)
                initial_b = snapshot(recorder, "valuation-initial-b", b.address)
                if any(
                    state["role"] != {"role": "user"}
                    for state in (initial_a, initial_b)
                ):
                    raise RuntimeError(
                        "Valuation requires existing A and B; no activation funding"
                    )
                if (
                    free_balance(initial_a["balances"], purr["index"]) < 3
                    or free_balance(initial_a["balances"], nq["index"]) < 1
                ):
                    raise RuntimeError(
                        "Valuation requires A>=3 free PURR and >=1 free NQ"
                    )
                if any(total(initial_b, token["index"]) != 0 for token in (purr, nq)):
                    raise RuntimeError(
                        "Valuation requires B initially zero total PURR and NQ"
                    )
                for label, token, pair in (
                    ("purr", purr, purr_pair),
                    ("nq", nq, nq_pair),
                ):
                    for direction, signer, recipient in (
                        ("forward", a, b),
                        ("return", b, a),
                    ):
                        case = f"valuation-{label}-{direction}"
                        before_a = snapshot(recorder, case + "-before-a", a.address)
                        before_b = snapshot(recorder, case + "-before-b", b.address)
                        if any(
                            state["role"] != {"role": "user"}
                            for state in (before_a, before_b)
                        ):
                            raise RuntimeError(
                                "Valuation accounts must remain existing users"
                            )
                        source_state = before_a if direction == "forward" else before_b
                        if free_balance(source_state["balances"], token["index"]) < 1:
                            raise RuntimeError(
                                "Exact one-token send lacks observed free inventory"
                            )
                        if direction == "forward" and (
                            total(before_b, token["index"]) != 0
                            or (
                                label == "purr"
                                and free_balance(before_a["balances"], token["index"])
                                < 3
                            )
                        ):
                            raise RuntimeError(
                                "Valuation forward inventory guard changed"
                            )
                        if (
                            direction == "return"
                            and total(before_b, token["index"]) != 1
                        ):
                            raise RuntimeError(
                                "B must hold exactly the observed one-token credit before return"
                            )
                        nonce = reserve_nonce(
                            args.nonce_file, signer.address, TESTNET, case
                        )
                        action = {
                            "type": "sendAsset",
                            "destination": recipient.address.lower(),
                            "sourceDex": "spot",
                            "destinationDex": "spot",
                            "token": token["name"] + ":" + token["tokenId"],
                            "amount": "1",
                            "fromSubAccount": "",
                            "nonce": nonce,
                        }
                        envelope = {
                            "action": action,
                            "nonce": nonce,
                            "signature": sign_send_asset_action(signer, action, False),
                            "vaultAddress": None,
                            "expiresAfter": None,
                        }
                        row = {
                            "case": case,
                            "state": "submission_pending",
                            "amount": "1",
                            "token": token["name"],
                            "nonce": nonce,
                            "hypothesis": "Compare transfer entryNtl against independently captured mark/mid/book; no valuation method assumed",
                        }
                        report["actions"].append(row)
                        # Last network reads before each send: independent context and book, never derived from its result.
                        market(recorder, case, pair)
                        result = submit(
                            recorder,
                            case,
                            envelope,
                            {
                                "signer": signer.address,
                                "intended_mutation": "exactly one controlled token to existing account; no activation",
                                "source_contexts": case + "-contexts",
                                "source_book": case + "-book",
                                "amount_cap": "1",
                            },
                            True,
                        )
                        row.update(
                            state="response_observed",
                            response=result.json,
                            http_status=result.status,
                        )
                        after_a = snapshot(recorder, case + "-after-a", a.address)
                        after_b = snapshot(recorder, case + "-after-b", b.address)
                        delta_a = total(after_a, token["index"]) - total(
                            before_a, token["index"]
                        )
                        delta_b = total(after_b, token["index"]) - total(
                            before_b, token["index"]
                        )
                        row.update(
                            state="observed",
                            delta_a=wire(delta_a),
                            delta_b=wire(delta_b),
                        )
                        expected_a = D(-1) if direction == "forward" else D(1)
                        if (
                            result.json
                            != {"status": "ok", "response": {"type": "default"}}
                            or delta_a != expected_a
                            or delta_b != -expected_a
                        ):
                            raise RuntimeError(
                                "Exact valuation transfer did not reconcile; halt without retry or cleanup"
                            )
            elif active == "fee":
                for label, pair, buy in (
                    ("nq-buy", pairs[1302], True),
                    ("nq-sell", pairs[1302], False),
                    ("test11-sell", pairs[49], False),
                ):
                    token = tokens[pair["tokens"][0]]
                    if pair["tokens"][1] != 0 or token["name"] != (
                        "TEST11" if pair["index"] == 49 else "NQ"
                    ):
                        raise RuntimeError("Controlled fee market identity changed")

                    def build_fee(
                        state, context, book, pair=pair, token=token, buy=buy
                    ):
                        if state["role"] != {"role": "user"}:
                            raise RuntimeError("A must be an existing user")
                        px = D(book["levels"][1 if buy else 0][0]["px"])
                        if price_wire(px, token["szDecimals"], ROUND_FLOOR) != px:
                            raise RuntimeError(
                                "Observed best price is not a legal wire price"
                            )
                        qty = D(10)
                        if pair["index"] == 49:
                            lot = D(1).scaleb(-token["szDecimals"])
                            qty = (D(10) / px).quantize(lot, rounding=ROUND_CEILING)
                            original_filled = source["rounding"]["response.body"][
                                "response"
                            ]["data"]["statuses"][0]["filled"]
                            if qty > D(original_filled["totalSz"]):
                                raise RuntimeError(
                                    "TEST11 sell exceeds original controlled purchase quantity"
                                )
                        notional = qty * px
                        if not D(10) <= notional <= D(15):
                            raise RuntimeError("Fee candidate outside10-15USDC")
                        if buy and free_balance(state["balances"], 0) < 15:
                            raise RuntimeError("A lacks bounded purchase funding")
                        if (
                            not buy
                            and free_balance(state["balances"], token["index"]) < qty
                        ):
                            raise RuntimeError(
                                "Insufficient controlled base inventory; do not assume prior buy filled"
                            )
                        return [order_wire(pair, buy, px, qty)], {
                            "fee_hypotheses": FEE_HYPOTHESES,
                            "base_wei_decimals": token["weiDecimals"],
                            "quote_wei_decimals": tokens[0]["weiDecimals"],
                            "captured_limit_notional": wire(notional),
                            "sell_price_improvement_can_increase_gross": not buy,
                        }

                    _, after, row = order("fee-" + label, a, pair, build_fee)
                    cloids = set(row["cloids"])
                    oids = {
                        item["oid_or_cloid"]
                        for item in row["statuses"]
                        if isinstance(item["oid_or_cloid"], int)
                    }
                    fills = [
                        fill
                        for fill in after["fills"]
                        if fill.get("cloid") in cloids or fill.get("oid") in oids
                    ]
                    gross = sum((D(fill["sz"]) * D(fill["px"]) for fill in fills), D(0))
                    row.update(
                        observed_fills=fills,
                        observed_fill_gross=wire(gross),
                        fills_may_be_endpoint_limited=True,
                    )
                    if gross > 15:
                        raise RuntimeError(
                            "Observed fee fill gross exceeds15USDC; halt, retain price-improvement evidence"
                        )
            elif active == "decimal":
                usdc = tokens[0]
                if usdc["name"] != "USDC":
                    raise RuntimeError("Quote identity mismatch")

                def send(case, signer, destination, raw, cap, usdc=usdc):
                    nonce = reserve_nonce(
                        args.nonce_file, signer.address, TESTNET, case
                    )
                    action = {
                        "type": "sendAsset",
                        "destination": destination.lower(),
                        "sourceDex": "spot",
                        "destinationDex": "spot",
                        "token": usdc["name"] + ":" + usdc["tokenId"],
                        "amount": raw,
                        "fromSubAccount": "",
                        "nonce": nonce,
                    }
                    envelope = {
                        "action": action,
                        "nonce": nonce,
                        "signature": sign_send_asset_action(signer, action, False),
                        "vaultAddress": None,
                        "expiresAfter": None,
                    }
                    row = {
                        "case": case,
                        "state": "submission_pending",
                        "declared_cap": wire(cap),
                        "raw_amount": raw,
                    }
                    report["actions"].append(row)
                    result = submit(
                        recorder,
                        case,
                        envelope,
                        {
                            "signer": signer.address,
                            "intended_mutation": "bounded parser probe or observed-delta-only return",
                            "declared_cap": wire(cap),
                        },
                        True,
                    )
                    row.update(
                        state="response_observed",
                        response=result.json,
                        http_status=result.status,
                    )
                    return result, row

                for label, raw, cap in (
                    ("scientific", "0.00000000000000000000000000011e28", D("1.1")),
                    ("underscore", "0.000_000_1", D("0.0000001")),
                ):
                    prefix = "decimal-" + label
                    before_c = snapshot(recorder, prefix + "-before-c", c.address)
                    before_a = snapshot(recorder, prefix + "-before-a", a.address)
                    c_guard(before_c, purr)
                    if (
                        before_a["role"] != {"role": "user"}
                        or free_balance(before_c["balances"], 0) < cap
                    ):
                        raise RuntimeError(
                            "Decimal guard requires existing A and sufficient controlled C funds"
                        )
                    result, row = send(prefix, c, a.address, raw, cap)
                    after_c = snapshot(recorder, prefix + "-after-c", c.address)
                    after_a = snapshot(recorder, prefix + "-after-a", a.address)
                    credited = total(after_a, 0) - total(before_a, 0)
                    debited = total(before_c, 0) - total(after_c, 0)
                    row.update(
                        observed_credit_a=wire(credited),
                        observed_debit_c=wire(debited),
                        state="observed",
                    )
                    if not D(0) <= credited <= cap or not D(0) <= debited <= cap:
                        raise RuntimeError(
                            "Observed decimal delta outside declared cap; no blind balance return"
                        )
                    accepted = result.json == {
                        "status": "ok",
                        "response": {"type": "default"},
                    }
                    rejected = (
                        isinstance(result.json, dict)
                        and result.json.get("status") == "err"
                    )
                    rejected = rejected or result.status in (400, 422)
                    if not accepted:
                        if not rejected or credited or debited:
                            raise RuntimeError(
                                "Unclassified decimal outcome or rejection with balance movement; halt"
                            )
                        row["return"] = (
                            "none: explicit rejection and zero observed movement"
                        )
                        continue
                    if not credited:
                        row["return"] = (
                            "none: zero observed credit; never assume amount"
                        )
                        raise RuntimeError(
                            "Accepted decimal send has no observed credit; stop for read-only recovery"
                        )
                    if (
                        credited > D("1.1")
                        or free_balance(after_a["balances"], 0) < credited
                    ):
                        raise RuntimeError(
                            "Observed credited amount is not safely returnable"
                        )
                    returned, return_row = send(
                        prefix + "-return", a, c.address, wire(credited), credited
                    )
                    final_a = snapshot(recorder, prefix + "-returned-a", a.address)
                    final_c = snapshot(recorder, prefix + "-returned-c", c.address)
                    return_row.update(
                        state="observed",
                        observed_debit_a=wire(total(after_a, 0) - total(final_a, 0)),
                        observed_credit_c=wire(total(final_c, 0) - total(after_c, 0)),
                    )
                    if (
                        returned.json
                        != {"status": "ok", "response": {"type": "default"}}
                        or total(final_c, 0) - total(after_c, 0) != credited
                        or total(after_a, 0) - total(final_a, 0) != credited
                    ):
                        raise RuntimeError(
                            "Observed-delta return did not reconcile; stop without retry"
                        )
            report["completed_cases"].append(active)
        report["state"] = "completed"
        create(recorder.root / "results.json", encode(report))
        recorder.event({"state": "scenario_completed", "scenario": "late-contract"})
    except (Exception, KeyboardInterrupt) as exc:
        report.update(
            state="partial_abort",
            active_case=active,
            last_request=getattr(recorder, "last_request", None),
            reason=str(exc),
            recovery="Inspect immutable captures; no retry, replay or automatic cleanup. Resolve pending actions with separately authorized read-only queries.",
        )
        create(recorder.root / "partial-abort.json", encode(report))
        recorder.event(
            {
                "state": "run_aborted",
                "reason": str(exc),
                "active_case": active,
                "automatic_retry": False,
            }
        )
        raise


if __name__ == "__main__":
    main()
