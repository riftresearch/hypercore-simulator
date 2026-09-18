"""Bounded, one-shot PURR dust lifecycle discrimination on controlled testnet users."""

import argparse
import time
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_send_asset_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET, create, encode, timestamp
from .scenarios import load_meta, reserve_nonce, snapshot, submit, wallet, wire


class HaltRecorder(Recorder):
    """Keep raw evidence, then stop even when a shared helper tolerates an error."""

    def request(self, case_id, path, body, **kwargs):
        result = super().request(case_id, path, body, **kwargs)
        if (
            result.transport_error
            or result.status is None
            or result.status == 429
            or result.status >= 500
        ):
            self.event(
                {
                    "state": "request_halted",
                    "case_id": case_id,
                    "ambiguous_action": path == "/exchange",
                    "automatic_retry": False,
                }
            )
            raise RuntimeError(
                f"Request {case_id} failed; inspect raw evidence; no further requests"
            )
        return result


def poll_plan(value):
    try:
        offsets = [int(item) for item in value.split(",")]
    except ValueError:
        raise argparse.ArgumentTypeError(
            "Use comma-separated integer seconds"
        ) from None
    if (
        len(offsets) < 2
        or len(offsets) > 32
        or offsets[0] != 0
        or offsets[-1] > 1200
        or offsets != sorted(set(offsets))
    ):
        raise argparse.ArgumentTypeError(
            "Use 2–32 strictly increasing offsets starting at 0 and ending <=1200"
        )
    return offsets


class Campaign:
    def __init__(self, args, actors, recorder):
        self.args, self.actors, self.recorder = args, actors, recorder
        self.started = time.monotonic()
        self.token = None
        self.expected = {}
        self.origins = {}
        self.observations = {name: [] for name in actors}
        self.actions = []
        self.spendability = {}
        self.returns = {}
        self.gross_sent = Decimal(0)
        self.gross_reserved = Decimal(0)
        self.initial = {}
        self.final_audit = {}
        self.poll_origin = None

    def clock(self):
        return {
            "utc": timestamp(),
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
        }

    def balance(self, state):
        if not isinstance(state, dict) or not isinstance(state.get("balances"), list):
            raise TypeError("Invalid balance response; no further mutation")
        rows = [
            row for row in state["balances"] if row.get("token") == self.token["index"]
        ]
        if len(rows) > 1:
            raise RuntimeError("Duplicate PURR balance rows")
        row = rows[0] if rows else None
        total = Decimal(row["total"]) if row else Decimal(0)
        hold = Decimal(row["hold"]) if row else Decimal(0)
        if not total.is_finite() or not hold.is_finite() or not 0 <= hold <= total:
            raise RuntimeError("Invalid PURR balance values")
        return {
            "present": row is not None,
            "total": wire(total),
            "hold": wire(hold),
            "free": wire(total - hold),
        }

    def observe(self, prefix, *, track=False, scheduled=None):
        result = {}
        for name, signer in self.actors.items():
            case = f"{prefix}-{name}-balances"
            start = self.clock()
            response = info(
                self.recorder,
                case,
                {"type": "spotClearinghouseState", "user": signer.address},
            )
            end = self.clock()
            if response.status != 200:
                raise RuntimeError(f"Required balance query {case} failed")
            item = {
                "case_id": case,
                "capture_started": start,
                "capture_finished": end,
                **self.balance(response.json),
            }
            if scheduled is not None:
                item["scheduled_offset_seconds"] = scheduled
                item["actual_poll_elapsed_seconds"] = round(
                    end["elapsed_seconds"] - self.poll_origin, 3
                )
            result[name] = item
            if track and name in self.origins:
                self.observations[name].append(item)
            self.recorder.event({"state": "balance_observed", "actor": name, **item})
        return result

    def send(self, case, source, destination, quantity, *, purpose, track=True):
        if (
            not quantity.is_finite()
            or not 0 < quantity <= self.args.max_inventory
            or quantity > self.expected[source]
        ):
            raise RuntimeError("Transfer exceeds controlled probe inventory")
        if self.gross_reserved + quantity > 2 * self.args.max_inventory:
            raise RuntimeError(
                "Cumulative worst-case gross transfers would exceed twice --max-inventory"
            )
        before = self.observe(case + "-before", track=track)
        # Tiny probes and omitted-balance returns deliberately must not use a display balance guard.
        if purpose == "setup" and Decimal(before[source]["free"]) < quantity:
            raise RuntimeError(
                "Setup source balance no longer funds the planned transfer"
            )
        for name, balance in before.items():
            if (
                Decimal(balance["total"]) > self.expected[name]
                or Decimal(balance["hold"]) != 0
            ):
                raise RuntimeError(
                    "Unexpected PURR credit or hold; halt rather than spend unrelated funds"
                )
        nonce = reserve_nonce(
            self.args.nonce_file, self.actors[source].address, TESTNET, case
        )
        action = {
            "type": "sendAsset",
            "destination": self.actors[destination].address.lower(),
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": self.token["name"] + ":" + self.token["tokenId"],
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
        self.gross_reserved += quantity
        record = {
            "case_id": case,
            "source": source,
            "destination": destination,
            "amount": wire(quantity),
            "purpose": purpose,
            "nonce": nonce,
            "mutation_window_started": self.clock(),
            "outcome": "pending_or_ambiguous",
        }
        self.actions.append(record)
        self.recorder.event({"state": "mutation_attempt", **record})
        response = submit(
            self.recorder,
            case,
            envelope,
            {
                "signer": self.actors[source].address.lower(),
                "intended_mutation": f"{purpose}: controlled PURR send",
                "probe_inventory_cap": str(self.args.max_inventory),
                "per_send_cap": str(self.args.max_inventory),
                "gross_transfer_cap": str(2 * self.args.max_inventory),
                "gross_reserved": wire(self.gross_reserved),
                "amount": wire(quantity),
                "automatic_retry": False,
            },
            True,
        )
        record.update(
            {
                "mutation_window_finished": self.clock(),
                "http_status": response.status,
                "response": response.json,
            }
        )
        if response.status != 200 or not isinstance(response.json, dict):
            raise RuntimeError("Unclassified exchange outcome; no further mutation")
        accepted = response.json == {"status": "ok", "response": {"type": "default"}}
        if not accepted and response.json.get("status") != "err":
            raise RuntimeError("Unclassified exchange response; no further mutation")
        record["outcome"] = "accepted" if accepted else "rejected"
        if accepted:
            self.expected[source] -= quantity
            self.expected[destination] += quantity
            self.gross_sent += quantity
            if case == "setup-a-to-b":
                self.origins["a"] = record.copy()
            elif case == "setup-b-to-d":
                self.origins["b"] = record.copy()
                self.origins["d"] = record.copy()
                self.poll_origin = record["mutation_window_finished"]["elapsed_seconds"]
        self.recorder.event({"state": "mutation_observed", **record})
        after = self.observe(case + "-after", track=track)
        return record, before, after

    def run(self):
        meta = load_meta(self.recorder)
        tokens = [token for token in meta["tokens"] if token["name"] == "PURR"]
        if len(tokens) != 1:
            raise RuntimeError("Expected one unambiguous PURR token")
        self.token = tokens[0]
        if self.token["szDecimals"] != 0 or not 1 <= self.token["weiDecimals"] <= 18:
            raise RuntimeError(
                "This design requires one-PURR market lots and a fractional transfer wei"
            )
        self.wei = Decimal(1).scaleb(-self.token["weiDecimals"])
        for name, signer in self.actors.items():
            self.initial[name] = snapshot(
                self.recorder, f"initial-{name}", signer.address
            )
            state = self.initial[name]
            if state["role"] != {"role": "user"}:
                raise RuntimeError(
                    f"{name.upper()} must be an existing ordinary user; no activation is allowed"
                )
            if not isinstance(state["fills"], list) or not isinstance(
                state["ledger"], list
            ):
                raise TypeError("Initial fills/ledger unavailable")
            balance = self.balance(state["balances"])
            if Decimal(balance["hold"]) != 0:
                raise RuntimeError("Actors must have no held PURR")
            self.expected[name] = Decimal(balance["free"])
        if (
            not 3 <= self.expected["a"] <= self.args.max_inventory
            or self.expected["b"] != 0
            or self.expected["d"] != 0
        ):
            raise RuntimeError(
                "Require A with 3 to --max-inventory free PURR and existing B/D with zero total PURR"
            )
        quantity = self.expected["a"].to_integral_value(rounding=ROUND_FLOOR)
        residual = self.expected["a"] - quantity
        if not 0 < residual < 1:
            raise RuntimeError(
                "floor(Afree) must leave a positive sub-lot; no mutation submitted"
            )
        create(
            self.recorder.root / "plan.json",
            encode(
                {
                    "actors": {
                        name: signer.address.lower()
                        for name, signer in self.actors.items()
                    },
                    "initial_purr": {
                        name: wire(value) for name, value in self.expected.items()
                    },
                    "token": self.token,
                    "inventory_cap": str(self.args.max_inventory),
                    "per_send_cap": str(self.args.max_inventory),
                    "gross_transfer_cap": str(2 * self.args.max_inventory),
                    "a_to_b": wire(quantity),
                    "expected_a_residual": wire(residual),
                    "b_to_d": "0.5",
                    "tiny_probe": wire(self.wei),
                    "poll_seconds": self.args.poll_seconds,
                    "poll_origin": "setup-b-to-d response captured",
                    "poll_cases": [
                        f"poll-{index:02d}-{offset:04d}s"
                        for index, offset in enumerate(self.args.poll_seconds)
                    ],
                    "budget_semantics": "Original inventory and each send <=max-inventory; gross transfers including returns <=2*max-inventory",
                }
            ),
        )
        first, _, after = self.send("setup-a-to-b", "a", "b", quantity, purpose="setup")
        if first["outcome"] != "accepted":
            raise RuntimeError("A-to-B setup rejected; halt")
        if not 0 < Decimal(after["a"]["free"]) < 1 or Decimal(
            after["b"]["free"]
        ) < Decimal("1.5"):
            raise RuntimeError(
                "Post-transfer A positive sub-lot/B inventory requirement failed; halt"
            )
        second, _, after = self.send(
            "setup-b-to-d", "b", "d", Decimal("0.5"), purpose="setup"
        )
        if second["outcome"] != "accepted":
            raise RuntimeError("B-to-D setup rejected; halt")
        if Decimal(after["b"]["free"]) < 1:
            raise RuntimeError("B must retain at least one PURR lot; halt")
        # An immediately omitted D is evidence, not a reason to discard the campaign.
        for index, offset in enumerate(self.args.poll_seconds):
            time.sleep(
                max(0, self.started + self.poll_origin + offset - time.monotonic())
            )
            self.observe(
                f"poll-{index:02d}-{offset:04d}s", track=True, scheduled=offset
            )
        for name, signer in self.actors.items():
            start = self.clock()
            self.final_audit[name] = snapshot(
                self.recorder, f"final-{name}", signer.address
            )
            self.recorder.event(
                {
                    "state": "final_audit_captured",
                    "actor": name,
                    "capture_started": start,
                    "capture_finished": self.clock(),
                }
            )
            if not isinstance(self.final_audit[name]["fills"], list) or not isinstance(
                self.final_audit[name]["ledger"], list
            ):
                raise TypeError("Final fills/ledger unavailable")
        if any(
            fill not in self.initial[name]["fills"]
            for name, state in self.final_audit.items()
            for fill in state["fills"]
        ):
            raise RuntimeError(
                "New fills contaminate the controlled experiment; halt without cleanup"
            )
        for name in ("a", "d"):
            result, before, after = self.send(
                f"spend-one-wei-{name}-to-b",
                name,
                "b",
                self.wei,
                purpose="spendability",
                track=False,
            )
            self.spendability[name] = {
                "case_id": result["case_id"],
                "amount": wire(self.wei),
                "before": before[name],
                "after": after[name],
                "destination_before": before["b"],
                "destination_after": after["b"],
                "outcome": result["outcome"],
                "response": result["response"],
                "finding": (
                    "omitted_but_one_wei_send_accepted"
                    if not before[name]["present"]
                    else "displayed_and_one_wei_send_accepted"
                )
                if result["outcome"] == "accepted"
                else (
                    "omitted_and_one_wei_send_rejected"
                    if not before[name]["present"]
                    else "displayed_and_one_wei_send_rejected"
                ),
            }
        for name in ("d", "b"):
            observed = self.observe(f"return-{name}-check")
            balance = observed[name]
            quantity = Decimal(balance["free"])
            hidden_spendable = (
                not balance["present"]
                and self.spendability.get(name, {}).get("outcome") == "accepted"
            )
            if hidden_spendable or (name == "b" and not balance["present"]):
                quantity = self.expected[name]
            if quantity == 0:
                self.returns[name] = {
                    "outcome": "no_displayed_spendable_balance",
                    "observed": balance,
                    "unreturned_inventory_upper_bound": wire(self.expected[name]),
                }
                continue
            result, _, after = self.send(
                f"return-{name}-to-a",
                name,
                "a",
                quantity,
                purpose="return",
                track=False,
            )
            self.returns[name] = {
                "case_id": result["case_id"],
                "amount": wire(quantity),
                "outcome": result["outcome"],
                "response": result["response"],
                "after": after[name],
            }
        self.observe("completed")

    def report(self, status, reason=None):
        transitions = {}
        for name, origin in self.origins.items():
            observations = self.observations[name]
            previous = None
            intervals = []
            for item in observations:
                absent = not item["present"] or Decimal(item["total"]) == 0
                prior_absent = previous is not None and (
                    not previous["present"] or Decimal(previous["total"]) == 0
                )
                if absent and not prior_absent:
                    intervals.append(
                        {
                            "last_positive_case": previous["case_id"]
                            if previous
                            else None,
                            "first_zero_or_omitted_case": item["case_id"],
                            "lower_bound": previous["capture_started"]
                            if previous
                            else origin["mutation_window_started"],
                            "upper_bound": item["capture_finished"],
                            "row_omitted": not item["present"],
                            "interpretation": "observation window, not a server deletion timestamp",
                        }
                    )
                previous = item
            transitions[name] = {
                "observation": (
                    "zero_or_omission_observed"
                    if intervals
                    else "no_observed_removal"
                    if observations
                    else "no_post_mutation_observation"
                ),
                "intervals": intervals,
                "mutation": origin,
                "balances": observations,
            }
        fills = {}
        for name, final in self.final_audit.items():
            initial = self.initial[name]["fills"]
            fills[name] = (
                [fill for fill in final["fills"] if fill not in initial]
                if isinstance(final.get("fills"), list)
                else None
            )
        value = {
            "status": status,
            "reason": reason,
            "finished": self.clock(),
            "automatic_retry": False,
            "automatic_cleanup_on_failure": False,
            "transitions": transitions,
            "spendability": self.spendability,
            "returns": self.returns,
            "actions": self.actions,
            "bounds_purr": {
                "original_inventory": str(self.args.max_inventory),
                "per_send": str(self.args.max_inventory),
                "gross_including_returns": str(2 * self.args.max_inventory),
            },
            "gross_purr_reserved_including_rejected_attempts": wire(
                self.gross_reserved
            ),
            "accepted_gross_purr_moved_including_returns": wire(self.gross_sent),
            "inventory_upper_bounds": {
                name: wire(amount) for name, amount in self.expected.items()
            },
            "new_fills_in_final_response": fills,
            "interpretation": "Per-actor evidence only; omission alone does not establish deletion. Inspect raw first/final ledger and fills for outside activity and endpoint truncation.",
        }
        create(self.recorder.root / "results.json", encode(value))
        self.recorder.event({"state": "workflow_" + status, "reason": reason})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--poll-seconds", type=poll_plan, default=poll_plan("0,10,30,60,120,300,600")
    )
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    parser.add_argument(
        "--max-inventory",
        type=int,
        default=10,
        help="Original PURR inventory/per-send cap, integer 3–20 (default 10)",
    )
    args = parser.parse_args()
    if not 3 <= args.max_inventory <= 20:
        parser.error("--max-inventory must be an integer from 3 to 20")
    if not args.execute:
        parser.error("This stateful campaign requires explicit --execute")
    if args.root.exists():
        parser.error("--root must be a new path, not a previous capture directory")
    actors = {
        "a": wallet("HYPERLIQUID_PRIVATE_KEY"),
        "b": wallet("HYPERLIQUID_B_PRIVATE_KEY"),
        "d": wallet("HYPERLIQUID_D_PRIVATE_KEY"),
    }
    if len({actor.address.lower() for actor in actors.values()}) != 3:
        parser.error("A, B, and D must be distinct controlled signers")
    campaign = Campaign(args, actors, HaltRecorder(args.root, TESTNET))
    try:
        campaign.run()
    except (Exception, KeyboardInterrupt) as exc:
        campaign.report("aborted", str(exc))
        raise
    campaign.report("completed")


if __name__ == "__main__":
    main()
