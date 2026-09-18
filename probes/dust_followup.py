"""Recover proven old dust inventory, then measure one bounded minute-scale conversion."""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_send_asset_action

from .dust_lifecycle import HaltRecorder
from .matrix import info
from .recorder import TESTNET, create, encode, timestamp
from .scenarios import load_meta, reserve_nonce, snapshot, submit, wallet, wire

OLD_TIME = 1789714860046
OLD_OID = 60415128044
ZERO_HASH = "0x" + "0" * 64
OLD_CREDITS = {"a": Decimal("3.04082091"), "d": Decimal("1.53547344")}
OLD_SIZES = {"a": Decimal("0.99019"), "d": Decimal("0.5")}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def amount(value):
    result = Decimal(str(value))
    require(result.is_finite(), "Non-finite amount")
    return result


def utc_ms(value):
    return datetime.fromtimestamp(value / 1000, UTC).isoformat(timespec="milliseconds")


class Campaign:
    def __init__(self, args, actors, recorder):
        self.args, self.actors, self.recorder = args, actors, recorder
        self.started = time.monotonic()
        self.actions, self.polls, self.markets = [], [], []
        self.conversions, self.statuses, self.comparisons = {}, {}, []
        self.initial, self.final, self.old = {}, {}, {}
        self.expected, self.baseline_fills = {}, {}
        self.gross = Decimal(0)
        self.phase = "fresh_diagnostics"
        self.experiment_start_ms = None
        self.credit_baseline = {}
        self.conversion_credit = {}
        self.last_state = {}
        self.spendability = {}
        self.old_analysis = {}

        self.authorized_fills = []

    def authorize_runs(self):
        for root in self.args.authorized_run:
            rows = [
                json.loads(line)
                for line in (root / "manifest.jsonl").read_text().splitlines()
            ]
            require(
                any(row.get("state") == "scenario_completed" for row in rows),
                f"Authorized run is not completed: {root}",
            )
            captures = []
            for row in rows:
                if (
                    row.get("state") != "completed"
                    or row.get("status") != 200
                    or row.get("transport_error")
                ):
                    continue
                case = row["case_id"]
                require(Path(case).name == case, "Invalid authorized capture case")
                request = (root / case / "request.body").read_bytes()
                response = (root / case / "response.body").read_bytes()
                require(
                    hashlib.sha256(request).hexdigest() == row["request_sha256"]
                    and hashlib.sha256(response).hexdigest() == row["response_sha256"],
                    "Authorized capture hash mismatch",
                )
                captures.append((row, json.loads(request), json.loads(response)))
            successful = {}
            for row, request, response in captures:
                if (
                    not row["url"].endswith("/exchange")
                    or request.get("action", {}).get("type") != "order"
                ):
                    continue
                if response.get("status") != "ok":
                    continue
                orders = request["action"]["orders"]
                statuses = (
                    response.get("response", {}).get("data", {}).get("statuses", [])
                )
                if not any(
                    isinstance(status, dict) and status.get("filled")
                    for status in statuses
                ):
                    continue
                require(
                    len(orders) == len(statuses),
                    "Authorized order/status cardinality mismatch",
                )
                signer = row.get("parameters", {}).get("signer", "").lower()
                for order, status in zip(orders, statuses):
                    filled = status.get("filled") if isinstance(status, dict) else None
                    if filled:
                        require(
                            signer
                            and order.get("c")
                            and isinstance(filled.get("oid"), int),
                            "Authorized filled order lacks signer/cloid/OID",
                        )
                        successful[(signer, filled["oid"], order["c"])] = row["case_id"]
            for row, request, response in captures:
                if (
                    not row["url"].endswith("/info")
                    or request.get("type") != "userFills"
                ):
                    continue
                signer = request.get("user", "").lower()
                actors = [
                    actor
                    for actor, wallet_ in self.actors.items()
                    if wallet_.address.lower() == signer
                ]
                if not actors:
                    continue
                require(
                    isinstance(response, list), "Authorized fills response unavailable"
                )
                actor = actors[0]
                for fill in response:
                    key = (signer, fill.get("oid"), fill.get("cloid"))
                    if key not in successful or fill in self.baseline_fills[actor]:
                        continue
                    require(
                        fill.get("coin") != self.coin,
                        "Authorized run must not alter controlled PURR inventory",
                    )
                    self.baseline_fills[actor].append(fill)
                    self.authorized_fills.append(
                        {
                            "actor": actor,
                            "root": str(root),
                            "order_case": successful[key],
                            "fill_case": row["case_id"],
                            "fill": fill,
                        }
                    )

    def clock(self):
        return {
            "utc": timestamp(),
            "epoch_ms": time.time_ns() // 1_000_000,
            "elapsed_seconds": round(time.monotonic() - self.started, 6),
        }

    def source(self, relative):
        return json.loads((self.args.source / relative).read_text())

    def query(self, case, body):
        start = self.clock()
        response = info(self.recorder, case, body)
        end = self.clock()
        require(response.status == 200, f"Required diagnostic failed: {case}")
        return {
            "case_id": case,
            "capture_started": start,
            "capture_finished": end,
            "value": response.json,
        }

    def balance(self, state, token):
        require(
            isinstance(state, dict) and isinstance(state.get("balances"), list),
            "Invalid balances",
        )
        rows = [row for row in state["balances"] if row.get("token") == token["index"]]
        require(len(rows) <= 1, "Duplicate token balances")
        total = amount(rows[0]["total"]) if rows else Decimal(0)
        hold = amount(rows[0]["hold"]) if rows else Decimal(0)
        require(0 <= hold <= total, "Invalid hold or total")
        return {
            "present": bool(rows),
            "total": wire(total),
            "hold": wire(hold),
            "free": wire(total - hold),
        }

    def totals(self, state):
        return {name: self.balance(state, token) for name, token in self.tokens.items()}

    def observe(self, prefix):
        states = {}
        for name, signer in self.actors.items():
            item = self.query(
                prefix + "-" + name + "-balances",
                {"type": "spotClearinghouseState", "user": signer.address},
            )
            item["balances"] = self.totals(item.pop("value"))
            states[name] = item
        self.last_state = states
        self.recorder.event(
            {"state": "balance_observation", "prefix": prefix, "actors": states}
        )
        return states

    def total(self, states, actor, token):
        row = states[actor]["balances"][token]
        require(amount(row["hold"]) == 0, f"Unexpected held {token} for {actor}")
        return amount(row["total"])

    def exact(self, states, expected):
        for actor, values in expected.items():
            for token, value in values.items():
                require(
                    self.total(states, actor, token) == value,
                    f"Unexpected {actor} {token} balance; refusing unrelated funds",
                )

    def market(self, prefix, *, full=True):
        result = {
            "book": self.query(prefix + "-book", {"type": "l2Book", "coin": self.coin})
        }
        if full:
            result["contexts"] = self.query(
                prefix + "-contexts", {"type": "spotMetaAndAssetCtxs"}
            )
            result["trades"] = self.query(
                prefix + "-trades", {"type": "recentTrades", "coin": self.coin}
            )
        self.markets.append(result)
        return result

    def dust_fill(self, fill, size):
        return (
            isinstance(fill, dict)
            and fill.get("coin") == self.coin
            and fill.get("dir", "").replace(" ", "") == "SpotDustConversion"
            and fill.get("side") == "A"
            and amount(fill.get("sz", "-1")) == size
            and amount(fill.get("startPosition", "-1")) == size
            and fill.get("hash") == ZERO_HASH
            and fill.get("tid") == 0
            and amount(fill.get("fee", "-1")) == 0
            and fill.get("feeToken") == "USDC"
            and isinstance(fill.get("oid"), int)
            and isinstance(fill.get("time"), int)
            and amount(fill.get("px", "0")) > 0
        )

    def classify(self, actor, fills, baseline, *, new=False):
        require(
            isinstance(fills, list) and len(fills) < 2000,
            "Unavailable or potentially truncated fills",
        )
        additions = [fill for fill in fills if fill not in baseline]
        if not new:
            require(not additions, f"Unknown out-of-band fills for {actor}")
            return
        require(actor in ("a", "d") or not additions, "B control has new fills")
        require(len(additions) <= 1, f"Multiple unexpected fills for {actor}")
        for fill in additions:
            require(
                self.dust_fill(fill, Decimal("0.5"))
                and self.experiment_start_ms
                <= fill["time"]
                <= time.time_ns() // 1_000_000,
                f"Unknown out-of-band fill for {actor}",
            )
            previous = self.conversions.get(actor)
            require(previous is None or previous == fill, "Conversion fill changed")
            self.conversions[actor] = fill
        if len(self.conversions) == 2:
            require(
                self.conversions["a"]["oid"] == self.conversions["d"]["oid"],
                "Owned dust converted under different OIDs; record partial result, no cleanup",
            )
            require(
                self.conversions["a"]["time"] == self.conversions["d"]["time"],
                "Owned dust conversion timestamps differ; no cleanup",
            )

    def audit_ledger(self, actor, ledger):
        require(
            isinstance(ledger, list) and len(ledger) < 2000,
            "Unavailable or potentially truncated ledger",
        )
        baseline = self.initial[actor]["ledger"]
        require(isinstance(baseline, list), "Initial ledger unavailable")
        for row in ledger:
            if row in baseline:
                continue
            delta = row.get("delta", {})
            actions = [
                action
                for action in self.actions
                if action["outcome"] == "accepted"
                and action["nonce"] == delta.get("nonce")
                and actor in (action["source"], action["destination"])
            ]
            require(
                len(actions) == 1,
                f"Unknown ledger activity for {actor}; conversion credit not isolated",
            )
            action = actions[0]
            require(
                delta.get("type") == "send"
                and delta.get("token") == action["token"]
                and amount(delta.get("amount", "-1")) == amount(action["amount"])
                and delta.get("user", "").lower()
                == self.actors[action["source"]].address.lower()
                and delta.get("destination", "").lower()
                == self.actors[action["destination"]].address.lower()
                and delta.get("sourceDex") == delta.get("destinationDex") == "spot"
                and amount(delta.get("fee", "-1"))
                == amount(delta.get("nativeTokenFee", "-1"))
                == 0,
                "Observed ledger event differs from owned transfer",
            )

    def audit_fills(self, prefix, *, new=False):
        for actor, signer in self.actors.items():
            item = self.query(
                prefix + "-" + actor + "-fills",
                {"type": "userFills", "user": signer.address},
            )
            self.classify(actor, item["value"], self.baseline_fills[actor], new=new)
        for actor, fill in self.conversions.items():
            if actor not in self.statuses:
                self.statuses[actor] = self.query(
                    prefix + "-" + actor + "-dust-status",
                    {
                        "type": "orderStatus",
                        "user": self.actors[actor].address,
                        "oid": fill["oid"],
                    },
                )

    def nonce_proof(self, case, record, token):
        proofs = {}
        for actor in (record["source"], record["destination"]):
            query = self.query(
                case + "-" + actor + "-nonce-proof",
                {
                    "type": "userNonFundingLedgerUpdates",
                    "user": self.actors[actor].address,
                    "startTime": record["mutation_started"]["epoch_ms"] - 1000,
                },
            )
            require(isinstance(query["value"], list), "Ledger proof unavailable")
            rows = [
                row
                for row in query["value"]
                if row.get("delta", {}).get("nonce") == record["nonce"]
            ]
            require(
                len(rows) == 1, "Missing or duplicate transfer nonce proof; no retry"
            )
            delta = rows[0]["delta"]
            require(
                delta.get("type") == "send"
                and delta.get("token") == token["name"]
                and delta.get("user", "").lower()
                == self.actors[record["source"]].address.lower()
                and delta.get("destination", "").lower()
                == self.actors[record["destination"]].address.lower()
                and delta.get("sourceDex") == "spot"
                and delta.get("destinationDex") == "spot"
                and amount(delta.get("amount", "-1")) == amount(record["amount"])
                and amount(delta.get("fee", "-1")) == 0
                and amount(delta.get("nativeTokenFee", "-1")) == 0,
                "Transfer proof differs from authorized zero-fee send",
            )
            proofs[actor] = query
        record["nonce_proof"] = proofs

    def send(self, case, source, destination, symbol, quantity, *, rejected=False):
        token = self.tokens[symbol]
        require(quantity > 0 and quantity.is_finite(), "Invalid transfer amount")
        if symbol == "PURR":
            require(
                quantity <= 13 and self.gross + quantity <= 50,
                "PURR transfer budget exceeded",
            )
        else:
            cap = (
                OLD_CREDITS["d"]
                if self.phase == "old_recovery"
                else self.conversion_credit.get("d", Decimal(0))
            )
            require(
                source == "d" and destination == "a" and quantity <= cap,
                "USDC return exceeds proven D conversion credit",
            )
        before = self.observe(case + "-before")
        self.exact(before, self.expected)
        if not rejected:
            require(
                self.total(before, source, symbol) >= quantity,
                "Insufficient authorized inventory",
            )
        nonce = reserve_nonce(
            self.args.nonce_file, self.actors[source].address, TESTNET, case
        )
        action = {
            "type": "sendAsset",
            "destination": self.actors[destination].address.lower(),
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": token["name"] + ":" + token["tokenId"],
            "amount": wire(quantity),
            "fromSubAccount": "",
            "nonce": nonce,
        }
        signature = sign_send_asset_action(self.actors[source], action, False)
        envelope = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        if symbol == "PURR":
            self.gross += quantity
        record = {
            "case_id": case,
            "source": source,
            "destination": destination,
            "token": symbol,
            "amount": wire(quantity),
            "nonce": nonce,
            "mutation_started": self.clock(),
            "outcome": "ambiguous",
        }
        self.actions.append(record)
        response = submit(
            self.recorder,
            case,
            envelope,
            {
                "signer": self.actors[source].address.lower(),
                "intended_mutation": self.phase,
                "automatic_retry": False,
                "per_send_purr_cap": "13",
                "cumulative_purr_cap": "50",
                "cumulative_purr_reserved": wire(self.gross),
                "activation_allowed": False,
            },
            True,
        )
        record.update(
            {
                "mutation_finished": self.clock(),
                "http_status": response.status,
                "response": response.json,
            }
        )
        require(
            response.status == 200 and isinstance(response.json, dict),
            "Unknown exchange outcome",
        )
        if rejected:
            require(
                response.json.get("status") == "err"
                and isinstance(response.json.get("response"), str)
                and "insufficient" in response.json["response"].lower(),
                "One-wei send did not produce expected insufficient-balance rejection",
            )
            record["outcome"] = "rejected_insufficient"
            self.spendability[source] = record
        else:
            require(
                response.json == {"status": "ok", "response": {"type": "default"}},
                "Transfer not accepted",
            )
            record["outcome"] = "accepted"
            self.expected[source][symbol] -= quantity
            self.expected[destination][symbol] += quantity
        after = self.observe(case + "-after")
        if self.phase == "new_setup":
            self.audit_fills(case + "-setup-dust", new=True)
            require(
                not self.conversions,
                "Owned automatic conversion occurred during setup; incomplete equal-dust experiment, no cleanup",
            )
        self.exact(after, self.expected)
        if not rejected:
            self.nonce_proof(case, record, token)
        self.audit_fills(case + "-audit", new=self.phase == "new_return")
        self.recorder.event({"state": "mutation_verified", **record})
        return after

    def prepare(self):
        plan = self.source("plan.json")
        require(
            plan["actors"]
            == {name: signer.address.lower() for name, signer in self.actors.items()},
            "Signers differ from immutable source ownership",
        )
        meta = load_meta(self.recorder)
        self.tokens = {}
        for symbol in ("PURR", "USDC"):
            rows = [token for token in meta["tokens"] if token["name"] == symbol]
            require(len(rows) == 1, "Ambiguous token metadata")
            self.tokens[symbol] = rows[0]
        purr, usdc = self.tokens["PURR"], self.tokens["USDC"]
        require(
            purr == plan["token"] and purr["szDecimals"] == 0 and usdc["index"] == 0,
            "Source token or one-lot market changed",
        )
        pairs = [
            pair
            for pair in meta["universe"]
            if pair["tokens"] == [purr["index"], usdc["index"]]
        ]
        require(len(pairs) == 1, "Ambiguous PURR/USDC market")
        self.coin = pairs[0]["name"]
        self.quote_wei = Decimal(1).scaleb(-usdc["weiDecimals"])
        self.purr_wei = Decimal(1).scaleb(-purr["weiDecimals"])
        for actor, signer in self.actors.items():
            self.initial[actor] = snapshot(
                self.recorder, "fresh-" + actor, signer.address
            )
            require(
                self.initial[actor]["role"] == {"role": "user"},
                "Existing ordinary users required; no activation",
            )
            self.old[actor] = {
                stage: {
                    field: self.source(f"{stage}-{actor}-{field}/response.body")
                    for field in ("balances", "fills")
                }
                for stage in ("initial", "final")
            }
            self.baseline_fills[actor] = list(self.old[actor]["final"]["fills"])
            self.expected[actor] = {
                symbol: amount(row["total"])
                for symbol, row in self.totals(self.initial[actor]["balances"]).items()
            }
        self.authorize_runs()
        # Current books/contexts are diagnostic, not retrospective snapshots of the old execution.
        diagnostic = self.market("old-time-diagnostic")
        trades = diagnostic["trades"]["value"]
        require(isinstance(trades, list), "Recent trades unavailable")
        self.old_analysis["historical_market_gap"] = {
            "requested_execution_time_ms": OLD_TIME,
            "requested_execution_utc": utc_ms(OLD_TIME),
            "recent_trades_in_old_minute": [
                trade
                for trade in trades
                if OLD_TIME - 60000 <= trade.get("time", 0) <= OLD_TIME + 60000
            ],
            "limitation": "recentTrades has no historical range contract; fresh book/contexts cannot establish old book depth or network dust",
        }
        for actor in self.actors:
            initial, final = self.old[actor]["initial"], self.old[actor]["final"]
            additions = [
                fill for fill in final["fills"] if fill not in initial["fills"]
            ]
            if actor in OLD_SIZES:
                require(
                    len(additions) == 1
                    and self.dust_fill(additions[0], OLD_SIZES[actor])
                    and additions[0]["oid"] == OLD_OID
                    and additions[0]["time"] == OLD_TIME,
                    "Old automatic conversion provenance differs",
                )
                credit = amount(
                    self.balance(final["balances"], usdc)["total"]
                ) - amount(self.balance(initial["balances"], usdc)["total"])
                require(
                    credit == OLD_CREDITS[actor],
                    "Old USDC credit not proven by initial/final delta",
                )
                self.query(
                    "old-" + actor + "-dust-status",
                    {
                        "type": "orderStatus",
                        "user": self.actors[actor].address,
                        "oid": OLD_OID,
                    },
                )
                self.query(
                    "old-" + actor + "-time-fills",
                    {
                        "type": "userFillsByTime",
                        "user": self.actors[actor].address,
                        "startTime": OLD_TIME - 1,
                        "endTime": OLD_TIME + 1,
                    },
                )
            else:
                require(not additions, "Old B control had unexpected fills")
            self.classify(
                actor, self.initial[actor]["fills"], self.baseline_fills[actor]
            )
        old_b = amount(self.balance(self.old["b"]["final"]["balances"], purr)["total"])
        require(
            old_b == Decimal("12.5"),
            "Old B inventory differs from authorized 12.5 PURR",
        )
        fresh = self.observe("fresh-guard")
        self.exact(fresh, self.expected)
        require(
            self.total(fresh, "b", "PURR") == old_b
            and self.total(fresh, "a", "PURR") == self.total(fresh, "d", "PURR") == 0,
            "Current full PURR inventory differs from old final state",
        )
        require(
            self.total(fresh, "d", "USDC")
            == amount(self.balance(self.old["d"]["final"]["balances"], usdc)["total"]),
            "D USDC changed since proven old credit; no recovery",
        )
        rate = amount(self.initial["a"]["fees"]["userSpotCrossRate"])
        require(
            rate == Decimal("0.0007")
            and all(
                amount(self.initial[name]["fees"]["userSpotCrossRate"]) == rate
                for name in ("b", "d")
            ),
            "Known fee profile changed",
        )
        self.rate = rate
        net = Decimal("4.5795") * (1 - rate)
        d_share = (net * OLD_SIZES["d"] / sum(OLD_SIZES.values())).quantize(
            self.quote_wei, rounding=ROUND_FLOOR
        )
        self.old_analysis["reconciliation"] = {
            "owned_dust": "1.49019",
            "floor_lots": "1",
            "fill_price": "4.5795",
            "gross": "4.5795",
            "fee_rate": wire(rate),
            "fee": "0.00320565",
            "net": wire(net),
            "d_floor_share": wire(d_share),
            "a_remainder": wire(net - d_share),
            "matches_both_credits": d_share == OLD_CREDITS["d"]
            and net - d_share == OLD_CREDITS["a"],
            "time_ms_into_utc_minute": OLD_TIME % 60000,
            "scope": "Known-price reconciliation, not proof that no other network dust participated",
        }
        create(
            self.recorder.root / "plan.json",
            encode(
                {
                    "source": str(self.args.source),
                    "actors": plan["actors"],
                    "controlled_new_purr": "12.5",
                    "new_dust": {"a": "0.5", "d": "0.5"},
                    "control_b": "11.5",
                    "per_send_purr_cap": "13",
                    "cumulative_purr_cap": "50",
                    "poll_seconds": 5,
                    "poll_deadline_seconds": 180,
                    "automatic_retry": False,
                    "automatic_cleanup_on_abort_or_timeout": False,
                }
            ),
        )

    def compare(self, final_states):
        observed = {
            actor: self.total(final_states, actor, "USDC") - self.credit_baseline[actor]
            for actor in ("a", "d")
        }
        require(
            all(value > 0 for value in observed.values()),
            "Missing positive conversion credits",
        )
        self.conversion_credit = observed
        conversion_time = self.conversions["a"]["time"]
        for market in self.markets:
            book = market["book"]
            if book["capture_started"]["epoch_ms"] < self.experiment_start_ms:
                continue
            levels = book["value"].get("levels", [])
            if not levels or not levels[0]:
                self.comparisons.append(
                    {"case_id": book["case_id"], "gap": "Empty bid book"}
                )
                continue
            remaining, gross = Decimal(1), Decimal(0)
            for level in levels[0]:
                take = min(remaining, amount(level["sz"]))
                gross += take * amount(level["px"])
                remaining -= take
                if not remaining:
                    break
            if remaining:
                self.comparisons.append(
                    {
                        "case_id": book["case_id"],
                        "gap": "Captured bids do not cover one lot",
                    }
                )
                continue
            fee = (gross * self.rate).quantize(self.quote_wei, rounding=ROUND_FLOOR)
            # Report the measured binary64 fee alternative independently, not fitted to credits.
            float_fee = (
                Decimal(
                    int(
                        float(gross)
                        * float(self.rate)
                        * 10 ** self.tokens["USDC"]["weiDecimals"]
                    )
                )
                * self.quote_wei
            )
            models = []
            for label, candidate_fee in (
                ("decimal_floor", fee),
                ("binary64_floor", float_fee),
            ):
                net = gross - candidate_fee
                gross_d = (gross / 2).quantize(self.quote_wei, rounding=ROUND_FLOOR)
                net_d = (net / 2).quantize(self.quote_wei, rounding=ROUND_FLOOR)
                models.append(
                    {
                        "model": label,
                        "fee": wire(candidate_fee),
                        "net": wire(net),
                        "gross_proportions": {
                            "a": wire(gross - gross_d),
                            "d": wire(gross_d),
                        },
                        "net_proportions_d_floor_a_remainder": {
                            "a": wire(net - net_d),
                            "d": wire(net_d),
                        },
                        "gross_matches": observed
                        == {"a": gross - gross_d, "d": gross_d},
                        "net_matches": observed == {"a": net - net_d, "d": net_d},
                        "observed_sum_minus_gross": wire(
                            sum(observed.values()) - gross
                        ),
                        "observed_sum_minus_net": wire(sum(observed.values()) - net),
                        "a_residual": wire(observed["a"] - (net - net_d)),
                        "d_residual": wire(observed["d"] - net_d),
                    }
                )
            self.comparisons.append(
                {
                    "case_id": book["case_id"],
                    "capture_started": book["capture_started"],
                    "capture_finished": book["capture_finished"],
                    "server_book_time": book["value"].get("time"),
                    "conversion_time_ms": conversion_time,
                    "owned_input_purr": "1",
                    "floor_lots": "1",
                    "gross_from_captured_bids": wire(gross),
                    "models": models,
                }
            )
        require(
            bool(self.comparisons), "No independent captured-book comparison available"
        )

    def run(self):
        self.prepare()
        self.phase = "old_spendability"
        for actor in ("a", "d"):
            self.send(
                "old-spend-one-wei-" + actor,
                actor,
                "b",
                "PURR",
                self.purr_wei,
                rejected=True,
            )
        self.phase = "old_recovery"
        self.send("recover-old-b-purr", "b", "a", "PURR", Decimal("12.5"))
        self.send("recover-old-d-credit", "d", "a", "USDC", OLD_CREDITS["d"])
        recovered = self.observe("old-recovered")
        self.exact(recovered, self.expected)
        require(
            [self.total(recovered, actor, "PURR") for actor in ("a", "b", "d")]
            == [Decimal("12.5"), Decimal(0), Decimal(0)],
            "Controlled recovery incomplete",
        )
        self.phase = "new_setup"
        self.credit_baseline = {
            actor: self.total(recovered, actor, "USDC") for actor in self.actors
        }
        self.market("new-before")
        # Start setup just after a UTC minute, giving both transfers time before the next boundary.
        wait_seconds = (60 - time.time() % 60) + 1
        self.recorder.event(
            {
                "state": "waiting_for_setup_minute",
                "seconds": wait_seconds,
                "clock": self.clock(),
            }
        )
        time.sleep(wait_seconds)
        self.experiment_start_ms = time.time_ns() // 1_000_000
        self.send("new-a-to-b", "a", "b", "PURR", Decimal(12))
        self.send("new-b-to-d", "b", "d", "PURR", Decimal("0.5"))
        origin = time.monotonic()
        self.phase = "new_poll"
        self.market("new-eligible")
        normal_interval = self.recorder.minimum_interval
        self.recorder.minimum_interval = 1.0
        self.recorder.event(
            {
                "state": "poll_request_spacing",
                "minimum_interval_seconds": 1.0,
                "scheduled_poll_seconds": 5,
                "maximum_observation_seconds": 180,
            }
        )
        previous = None
        for index in range(37):
            target = origin + 5 * index
            if time.monotonic() > origin + 180:
                break
            time.sleep(max(0, target - time.monotonic()))
            started = self.clock()
            states = self.observe(f"poll-{index:02d}")
            market = self.market(f"poll-{index:02d}", full=False)
            row = {
                "scheduled_offset_seconds": index * 5,
                "actual_offset_seconds": round(time.monotonic() - origin, 6),
                "capture_started": started,
                "capture_finished": self.clock(),
                "actors": states,
                "book_case_id": market["book"]["case_id"],
                "eligible_visible_balances": {
                    actor: states[actor]["balances"]["PURR"] for actor in ("a", "d")
                },
            }
            self.polls.append(row)
            self.recorder.event({"state": "dust_poll", **row})
            require(
                self.total(states, "b", "PURR") == Decimal("11.5")
                and self.total(states, "b", "USDC") == self.credit_baseline["b"],
                "B control changed",
            )
            changed = any(
                self.total(states, actor, "PURR") != Decimal("0.5")
                or self.total(states, actor, "USDC") != self.credit_baseline[actor]
                for actor in ("a", "d")
            )
            if changed:
                self.audit_fills(f"poll-{index:02d}-conversion", new=True)
                for actor in ("a", "d"):
                    purr = self.total(states, actor, "PURR")
                    require(
                        purr in (Decimal(0), Decimal("0.5")),
                        "Unexpected dust inventory change",
                    )
                    if purr == 0:
                        require(
                            actor in self.conversions,
                            "Omission/zero without owned conversion fill is not proof",
                        )
                if (
                    row["actual_offset_seconds"] <= 180
                    and len(self.conversions) == 2
                    and all(
                        self.total(states, actor, "PURR") == 0 for actor in ("a", "d")
                    )
                ):
                    self.conversion_window = {
                        "last_observation": previous,
                        "first_both_converted": row,
                    }
                    break
            previous = row
        self.recorder.minimum_interval = normal_interval
        self.phase = "new_final_audit"
        self.market("new-after")
        self.audit_fills("new-final", new=True)
        for actor, signer in self.actors.items():
            self.final[actor] = snapshot(
                self.recorder, "new-final-snapshot-" + actor, signer.address
            )
            self.classify(
                actor, self.final[actor]["fills"], self.baseline_fills[actor], new=True
            )
            self.audit_ledger(actor, self.final[actor]["ledger"])
        final_states = self.observe("new-final-guard")
        if not hasattr(self, "conversion_window"):
            self.phase = "timeout_no_cleanup"
            return "not_converted_within_window"
        require(
            all(self.total(final_states, actor, "PURR") == 0 for actor in ("a", "d"))
            and self.total(final_states, "b", "PURR") == Decimal("11.5"),
            "Final conversion inventory differs",
        )
        self.compare(final_states)
        self.expected = {
            actor: {
                symbol: self.total(final_states, actor, symbol)
                for symbol in self.tokens
            }
            for actor in self.actors
        }
        self.phase = "new_return"
        self.send("return-new-b-purr", "b", "a", "PURR", Decimal("11.5"))
        self.send("return-new-d-credit", "d", "a", "USDC", self.conversion_credit["d"])
        completed = self.observe("completed")
        self.exact(completed, self.expected)
        self.phase = "completed"
        return "completed"

    def report(self, status, reason=None):
        value = {
            "status": status,
            "reason": reason,
            "phase": self.phase,
            "finished": self.clock(),
            "source": str(self.args.source),
            "automatic_retry": False,
            "automatic_cleanup_on_failure": False,
            "authorized_fills": self.authorized_fills,
            "old_analysis": self.old_analysis,
            "actions": self.actions,
            "spendability": self.spendability,
            "gross_purr_reserved_including_rejections": wire(self.gross),
            "per_send_purr_cap": "13",
            "cumulative_purr_cap": "50",
            "polls": self.polls,
            "conversion_fills": self.conversions,
            "conversion_statuses": self.statuses,
            "conversion_window": getattr(self, "conversion_window", None),
            "conversion_timing": {
                actor: {
                    "utc": utc_ms(fill["time"]),
                    "ms_into_utc_minute": fill["time"] % 60000,
                }
                for actor, fill in self.conversions.items()
            },
            "observed_usdc_credits": {
                actor: wire(value) for actor, value in self.conversion_credit.items()
            },
            "independent_book_comparisons": self.comparisons,
            "last_observed_state": self.last_state,
            "gaps": [
                "Other network dust and system execution fee profile are not observable; owned one-lot model is conditional.",
                "Captured bid books are observations, not execution-time orderbook reconstruction.",
                "Sequential requests may overrun five-second scheduled ticks; exact UTC windows and offsets are recorded.",
                "No timeout, missing fill, omitted row, or unavailable historical trade is treated as a passing assumption.",
                "Minute-boundary testnet evidence does not establish mainnet daily/one-dollar policy.",
            ],
        }
        create(self.recorder.root / "results.json", encode(value))
        self.recorder.event(
            {"state": "workflow_" + status, "reason": reason, "phase": self.phase}
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--source", type=Path, default=Path("captures/testnet/dust-lifecycle-01")
    )
    parser.add_argument(
        "--authorized-run",
        type=Path,
        action="append",
        default=[],
        help="Completed immutable run explaining exact signer/cloid/OID fills; repeatable",
    )
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    args = parser.parse_args()
    if not args.execute:
        parser.error("Explicit --execute is required")
    if args.root.exists():
        parser.error("--root must not exist; captures are immutable")
    if not (args.source / "plan.json").is_file():
        parser.error("--source must contain the old immutable dust capture")
    actors = {
        "a": wallet("HYPERLIQUID_PRIVATE_KEY"),
        "b": wallet("HYPERLIQUID_B_PRIVATE_KEY"),
        "d": wallet("HYPERLIQUID_D_PRIVATE_KEY"),
    }
    if len({signer.address.lower() for signer in actors.values()}) != 3:
        parser.error("A, B, D must be distinct controlled actors")
    campaign = Campaign(args, actors, HaltRecorder(args.root, TESTNET))
    try:
        status = campaign.run()
    except (Exception, KeyboardInterrupt) as exc:
        campaign.report("aborted", str(exc))
        raise
    campaign.report(status)


if __name__ == "__main__":
    main()
