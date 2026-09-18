"""One-shot signed IOC evidence campaigns; no retries and no inferred outcomes."""

from __future__ import annotations

import argparse
import hashlib
from copy import deepcopy
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_l1_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET
from .scenarios import (
    choose_cases,
    decimal,
    free_balance,
    load_meta,
    price_wire,
    reserve_nonce,
    snapshot,
    submit,
    wallet,
    wire,
)

MINIMUM = Decimal(10)
MAXIMUM = Decimal(30)
HEADROOM = Decimal("1.01")


class Campaign:
    def __init__(self, recorder, args):
        self.recorder = recorder
        self.args = args
        self.signer = wallet(args.key_env)
        self.budget = decimal(args.max_spend)
        self.reserved = Decimal(0)
        self.known_oids = []
        self.meta = load_meta(recorder)
        self.tokens = {token["index"]: token for token in self.meta["tokens"]}
        self.quote_tokens = {market["tokens"][1] for market in self.meta["universe"]}
        self.markets = [
            market
            for market in sorted(self.meta["universe"], key=lambda item: item["index"])
            if self.tokens[market["tokens"][1]]["name"] == "USDC"
        ]
        if not self.markets:
            raise ValueError("Captured metadata contains no USDC-quoted spot market")
        if args.coin and not any(m["name"] == args.coin for m in self.markets):
            raise ValueError("--coin must name a captured USDC-quoted spot market")

    def unavailable(self, case, reason, **details):
        self.recorder.event(
            {
                "state": "precondition_unavailable",
                "case": case,
                "reason": reason,
                **details,
            }
        )
        print(f"{case}: unavailable precondition: {reason}", flush=True)

    def state(self, prefix):
        return snapshot(self.recorder, prefix, self.signer.address)

    def market(self):
        return next(
            market
            for market in self.markets
            if not self.args.coin or market["name"] == self.args.coin
        )

    def scan_markets(self):
        if self.args.coin:
            return [self.market()]
        return self.markets[
            self.args.market_offset : self.args.market_offset + self.args.market_count
        ]

    def book(self, prefix, market):
        result = info(self.recorder, prefix, {"type": "l2Book", "coin": market["name"]})
        if result.transport_error or result.status != 200:
            raise RuntimeError("Book request failed; halted without retry")
        book = result.json
        if book is None:
            return None
        if (
            not isinstance(book, dict)
            or not isinstance(book.get("levels"), list)
            or len(book["levels"]) != 2
        ):
            raise RuntimeError(
                "Unexpected book shape; cannot establish spending bounds"
            )
        for side in book["levels"]:
            if not isinstance(side, list):
                raise TypeError("Unexpected book side shape")
            for level in side:
                if decimal(level["px"]) <= 0 or decimal(level["sz"]) <= 0:
                    raise RuntimeError("Nonpositive book level; refusing candidate")
        return book

    def step(self, market):
        return Decimal(1).scaleb(-self.tokens[market["tokens"][0]]["szDecimals"])

    def rate(self, state):
        rate = decimal(state["fees"]["userSpotCrossRate"])
        if not Decimal(0) <= rate <= Decimal("0.01"):
            raise RuntimeError("Observed fee rate is outside the 1% source reserve")
        return rate

    def order(self, market, price, size, buy=True):
        return {
            "a": 10000 + market["index"],
            "b": buy,
            "p": wire(price),
            "s": wire(size),
            "r": False,
            "t": {"limit": {"tif": "Ioc"}},
        }

    def notional(self, order):
        value = decimal(order["p"]) * decimal(order["s"])
        if not MINIMUM <= value <= MAXIMUM:
            raise ValueError(
                f"Every candidate order must have limit notional between {MINIMUM} and {MAXIMUM} USDC"
            )
        return value

    def can_reserve(self, case, orders, repetitions=1):
        amount = (
            sum((self.notional(order) for order in orders), Decimal(0))
            * HEADROOM
            * repetitions
        )
        if self.reserved + amount > self.budget:
            self.unavailable(
                case,
                "Conservative cumulative budget cannot cover this complete slice",
                required_reservation=wire(amount),
                cumulative_reserved_notional=wire(self.reserved),
                max_spend=wire(self.budget),
            )
            return False
        return True

    def observe(self, prefix, orders, result):
        after = self.state(prefix + "-after")
        if result is not None:
            payload = result.json
            if isinstance(payload, dict) and isinstance(payload.get("response"), dict):
                data = payload["response"].get("data")
                statuses = data.get("statuses", []) if isinstance(data, dict) else []
                for status in statuses:
                    if not isinstance(status, dict):
                        continue
                    for detail in status.values():
                        if (
                            isinstance(detail, dict)
                            and "oid" in detail
                            and detail["oid"] not in self.known_oids
                        ):
                            self.known_oids.append(detail["oid"])
        for index, cloid in enumerate(dict.fromkeys(order["c"] for order in orders)):
            response = info(
                self.recorder,
                f"{prefix}-status-cloid-{index}",
                {"type": "orderStatus", "user": self.signer.address, "oid": cloid},
            )
            if response.transport_error or response.status != 200:
                raise RuntimeError("Cloid status request failed; halted without retry")
            payload = response.json
            if isinstance(payload, dict) and isinstance(payload.get("order"), dict):
                stored = payload["order"].get("order")
                if (
                    isinstance(stored, dict)
                    and "oid" in stored
                    and stored["oid"] not in self.known_oids
                ):
                    self.known_oids.append(stored["oid"])
        for index, oid in enumerate(self.known_oids):
            response = info(
                self.recorder,
                f"{prefix}-status-oid-{index}",
                {"type": "orderStatus", "user": self.signer.address, "oid": oid},
            )
            if response.transport_error or response.status != 200:
                raise RuntimeError(
                    "Order-ID status request failed; halted without retry"
                )
        return after

    def send(self, prefix, market, orders, *, grouping="na", details=None, replay=None):
        # Guard every attempt independently, including malformed orders and signed replays.
        orders = deepcopy(orders)
        before = self.state(prefix + "-before")
        self.rate(before)
        principal = sum((self.notional(order) for order in orders), Decimal(0))
        reservation = principal * HEADROOM
        if self.reserved + reservation > self.budget:
            raise RuntimeError(
                "Cumulative potential quote notional plus1% exceeds --max-spend"
            )
        required = {}
        for order in orders:
            token = market["tokens"][1 if order["b"] else 0]
            amount = self.notional(order) if order["b"] else decimal(order["s"])
            required[token] = required.get(token, Decimal(0)) + amount * HEADROOM
        for token, amount in required.items():
            if free_balance(before["balances"], token) < amount:
                raise RuntimeError(
                    "Observed free source-token balance cannot cover the entire action plus1%"
                )
        if replay is None:
            nonce = reserve_nonce(
                self.args.nonce_file,
                self.signer.address,
                self.recorder.base_url,
                prefix,
            )
            for index, order in enumerate(orders):
                order.setdefault(
                    "c",
                    "0x"
                    + hashlib.sha256(
                        f"{self.signer.address}:{nonce}:{prefix}:{index}".encode()
                    ).hexdigest()[:32],
                )
            action = {"type": "order", "orders": orders, "grouping": grouping}
            signature = sign_l1_action(self.signer, action, None, nonce, None, False)
            envelope = {
                "action": action,
                "nonce": nonce,
                "signature": signature,
                "vaultAddress": None,
                "expiresAfter": None,
            }
        else:
            envelope = deepcopy(replay)
            if envelope["action"]["orders"] != orders:
                raise ValueError("Replay must reuse the exact original signed action")
            nonce = envelope["nonce"]
        self.reserved += reservation
        metadata = {
            "signer": self.signer.address.lower(),
            "scenario": prefix,
            "coin": market["name"],
            "nonce": nonce,
            "cloids": [order["c"] for order in orders],
            "limit_notional": wire(principal),
            "reservation": wire(reservation),
            "cumulative_reserved_notional": wire(self.reserved),
            "max_spend": wire(self.budget),
            "exact_signed_replay": replay is not None,
            "intended_mutation": "bounded spot IOC if admitted and matched; empty array has no orders",
            "assumption": "case labels describe intent, never guarantee live matching or rejection",
            **(details or {}),
        }
        self.recorder.event(
            {"state": "submission_reserved", "case_id": prefix, **metadata}
        )
        try:
            result = submit(
                self.recorder, prefix, envelope, metadata, self.args.execute
            )
        except (Exception, KeyboardInterrupt) as submission_error:
            if self.args.execute:
                try:
                    self.observe(prefix, orders, None)
                except (Exception, KeyboardInterrupt) as observation_error:
                    self.recorder.event(
                        {
                            "state": "post_submission_observation_failed",
                            "case_id": prefix,
                            "reason": str(observation_error) or "interrupted",
                            "automatic_retry": False,
                        }
                    )
                    raise submission_error from observation_error
            raise
        after = self.observe(prefix, orders, result) if result is not None else None
        if result is not None and (
            result.status is None or result.status == 429 or result.status >= 500
        ):
            raise RuntimeError(
                "Exchange unavailable/rate-limited; evidence captured; no retry"
            )
        return envelope, result, before, after

    def filled(self, result):
        if result is None or not isinstance(result.json, dict):
            return False
        response = result.json.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            return False
        for status in response["data"].get("statuses", []):
            if (
                isinstance(status, dict)
                and isinstance(status.get("filled"), dict)
                and decimal(status["filled"].get("totalSz", "0")) > 0
            ):
                return True
        return False

    def cloid(self):
        market = self.market()
        book_id = "cloid-source-book"
        book = self.book(book_id, market)
        if not book or not book["levels"][1]:
            return self.unavailable("cloid", "No asks for the small buy")
        base = self.tokens[market["tokens"][0]]
        price = price_wire(
            decimal(book["levels"][1][0]["px"]) * Decimal("1.005"),
            base["szDecimals"],
            ROUND_UP,
        )
        size = (Decimal(12) / price).quantize(self.step(market), rounding=ROUND_UP)
        if not MINIMUM <= size * price <= Decimal(15):
            return self.unavailable(
                "cloid", "Size increment cannot fit a10-15 USDC buy"
            )
        order = self.order(market, price, size)
        if not self.can_reserve("cloid", [order], repetitions=3):
            return
        envelope, result, _, _ = self.send(
            "cloid-first", market, [order], details={"source_book": book_id}
        )
        if result is None:
            return self.unavailable(
                "cloid-continuation", "Plan only; reuse requires an observed first fill"
            )
        if not self.filled(result):
            return self.unavailable(
                "cloid-continuation",
                "First IOC did not report a positive fill; no reuse or replay sent",
            )
        self.send(
            "cloid-new-nonce",
            market,
            envelope["action"]["orders"],
            details={
                "source_book": book_id,
                "first_nonce": envelope["nonce"],
                "possible_second_fill": True,
            },
        )
        self.send(
            "cloid-exact-replay",
            market,
            envelope["action"]["orders"],
            replay=envelope,
            details={"replayed_case": "cloid-first", "replay_allowance_reserved": True},
        )

    def batch(self):
        market = self.market()
        # These actions cannot contain a matching order even if invalid grouping were accepted.
        self.send("batch-empty", market, [])
        self.send("batch-invalid-grouping", market, [], grouping="invalid")
        book_id = "batch-source-book"
        book = self.book(book_id, market)
        if not book or not all(book["levels"]):
            return self.unavailable(
                "batch-arrays", "Two-sided book required for no-crossing candidates"
            )
        base = self.tokens[market["tokens"][0]]
        if not 0 <= base["szDecimals"] <= 7:
            return self.unavailable(
                "batch-arrays",
                "Need an invalid size within the eight-decimal wire boundary",
            )
        price = price_wire(
            decimal(book["levels"][0][0]["px"]) * Decimal("0.5"),
            base["szDecimals"],
            ROUND_DOWN,
        )
        if price <= 0:
            return self.unavailable("batch-arrays", "No positive no-crossing price")
        step = self.step(market)
        size = (MINIMUM / price).quantize(step, rounding=ROUND_UP)
        good = self.order(market, price, size)
        bad_size = self.order(market, price, size + step / 10)
        # More than the spot price's allowed decimals, but at most eight wire decimals.
        # For szDecimals0 use a sixth significant digit instead of a ninth decimal.
        tick = Decimal(1).scaleb(-min(8, max(0, 8 - base["szDecimals"]) + 1))
        bad_price_value = price + tick
        if (
            price_wire(bad_price_value, base["szDecimals"], ROUND_DOWN)
            == bad_price_value
        ):
            return self.unavailable(
                "batch-arrays",
                "Could not isolate an invalid price within the wire boundary",
            )
        bad_price = self.order(market, bad_price_value, size)
        reduce_only = {**good, "r": True}
        invalid_asset = {**good, "a": 2147483647}
        if any(
            10000 + item["index"] == invalid_asset["a"]
            for item in self.meta["universe"]
        ):
            raise RuntimeError(
                "Invalid-asset sentinel unexpectedly present in metadata"
            )
        variants = [good, bad_size, bad_price, reduce_only, invalid_asset]
        if any(
            not MINIMUM <= decimal(o["p"]) * decimal(o["s"]) <= MAXIMUM
            for o in variants
        ):
            return self.unavailable(
                "batch-arrays",
                f"Market granularity cannot keep every candidate between {MINIMUM} and {MAXIMUM}",
            )
        if bad_price_value >= decimal(book["levels"][1][0]["px"]):
            return self.unavailable(
                "batch-arrays", "Invalid-price variant would cross the captured ask"
            )
        if not self.can_reserve("batch-arrays", variants, repetitions=2):
            return
        labels = [
            "valid-no-cross",
            "invalid-size",
            "invalid-price",
            "reduce-only",
            "invalid-asset",
        ]
        for direction, orders, names in (
            ("forward", variants, labels),
            ("reverse", list(reversed(variants)), list(reversed(labels))),
        ):
            self.send(
                "batch-" + direction,
                market,
                orders,
                details={
                    "source_book": book_id,
                    "order_variants": names,
                    "precondition": "all known-market buy limits below captured best ask; moving books may fill",
                },
            )

    def rounding(self):
        selection_state = self.state("rounding-selection")
        rate = self.rate(selection_state)
        selected = None
        for market in self.scan_markets():
            base = self.tokens[market["tokens"][0]]
            if base["szDecimals"] < 2:
                continue
            step = self.step(market)
            quantum = Decimal(1).scaleb(-base["weiDecimals"])
            book_id = f"rounding-source-book-{market['index']}"
            book = self.book(book_id, market)
            if not book or not all(book["levels"]):
                continue
            price = price_wire(
                decimal(book["levels"][1][0]["px"]), base["szDecimals"], ROUND_UP
            )
            size = (Decimal(12) / price).quantize(step, rounding=ROUND_UP)
            effective_rate = rate * (
                Decimal("0.2") if market["tokens"][0] in self.quote_tokens else 1
            )
            quote_quantum = Decimal(1).scaleb(
                -self.tokens[market["tokens"][1]]["weiDecimals"]
            )
            direct_fee = (size * effective_rate).quantize(quantum, rounding=ROUND_DOWN)
            quote_fee = (price * size * effective_rate).quantize(
                quote_quantum, rounding=ROUND_DOWN
            )
            converted_fee = (quote_fee / price).quantize(quantum, rounding=ROUND_DOWN)
            if direct_fee == converted_fee:
                continue
            if not MINIMUM <= price * size <= Decimal(15):
                continue
            depth = sum(
                (
                    decimal(level["sz"])
                    for level in book["levels"][1]
                    if decimal(level["px"]) <= price
                ),
                Decimal(0),
            )
            if depth < size:
                continue
            selected = (
                market,
                price,
                size,
                quantum,
                book_id,
                effective_rate,
                direct_fee,
                converted_fee,
            )
            break
        if selected is None:
            return self.unavailable(
                "rounding",
                "No szDecimals>=2 market with10-15 USDC depth distinguishing base-direct versus quote-first fee rounding in this scan window",
            )
        (
            market,
            price,
            size,
            quantum,
            book_id,
            effective_rate,
            direct_fee,
            converted_fee,
        ) = selected
        order = self.order(market, price, size)
        if not self.can_reserve("rounding-buy", [order]):
            return
        _, result, before, after = self.send(
            "rounding-buy",
            market,
            [order],
            details={
                "source_book": book_id,
                "observed_taker_rate": wire(rate),
                "effective_rate_from_quote_pair_rule": wire(effective_rate),
                "base_fee_quantum": wire(quantum),
                "direct_base_fee": wire(direct_fee),
                "quote_first_converted_fee": wire(converted_fee),
                "precondition": "base-direct and quote-first fee hypotheses differ; actual fills decide outcome",
            },
        )
        if not self.filled(result):
            return self.unavailable(
                "rounding-sell", "No observed positive buy fill (or plan only)"
            )
        base_index = market["tokens"][0]
        acquired = free_balance(after["balances"], base_index) - free_balance(
            before["balances"], base_index
        )
        sell_state = self.state("rounding-sell-selection")
        owned = min(acquired, free_balance(sell_state["balances"], base_index))
        sell_book_id = "rounding-sell-source-book"
        book = self.book(sell_book_id, market)
        if owned <= 0 or not book or not book["levels"][0]:
            return self.unavailable(
                "rounding-sell",
                "No observable post-fee acquired free balance or no bids",
            )
        base = self.tokens[base_index]
        sell_price = price_wire(
            decimal(book["levels"][0][0]["px"]) * Decimal("0.995"),
            base["szDecimals"],
            ROUND_DOWN,
        )
        if sell_price <= 0:
            return self.unavailable("rounding-sell", "No positive sell limit")
        sell_size = min(owned / HEADROOM, Decimal(15) / sell_price).quantize(
            self.step(market), rounding=ROUND_DOWN
        )
        if sell_size * sell_price < MINIMUM:
            return self.unavailable(
                "rounding-sell",
                "Actual acquired post-fee balance cannot fund10 USDC plus source headroom",
                observed_acquired=wire(acquired),
                observed_free=wire(owned),
            )
        sell_order = self.order(market, sell_price, sell_size, buy=False)
        if self.can_reserve("rounding-sell", [sell_order]):
            self.send(
                "rounding-sell",
                market,
                [sell_order],
                details={
                    "source_book": sell_book_id,
                    "observed_acquired": wire(acquired),
                    "observed_free_base": wire(owned),
                    "source_headroom_factor": wire(HEADROOM),
                },
            )

    def multilevel(self):
        market = self.market()
        if self.args.coin != "@49" or market["index"] != 49:
            raise ValueError("multilevel requires explicit --coin @49")
        book_id = "multilevel-source-book"
        book = self.book(book_id, market)
        if not book or len(book["levels"][1]) < 2:
            return self.unavailable("multilevel", "Two distinct ask levels required")
        asks = book["levels"][1]
        best = decimal(asks[0]["px"])
        second = next(
            (decimal(level["px"]) for level in asks if decimal(level["px"]) > best),
            None,
        )
        if second is None:
            return self.unavailable("multilevel", "Two distinct ask prices required")
        available = sum(
            (decimal(level["sz"]) for level in asks if decimal(level["px"]) == best),
            Decimal(0),
        )
        step = self.step(market)
        size = available + step
        base = self.tokens[market["tokens"][0]]
        principal = size * second
        if (
            size.quantize(step, rounding=ROUND_DOWN) != size
            or price_wire(second, base["szDecimals"], ROUND_DOWN) != second
        ):
            return self.unavailable(
                "multilevel", "Captured size/price is not wire-canonical"
            )
        if not MINIMUM <= principal <= MAXIMUM or principal * HEADROOM > self.budget:
            return self.unavailable(
                "multilevel",
                f"Best visible size plus one lot must fit {MINIMUM}-{MAXIMUM} USDC and --max-spend including headroom",
                captured_limit_notional=wire(principal),
            )
        second_depth = sum(
            (decimal(level["sz"]) for level in asks if decimal(level["px"]) == second),
            Decimal(0),
        )
        if second_depth < step:
            return self.unavailable(
                "multilevel", "Second ask lacks one visible base lot"
            )
        order = self.order(market, second, size)
        if not self.can_reserve("multilevel", [order]):
            return
        previous_oids = set(self.known_oids)
        envelope, result, _, _ = self.send(
            "multilevel-attempt",
            market,
            [order],
            details={
                "source_book": book_id,
                "book_time": book.get("time"),
                "best_ask": wire(best),
                "second_ask": wire(second),
                "best_visible_size": wire(available),
                "base_lot": wire(step),
                "requested_size": wire(size),
                "attempt_budget_cap": wire(self.budget),
                "principal_cap": wire(MAXIMUM),
                "precondition": "captured best depth plus one lot targets two levels; actual fills alone classify execution",
            },
        )
        if result is None:
            return self.unavailable("multilevel-outcome", "Plan only; no fill outcome")
        captured = {}
        for aggregate in (False, True):
            mode = "aggregated" if aggregate else "individual"
            response = info(
                self.recorder,
                "multilevel-fills-" + mode,
                {
                    "type": "userFills",
                    "user": self.signer.address,
                    "aggregateByTime": aggregate,
                },
            )
            if (
                response.transport_error
                or response.status != 200
                or not isinstance(response.json, list)
            ):
                raise RuntimeError(
                    "Multilevel fill capture unavailable; halt without retry"
                )
            captured[mode] = response.json
        cloids = {item["c"] for item in envelope["action"]["orders"]}
        oids = set(self.known_oids) - previous_oids
        fills = [
            fill
            for fill in captured["individual"]
            if fill.get("cloid") in cloids or fill.get("oid") in oids
        ]
        prices = sorted(
            {decimal(fill["px"]) for fill in fills if decimal(fill["sz"]) > 0}
        )
        positive = [fill for fill in fills if decimal(fill["sz"]) > 0]
        self.recorder.event(
            {
                "state": "multilevel_outcome_observed",
                "case": "multilevel",
                "cloids": sorted(cloids),
                "oids": sorted(oids),
                "observed_fills": fills,
                "positive_fill_count": len(positive),
                "distinct_fill_prices": [wire(px) for px in prices],
                "actual_multi_fill": len(positive) > 1,
                "actual_multi_price": len(prices) > 1,
                "classification": "multiple observed prices"
                if len(prices) > 1
                else "multiple fills at one price"
                if len(positive) > 1
                else "one observed fill"
                if positive
                else "no matched fills observed",
                "source_individual_fills": "multilevel-fills-individual",
                "source_aggregated_fills": "multilevel-fills-aggregated",
                "caveat": "Endpoint-limited observations; moving books need not execute at the captured two levels",
                "automatic_retry": False,
            }
        )

    def partial(self):
        for market in self.scan_markets():
            book_id = f"partial-source-book-{market['index']}"
            book = self.book(book_id, market)
            if not book or not book["levels"][1]:
                continue
            best = min(decimal(level["px"]) for level in book["levels"][1])
            available = sum(
                (
                    decimal(level["sz"])
                    for level in book["levels"][1]
                    if decimal(level["px"]) == best
                ),
                Decimal(0),
            )
            if available * best >= MAXIMUM:
                continue
            step = self.step(market)
            size = max(
                available.quantize(step, rounding=ROUND_DOWN) + step,
                (MINIMUM / best).quantize(step, rounding=ROUND_UP),
            )
            if not MINIMUM <= size * best <= MAXIMUM or size <= available:
                continue
            order = self.order(market, best, size)
            if not self.can_reserve("partial", [order]):
                continue
            self.send(
                "partial-attempt",
                market,
                [order],
                details={
                    "source_book": book_id,
                    "book_time": book.get("time"),
                    "visible_at_limit": wire(available),
                    "visible_notional": wire(available * best),
                    "requested_size": wire(size),
                    "limit_exactly_best_ask": wire(best),
                    "market_offset": self.args.market_offset,
                    "market_count": self.args.market_count,
                    "precondition": "requested size exceeds all captured depth at the exact best ask; book can change before matching",
                },
            )
            return
        self.unavailable(
            "partial",
            "No suitable thin book within the explicit scan, lot-size, and remaining-budget bounds; no partial action submitted",
            market_offset=self.args.market_offset,
            market_count=self.args.market_count,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", required=True, type=Path, help="New or empty capture directory"
    )
    parser.add_argument("--base-url", default=TESTNET, help="Testnet or loopback only")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--cases",
        default="cloid,batch,rounding,partial",
        help="Ordered unique subset of cloid,batch,rounding,partial,multilevel; multilevel is opt-in and requires --coin @49",
    )
    parser.add_argument(
        "--max-spend",
        default="100",
        help="Cumulative potential USDC notional plus1%%, including replays and sells",
    )
    parser.add_argument("--key-env", default="HYPERLIQUID_PRIVATE_KEY")
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    parser.add_argument(
        "--coin", help="Exact USDC-quoted market name; restricts scans to this market"
    )
    parser.add_argument(
        "--market-offset",
        type=int,
        default=0,
        help="Offset in sorted USDC spot markets, matching probes.partial's first30 ordering",
    )
    parser.add_argument(
        "--market-count",
        type=int,
        default=30,
        help="Bounded read-only scan window (1-150)",
    )
    args = parser.parse_args()
    cases = choose_cases(
        args.cases, ("cloid", "batch", "rounding", "partial", "multilevel")
    )
    if "multilevel" in cases and args.coin != "@49":
        parser.error("multilevel requires explicit --coin @49")
    if (
        decimal(args.max_spend) <= 0
        or args.market_offset < 0
        or not 1 <= args.market_count <= 150
    ):
        parser.error(
            "Require positive max-spend, nonnegative market-offset, and market-count1-150"
        )
    recorder = Recorder(args.root, args.base_url)
    campaign = None
    try:
        recorder.event(
            {
                "state": "workflow_started",
                "workflow": "order-campaign",
                "cases": cases,
                "execute": args.execute,
                "max_spend": args.max_spend,
                "market_offset": args.market_offset,
                "market_count": args.market_count,
            }
        )
        campaign = Campaign(recorder, args)
        for case in cases:
            getattr(campaign, case)()
            recorder.event(
                {
                    "state": "case_completed",
                    "case": case,
                    "meaning": "workflow finished; inspect observations and precondition_unavailable events, not a parity verdict",
                }
            )
        recorder.event(
            {
                "state": "workflow_completed",
                "workflow": "order-campaign",
                "execute": args.execute,
                "cumulative_reserved_notional": wire(campaign.reserved),
            }
        )
    except (Exception, KeyboardInterrupt) as exc:
        recorder.event(
            {
                "state": "workflow_aborted",
                "workflow": "order-campaign",
                "reason": str(exc) or "interrupted",
                "automatic_retry": False,
                "cumulative_reserved_notional": wire(campaign.reserved)
                if campaign
                else "0",
            }
        )
        raise
    print(f"Evidence: {recorder.root}")


if __name__ == "__main__":
    main()
