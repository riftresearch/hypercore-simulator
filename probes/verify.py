"""Exercise the real loopback server; every assertion and HTTP exchange is recorded.

Run: uv run python -m probes.verify [--root DIR]
No funded-account credentials are used. Signing keys below are public local fixtures.
"""

from __future__ import annotations

import argparse
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action, sign_send_asset_action

from . import Recorder
from .recorder import TESTNET, create, encode, run_root
from .replay import reverse_objects


class Verification:
    def __init__(self, recorder, mainnet=False):
        if recorder.base_url == TESTNET:
            raise ValueError("Verification mutates state and is loopback-only")
        self.recorder = recorder
        self.mainnet = mainnet
        self.sequence = 0
        self.assertions = 0
        self.now = 1_800_000_000_000
        self.nonce = self.now
        self.a = Account.from_key(bytes.fromhex("11" * 32))
        self.b = Account.from_key(bytes.fromhex("22" * 32))
        self.c = Account.from_key(bytes.fromhex("33" * 32))

    def call(self, name, path, body, **kwargs):
        self.sequence += 1
        return self.recorder.request(
            f"{self.sequence:03d}-{name}", path, body, **kwargs
        )

    def check(self, name, condition):
        self.recorder.event(
            {"state": "assertion", "name": name, "passed": bool(condition)}
        )
        if not condition:
            raise AssertionError(name)
        self.assertions += 1

    def info(self, kind, user=None, **fields):
        body = {"type": kind, **fields}
        if user is not None:
            body["user"] = user
        result = self.call(kind, "/info", body)
        self.check(
            kind + " returns HTTP 200",
            result.status == 200 and not result.transport_error,
        )
        return result.json

    def control(self, name, **fields):
        result = self.call(name, "/_test/" + name, fields)
        self.check(
            name + " control succeeds",
            result.status == 200 and not result.transport_error,
        )
        return result.json

    def balances(self, account):
        state = self.info("spotClearinghouseState", account.address)
        return {row["token"]: Decimal(row["total"]) for row in state["balances"]}

    def send(self, sender, recipient, amount="0.1", nonce=None):
        if nonce is None:
            self.nonce += 1
            nonce = self.nonce
        action = {
            "type": "sendAsset",
            "destination": recipient.address.lower(),
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": self.usdc_wire,
            "amount": amount,
            "fromSubAccount": "",
            "nonce": nonce,
        }
        signature = sign_send_asset_action(sender, action, self.mainnet)
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }

    def order(self, size, price, buy=True, expires=None):
        self.nonce += 1
        cloid = "0x" + f"{self.nonce:032x}"
        action = {
            "type": "order",
            "orders": [
                {
                    "a": 10000 + self.pair["index"],
                    "b": buy,
                    "p": price,
                    "s": size,
                    "r": False,
                    "t": {"limit": {"tif": "Ioc"}},
                    "c": cloid,
                }
            ],
            "grouping": "na",
        }
        signature = sign_l1_action(
            self.a, action, None, self.nonce, expires, self.mainnet
        )
        return {
            "action": action,
            "nonce": self.nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": expires,
        }

    def submit(self, name, envelope):
        result = self.call(name, "/exchange", envelope)
        self.check(
            name + " returns in-band JSON",
            result.status == 200 and isinstance(result.json, dict),
        )
        return result.json

    def replay_readonly(self, root):
        # Live books/fee schedules/ledger histories vary with time. Compare stable
        # schema failures and unknown lookups exactly, not their dynamic values.
        compared = 0
        for directory in sorted(root.iterdir()):
            if not (directory / "metadata.json").exists():
                continue
            metadata = json.loads((directory / "metadata.json").read_text())
            if metadata.get("state") != "completed":
                continue
            raw = (directory / "request.body").read_bytes()
            expected = (directory / "response.body").read_bytes()
            status = metadata["status"]
            stable = status in (400, 415, 422) or (
                status == 200
                and expected
                in (
                    b"null",
                    b'{"status":"unknownOid"}',
                    b'{"status":"err","response":"Unable to recover signer."}',
                )
            )
            # Invalid aggregation options are repeatable, endpoint-specific 500s.
            stable = stable or (
                directory.name.startswith(("book-sigfigs-", "book-mantissa-"))
                and status == 500
            )
            if not stable:
                continue
            result = self.call(
                "replay-" + directory.name,
                urlsplit(metadata["url"]).path,
                raw,
                content_type=metadata["content_type"],
                metadata={
                    "reference": str(directory),
                    "comparison": "exact status and body bytes",
                },
            )
            self.check(
                "captured parity: " + directory.name,
                result.status == status and result.body == expected,
            )

            def content_type(path):
                return next(
                    line.split(":", 1)[1].strip().lower()
                    for line in path.read_text().splitlines()
                    if line.lower().startswith("content-type:")
                )

            self.check(
                "captured content type: " + directory.name,
                content_type(result.directory / "response.headers")
                == content_type(directory / "response.headers"),
            )
            compared += 1
        self.check("captured schema cases were exercised", compared >= 20)
        self.recorder.event(
            {
                "state": "readonly_replay_complete",
                "compared": compared,
                "excluded": "Dynamic state/books/fees and numeric type42 lending dispatch (outside spot scope)",
            }
        )

    def signed(self, wallet, action, expires=None):
        self.nonce += 1
        signature = sign_l1_action(
            wallet, action, None, self.nonce, expires, self.mainnet
        )
        return {
            "action": action,
            "nonce": self.nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": expires,
        }

    def resting_orders(self):
        """Resting, post-only, cancel, modify and trigger paths, each signed by the SDK."""
        coin = self.pair["name"]
        asset = 10000 + self.pair["index"]
        usdc = self.usdc["index"]

        def limit(tif, buy, px, sz, cloid=None):
            order = {"a": asset, "b": buy, "p": px, "s": sz, "r": False, "t": {"limit": {"tif": tif}}}
            if cloid:
                order["c"] = cloid
            return order

        def hold():
            state = self.info("spotClearinghouseState", self.a.address)
            return Decimal(next(row for row in state["balances"] if row["token"] == usdc)["hold"])

        self.control("book", coin=coin, markPx="1",
                     bids=[{"px": "0.9", "sz": "1000", "n": 1}], asks=[{"px": "1.1", "sz": "1000", "n": 1}])
        self.control("fund", address=self.a.address, token="USDC", amount="100", mode="transfer")
        rested = self.submit("gtc-rests", self.signed(self.a, {"type": "order", "orders": [limit("Gtc", True, "1", "20")], "grouping": "na"}))
        oid = rested["response"]["data"]["statuses"][0].get("resting", {}).get("oid")
        self.check("Gtc order rests", isinstance(oid, int))
        self.check("resting buy locks its quote notional", hold() == 20)
        self.check("openOrders lists the resting order", [o["oid"] for o in self.info("openOrders", self.a.address)] == [oid])
        book = self.info("l2Book", coin=coin)
        self.check("resting order is visible in the book", book["levels"][0][0] == {"px": "1.0", "sz": "20.0", "n": 1})
        crossing = self.submit("alo-crosses", self.signed(self.a, {"type": "order", "orders": [limit("Alo", True, "1.1", "20")], "grouping": "na"}))
        self.check("post-only order that would cross is rejected",
                   crossing["response"]["data"]["statuses"][0].get("error", "").startswith("Post only order would have immediately matched"))
        modified = self.submit("modify", self.signed(self.a, {"type": "modify", "oid": oid, "order": limit("Gtc", True, "1.05", "10")}))
        self.check("modify replaces the order",
                   modified["response"]["type"] == "default"
                   and self.info("orderStatus", self.a.address, oid=oid)["order"]["status"] == "canceled")
        open_orders = self.info("openOrders", self.a.address)
        self.check("modified order rests under a new oid", len(open_orders) == 1 and open_orders[0]["oid"] != oid and open_orders[0]["limitPx"] == "1.05")
        canceled = self.submit("cancel", self.signed(self.a, {"type": "cancel", "cancels": [{"a": asset, "o": open_orders[0]["oid"]}, {"a": asset, "o": 0}]}))
        statuses = canceled["response"]["data"]["statuses"]
        self.check("cancel succeeds and reports missing orders", statuses[0] == "success" and "never placed" in statuses[1]["error"])
        cloid = "0x" + f"{self.nonce + 1:032x}"
        self.submit("gtc-cloid", self.signed(self.a, {"type": "order", "orders": [limit("Gtc", True, "1", "20", cloid)], "grouping": "na"}))
        by_cloid = self.submit("cancel-by-cloid", self.signed(self.a, {"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": cloid}]}))
        self.check("cancelByCloid succeeds", by_cloid["response"]["data"]["statuses"] == ["success"])
        self.check("cancels release the hold", hold() == 0)
        self.control("fund", address=self.a.address, token=self.base["name"], amount="20", mode="transfer")
        stop = {"a": asset, "b": False, "p": "0.85", "s": "20", "r": False,
                "t": {"trigger": {"isMarket": True, "triggerPx": "0.95", "tpsl": "sl"}}}
        trigger = self.submit("stop-market", self.signed(self.a, {"type": "order", "orders": [stop], "grouping": "na"}))
        stop_oid = trigger["response"]["data"]["statuses"][0].get("resting", {}).get("oid")
        frontend = self.info("frontendOpenOrders", self.a.address)
        self.check("stop order rests untriggered", isinstance(stop_oid, int) and frontend[0]["triggerCondition"] == "Price below 0.95")
        self.control("book", coin=coin, markPx="0.9")
        self.check("stop fires when the mark crosses its trigger",
                   self.info("orderStatus", self.a.address, oid=stop_oid)["order"]["status"] == "filled")
        gated = self.submit("schedule-cancel", self.signed(self.a, {"type": "scheduleCancel", "time": self.now + 10_000}))
        self.check("scheduleCancel is volume gated", gated["status"] == "err")


    def run(self, evidence):
        self.control("reset")
        self.control("time", now_ms=self.now)
        self.replay_readonly(evidence)
        self.replay_readonly(evidence.parent / "unsigned-shapes")
        meta = self.info("spotMeta")
        self.usdc = next(token for token in meta["tokens"] if token["name"] == "USDC")
        self.usdc_wire = self.usdc["name"] + ":" + self.usdc["tokenId"]
        tokens = {token["index"]: token for token in meta["tokens"]}
        self.pair = next(
            pair
            for pair in meta["universe"]
            if pair["tokens"][1] == self.usdc["index"]
            and tokens[pair["tokens"][0]]["szDecimals"] == 0
        )
        self.base = tokens[self.pair["tokens"][0]]
        self.check(
            "fresh account missing",
            self.info("userRole", self.b.address) == {"role": "missing"},
        )
        self.control(
            "fund", address=self.a.address, token="USDC", amount="501", mode="deposit"
        )
        self.check(
            "deposit activation deducted once",
            self.balances(self.a)[self.usdc["index"]] == Decimal(500),
        )
        self.control(
            "fund", address=self.a.address, token="USDC", amount="1", mode="deposit"
        )
        self.check(
            "existing deposit has no activation",
            self.balances(self.a)[self.usdc["index"]] == Decimal(501),
        )
        first = self.send(self.a, self.b, "2")
        self.check(
            "fresh send accepted",
            self.submit("fresh-send", first)
            == {"status": "ok", "response": {"type": "default"}},
        )
        self.check(
            "sender pays activation",
            self.balances(self.a)[self.usdc["index"]] == Decimal(498),
        )
        self.check(
            "recipient credited in full",
            self.balances(self.b)[self.usdc["index"]] == Decimal(2),
        )
        self.check(
            "recipient becomes user",
            self.info("userRole", self.b.address) == {"role": "user"},
        )
        a_ledger = self.info(
            "userNonFundingLedgerUpdates", self.a.address, startTime=self.now
        )
        b_ledger = self.info(
            "userNonFundingLedgerUpdates", self.b.address, startTime=self.now
        )
        self.check(
            "same positive send delta on both sides",
            a_ledger[-1] == b_ledger[-1]
            and Decimal(b_ledger[-1]["delta"]["amount"]) == 2,
        )
        self.check(
            "ledger includes action nonce",
            b_ledger[-1]["delta"]["nonce"] == first["nonce"],
        )
        self.check(
            "nonce replay refused",
            self.submit("nonce-replay", first)["status"] == "err",
        )
        self.check(
            "replay cannot duplicate credit",
            self.balances(self.b)[self.usdc["index"]] == Decimal(2),
        )
        tampered = self.send(self.a, self.b)
        tampered["action"]["amount"] = "0.2"
        self.check(
            "tampered payload not authorized as A",
            self.submit("tampered-signature", tampered)["status"] == "err",
        )
        self.check(
            "unknown signer refused",
            self.submit("unknown-signer", self.send(self.c, self.b))["status"] == "err",
        )
        opposite_chain = self.send(self.a, self.b)
        opposite_chain["signature"] = sign_send_asset_action(
            self.a, opposite_chain["action"], not self.mainnet
        )
        self.check(
            "opposite user-signature chain rejected",
            self.submit("opposite-user-chain", opposite_chain)["status"] == "err",
        )
        insufficient = self.send(self.b, self.a, "3")
        self.check(
            "insufficient balance refused",
            self.submit("insufficient", insufficient)["status"] == "err",
        )
        self.check(
            "failed transfer leaves sender unchanged",
            self.balances(self.b)[self.usdc["index"]] == Decimal(2),
        )
        self.check(
            "old nonce refused",
            self.submit(
                "old-nonce", self.send(self.a, self.b, nonce=self.now - 172_800_001)
            )["status"]
            == "err",
        )
        self.check(
            "future nonce refused",
            self.submit(
                "future-nonce", self.send(self.a, self.b, nonce=self.now + 86_400_001)
            )["status"]
            == "err",
        )

        coin = self.pair["name"]
        self.control(
            "book",
            coin=coin,
            bids=[
                {"px": "4.5795", "sz": "2", "n": 1},
                {"px": "4.5788", "sz": "3", "n": 2},
            ],
            asks=[
                {"px": "4.6252", "sz": "4", "n": 1},
                {"px": "4.6256", "sz": "5", "n": 2},
            ],
        )
        aggregated = self.info("l2Book", coin=coin, nSigFigs=2)["levels"]
        self.check(
            "aggregation rounds outward and conserves size/count",
            aggregated
            == [
                [{"px": "4.5", "sz": "5.0", "n": 3}],
                [{"px": "4.7", "sz": "9.0", "n": 3}],
            ],
        )
        coarse = self.info("l2Book", coin=coin, nSigFigs=5, mantissa=2)["levels"]
        self.check(
            "mantissa widens the price bucket",
            coarse[0][0]["px"] == "4.5794" and coarse[1][0]["px"] == "4.6252",
        )
        self.control(
            "book",
            coin=coin,
            bids=[{"px": "4.5", "sz": "100", "n": 1}],
            asks=[{"px": "5", "sz": "2", "n": 1}, {"px": "5.1", "sz": "1", "n": 1}],
        )
        buy_before = self.balances(self.a)
        buy = self.order("5", "5.2")
        result = self.submit("partial-ioc", buy)
        filled = result["response"]["data"]["statuses"][0]["filled"]
        self.check("IOC partial fill stops at depth", Decimal(filled["totalSz"]) == 3)
        opposite_agent = self.order("3", "5.2")
        opposite_agent["signature"] = sign_l1_action(
            self.a,
            opposite_agent["action"],
            None,
            opposite_agent["nonce"],
            None,
            not self.mainnet,
        )
        self.check(
            "opposite phantom-agent domain rejected",
            self.submit("opposite-agent-domain", opposite_agent)["status"] == "err",
        )
        for name, transformed in (
            ("reordered", reverse_objects(buy)),
            ("padded", deepcopy(buy)),
            ("uppercase", deepcopy(buy)),
        ):
            wire_order = transformed["action"]["orders"][0]
            if name == "padded":
                wire_order["p"] = format(Decimal(wire_order["p"]), ".9f")
                wire_order["s"] = format(Decimal(wire_order["s"]), ".9f")
            if name == "uppercase":
                wire_order["c"] = "0x" + wire_order["c"][2:].upper()
            self.check(
                name + " wire form recovers original signer",
                self.submit(name + "-replay", transformed)
                == {
                    "status": "err",
                    "response": f"Invalid nonce: duplicate nonce {buy['nonce']}",
                },
            )
        status = self.info("orderStatus", self.a.address, oid=filled["oid"])
        cloid_status = self.info(
            "orderStatus", self.a.address, oid=buy["action"]["orders"][0]["c"]
        )
        self.check("oid and cloid identify same order", status == cloid_status)
        self.check(
            "unfilled IOC remainder does not rest",
            status["order"]["status"] == "filled"
            and Decimal(status["order"]["order"]["sz"]) == 2,
        )
        fills = self.info("userFills", self.a.address)
        buy_fills = [fill for fill in fills if fill["oid"] == filled["oid"]]
        self.check(
            "buy fill fee in received token",
            all(
                fill["feeToken"] == self.base["name"] and fill["dir"] == "Buy"
                for fill in buy_fills
            ),
        )
        self.check(
            "fills reconcile to execution",
            sum(Decimal(fill["sz"]) for fill in buy_fills) == 3,
        )
        expected_base = Decimal(3) - sum(Decimal(fill["fee"]) for fill in buy_fills)
        self.check(
            "received balance is fee net",
            self.balances(self.a)[self.base["index"]] == expected_base,
        )
        buy_after = self.balances(self.a)
        buy_notional = sum(
            Decimal(fill["px"]) * Decimal(fill["sz"]) for fill in buy_fills
        )
        self.check(
            "buy debits executed quote only",
            buy_after[self.usdc["index"]]
            == buy_before[self.usdc["index"]] - buy_notional,
        )
        self.check(
            "buy charges measured taker rate",
            sum(Decimal(fill["fee"]) for fill in buy_fills) == Decimal("0.0021"),
        )
        self.check("depth consumed", self.info("l2Book", coin=coin)["levels"][1] == [])
        no_fill = self.submit("empty-book-ioc", self.order("3", "5"))
        self.check(
            "empty IOC reports per-order error",
            "error" in no_fill["response"]["data"]["statuses"][0],
        )
        self.control(
            "book",
            coin=coin,
            bids=[{"px": "4.5", "sz": "100", "n": 1}],
            asks=[{"px": "5", "sz": "100", "n": 1}],
        )
        minimum = self.submit("minimum-ioc", self.order("1", "5"))
        self.check(
            "minimum notional enforced before matching",
            minimum["response"]["data"]["statuses"][0]
            == {
                "error": f"Order must have minimum value of 10 USDC. asset={10000 + self.pair['index']}"
            },
        )
        precision = self.submit("precision-ioc", self.order("3.1", "5"))
        self.check(
            "excess size precision rejected rather than rounded",
            precision["response"]["data"]["statuses"][0]
            == {"error": "Order has invalid size."},
        )
        self.control(
            "book",
            coin=coin,
            bids=[{"px": "6", "sz": "100", "n": 1}],
            asks=[{"px": "7", "sz": "100", "n": 1}],
        )
        sell_before = self.balances(self.a)
        sell = self.submit("sell-ioc", self.order("2", "6", buy=False))
        sell_oid = sell["response"]["data"]["statuses"][0]["filled"]["oid"]
        sell_fills = [
            fill
            for fill in self.info("userFills", self.a.address)
            if fill["oid"] == sell_oid
        ]
        self.check(
            "sell fee in received USDC",
            all(
                fill["feeToken"] == "USDC" and fill["dir"] == "Sell"
                for fill in sell_fills
            ),
        )
        self.check(
            "sell fills reconcile to requested quantity",
            sum(Decimal(fill["sz"]) for fill in sell_fills) == 2,
        )
        sell_after = self.balances(self.a)
        sell_received = sum(
            Decimal(fill["px"]) * Decimal(fill["sz"]) - Decimal(fill["fee"])
            for fill in sell_fills
        )
        self.check(
            "sell debits exactly sold base",
            sell_after[self.base["index"]] == sell_before[self.base["index"]] - 2,
        )
        self.check(
            "sell credits fee-net quote",
            sell_after[self.usdc["index"]]
            == sell_before[self.usdc["index"]] + sell_received,
        )
        expiring = self.order("2", "7", expires=self.now + 10_000)
        self.check(
            "expiry enters signed action hash",
            self.submit("expiry-signed-order", expiring)["status"] == "ok",
        )

        self.resting_orders()

        for kind in ("refuse", "rate_limit", "delay", "drop_after_commit"):
            before = self.balances(self.b)[self.usdc["index"]]
            envelope = self.send(self.a, self.b)
            self.control("fault", kind=kind, delay_ms=150)
            result = self.call("fault-" + kind, "/exchange", envelope)
            after = self.balances(self.b)[self.usdc["index"]]
            if kind in ("refuse", "rate_limit"):
                self.check(kind + " does not mutate", after == before)
                self.check(
                    kind + " exposes requested failure",
                    result.status == (429 if kind == "rate_limit" else 200)
                    and (kind == "rate_limit" or result.json["status"] == "err"),
                )
                self.check(
                    kind + " does not consume nonce",
                    self.submit("after-" + kind, envelope)["status"] == "ok",
                )
            else:
                self.check(
                    kind + " records action once", after == before + Decimal("0.1")
                )
                if kind == "drop_after_commit":
                    self.check(
                        "dropped response is a transport failure",
                        result.transport_error is not None,
                    )
                    ledger = self.info(
                        "userNonFundingLedgerUpdates",
                        self.b.address,
                        startTime=self.now,
                    )
                    self.check(
                        "ambiguous send resolved by nonce",
                        len(
                            [
                                entry
                                for entry in ledger
                                if entry["delta"].get("nonce") == envelope["nonce"]
                            ]
                        )
                        == 1,
                    )
                else:
                    timing = json.loads(
                        (result.directory / "metadata.json").read_text()
                    )["transfer"]["time_total"]
                    self.check("response really delayed", timing >= 0.15)
                self.check(
                    kind + " replay rejected",
                    self.submit("replay-" + kind, envelope)["status"] == "err",
                )
                self.check(
                    kind + " replay cannot duplicate credit",
                    self.balances(self.b)[self.usdc["index"]] == after,
                )

        self.control(
            "fund",
            address=self.c.address,
            sender=self.a.address,
            token="USDC",
            amount="2",
            mode="transfer",
        )
        self.check(
            "transfer-style funding credits full amount",
            self.balances(self.c)[self.usdc["index"]] == Decimal(2),
        )
        self.control(
            "fund",
            address=self.c.address,
            token="USDC",
            amount="9007199254740993.125",
            mode="transfer",
            serialize_f64=True,
        )
        exact_total = Decimal("9007199254740995.125")
        reported_total = self.balances(self.c)[self.usdc["index"]]
        self.check(
            "optional f64 mode exposes observable precision loss",
            reported_total == Decimal(str(float(exact_total)))
            and reported_total != exact_total,
        )
        large = Account.from_key(bytes.fromhex("44" * 32))
        maximum = Decimal(79228162514264337593543950335)
        self.control(
            "fund",
            address=large.address,
            token="USDC",
            amount=str(maximum),
            mode="transfer",
        )
        small_before = self.balances(self.b)[self.usdc["index"]]
        self.check(
            "lossy debit refused",
            self.submit("lossy-debit", self.send(large, self.b))["status"] == "err",
        )
        self.check(
            "lossy debit cannot create recipient funds",
            self.balances(self.b)[self.usdc["index"]] == small_before,
        )
        self.check(
            "unrepresentable debit leaves sender intact",
            self.balances(large)[self.usdc["index"]] == maximum,
        )
        self.check(
            "lossy recipient credit refused atomically",
            self.submit("lossy-credit", self.send(self.b, large))["status"] == "err",
        )
        self.check(
            "failed recipient credit cannot debit sender",
            self.balances(self.b)[self.usdc["index"]] == small_before,
        )
        report = {
            "assertions_passed": self.assertions,
            "requests_recorded": self.sequence,
            "network_signatures": "mainnet" if self.mainnet else "testnet",
            "coverage": [
                "captured HTTP failures",
                "funding modes",
                "real SDK signature recovery",
                "nonce replay/window",
                "activation and ledger",
                "IOC depth/fees/status",
                "all four faults",
            ],
        }
        create(self.recorder.root / "verification.json", encode(report))
        print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Evidence directory; defaults to a fresh temporary directory")
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--evidence", type=Path, default=Path("captures/testnet/readonly-contract")
    )
    parser.add_argument("--network", choices=("testnet", "mainnet"), default="testnet")
    args = parser.parse_args()
    args.root = run_root(args.root, "verify")
    Verification(Recorder(args.root, args.base_url), args.network == "mainnet").run(
        args.evidence
    )


if __name__ == "__main__":
    main()
