"""Replay signed spot capture episodes on a disposable loopback simulator, without keys."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from urllib.parse import urlsplit

from . import Recorder
from .differential import unsigned
from .recorder import run_root, TESTNET, create, encode, validate_base_url

# A suite is one continuous stateful episode, not a bag of independent requests.
SUPPORTED = (
    "accounts-dust-admission",
    "accounts-transfer",
    "orders-cloid-batch",
    "orders-edge-contract",
    "orders-rounding-partial",
    "quote-activation",
    "matching-contract",
    "rounding-canonical",
    "refinements-aliases",
    "refinements-signed",
    "late-contract",
    "orders-multilevel",
    "recovery-chain",
)
OPTIONAL = (
    "dust-followup",
    "transfer-valuation",
    "reference-contract",
    "dust-split-asymmetric",
    "minimum-contract",
    "minimum-nq",
    "minimum-funded-02",
    "band-nq",
    "band-test8-02",
    "orders-multilevel-02",
    "orders-multilevel-04",
    "band-inside",
    "activation-priority",
    "activation-priority-usdc",
    "activation-priority-purr",
    "activation-fallback",
    "activation-alternate-gas",
    "quote-accounting",
    "fee-share-one",
    "fee-share-half",
    "quote-volume-usdyp",
)
RECOVERY_CHAIN = (
    "quote-activation",
    "orders-cloid-batch",
    "orders-edge-contract",
    "orders-rounding-partial",
    "omitted-order-defaults",
    "matching-contract",
    "rounding-canonical",
    "refinements-unsigned",
    "refinements-signed",
    "refinements-aliases",
    "dust-lifecycle-01",
)
ACCOUNT_INFO = {
    "spotClearinghouseState",
    "userRole",
    "preTransferCheck",
    "userNonFundingLedgerUpdates",
    "userFills",
    "userFees",
    "orderStatus",
}
HISTORY = {"userFills", "userNonFundingLedgerUpdates"}
AUXILIARY = {"recentTrades", "userFillsByTime"}
DYNAMIC = {
    "oid": "oid",
    "tid": "tid",
    "hash": "hash",
    "time": "time",
    "timestamp": "time",
    "statusTimestamp": "time",
}
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")


class Gap(Exception):
    """Missing captured prerequisite, never a passing comparison."""


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def milliseconds(value):
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def decoded(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None


def address(value):
    return (
        value.lower() if isinstance(value, str) and ADDRESS.fullmatch(value) else None
    )


def action_key(body):
    return encode(
        {
            key: body.get(key)
            for key in ("action", "nonce", "vaultAddress", "expiresAfter")
        }
    )


def difference(rows, baseline, grouped):
    """Exclude fixed pre-episode rows or groups; retain every observable new row."""
    if grouped:
        prior = {(row["oid"], row["time"]) for row in baseline}
        return [row for row in rows if (row["oid"], row["time"]) not in prior]
    counts = Counter(json.dumps(row, sort_keys=True) for row in baseline)
    result = []
    for row in rows:
        key = json.dumps(row, sort_keys=True)
        if counts[key]:
            counts[key] -= 1
        else:
            result.append(row)
    return result


def volume_delta(current, baseline):
    def index(rows):
        return {row["date"]: row for row in rows}

    now, before = index(current), index(baseline)
    result = {}
    with localcontext() as context:
        context.prec = 100
        for day in sorted(now.keys() | before.keys()):
            changes = {
                key: Decimal(now.get(day, {}).get(key, "0"))
                - Decimal(before.get(day, {}).get(key, "0"))
                for key in ("userCross", "userAdd")
            }
            result[day] = {
                key: str(value.normalize()) for key, value in changes.items()
            }
    return result


class Mapping:
    """Consistent entity identity, with shared timestamp domain across endpoints."""

    def __init__(self):
        self.forward = {kind: {} for kind in set(DYNAMIC.values())}
        self.reverse = {kind: {} for kind in self.forward}

    def compare(self, expected, actual, path="$", field=None):
        if field in DYNAMIC and expected is not None:
            kind = DYNAMIC[field]
            if type(expected) is not type(actual):
                return [f"{path}: dynamic value type differs"]
            if kind in ("oid", "tid", "time") and (
                not isinstance(actual, int) or actual < 0
            ):
                return [f"{path}: invalid generated integer"]
            if kind == "hash" and not (
                isinstance(actual, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", actual)
            ):
                return [f"{path}: invalid generated hash"]
            old = self.forward[kind].get(expected)
            if old is not None and old != actual:
                return [
                    f"{path}: inconsistent {kind} mapping {expected!r}: {old!r} != {actual!r}"
                ]
            # A local fixed clock can merge distinct live times, but never identities.
            owner = self.reverse[kind].get(actual)
            if kind != "time" and owner is not None and owner != expected:
                return [f"{path}: non-injective {kind} mapping"]
            self.forward[kind][expected] = actual
            self.reverse[kind][actual] = expected
            return []
        if type(expected) is not type(actual):
            return [f"{path}: expected {expected!r}, observed {actual!r}"]
        if isinstance(expected, dict):
            errors = []
            if expected.keys() != actual.keys():
                errors.append(
                    f"{path}: fields differ: expected {sorted(expected)}, observed {sorted(actual)}"
                )
            for key in expected.keys() & actual.keys():
                errors.extend(
                    self.compare(expected[key], actual[key], f"{path}.{key}", key)
                )
            return errors
        if isinstance(expected, list):
            errors = (
                []
                if len(expected) == len(actual)
                else [f"{path}: row count {len(expected)} != {len(actual)}"]
            )
            for index, (left, right) in enumerate(zip(expected, actual)):
                errors.extend(self.compare(left, right, f"{path}[{index}]"))
            return errors
        return (
            []
            if expected == actual
            else [f"{path}: expected {expected!r}, observed {actual!r}"]
        )


class Capture:
    def __init__(self, root):
        self.root = root
        manifest = (root / "manifest.jsonl").read_bytes()
        self.manifest_hash = digest(manifest)
        self.events = [json.loads(line) for line in manifest.splitlines() if line]
        self.records = []
        seen = set()
        for event in self.events:
            if event.get("state") != "completed":
                continue
            case = event["case_id"]
            if case in seen or not re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", case
            ):
                raise Gap("duplicate or unsafe source case ID")
            seen.add(case)
            folder = root / case
            raw = (folder / "request.body").read_bytes()
            response = (folder / "response.body").read_bytes()
            if (
                digest(raw) != event["request_sha256"]
                or digest(response) != event["response_sha256"]
            ):
                raise Gap(f"source integrity mismatch: {case}")
            self.records.append(
                {
                    "case": case,
                    "meta": event,
                    "raw": raw,
                    "body": decoded(raw),
                    "response": response,
                    "value": decoded(response),
                    "path": urlsplit(event["url"]).path,
                }
            )
            self.records[-1]["origin"] = self
        pending = {
            event["case_id"] for event in self.events if event.get("state") == "pending"
        } - seen
        self.incomplete = sorted(pending)
        self.finished = any(
            event.get("state") in ("scenario_completed", "workflow_completed")
            for event in self.events
        )


class Replay:
    def __init__(self, recorder, external_contexts=()):
        self.recorder = recorder
        self.sequence = 0
        self.outcomes = []
        self.suite = ""
        self.source = None
        self.external_contexts = external_contexts
        self.sources = []
        self.records = []

    def outcome(self, case, state, **details):
        row = {"suite": self.suite, "case": case, "outcome": state, **details}
        self.outcomes.append(row)
        self.recorder.event({"state": "golden_comparison", **row})

    def provenance(self, row):
        source = row["origin"]
        case = row.get("source_case", row["case"])
        return {
            "root": str(source.root),
            "case_id": case,
            "path": str(source.root / case / "response.body"),
            "request_sha256": digest(row["raw"]),
            "response_sha256": digest(row["response"]),
            "manifest_sha256": source.manifest_hash,
            "started_at": row["meta"]["started_at"],
            "finished_at": row["meta"].get("finished_at"),
        }

    def token(self, text):
        if not isinstance(text, str):
            return None
        return next(
            (
                item
                for item in self.meta["tokens"]
                if text.lower() == item["name"].lower()
                or text.rsplit(":", 1)[-1].lower() == item["tokenId"].lower()
            ),
            None,
        )

    def recipient_touched(self, action):
        token = self.token(action.get("token"))
        if token is None:
            return (
                True  # Unknown parsing/admission: do not assume recipient independence.
            )
        try:
            amount = Decimal(action["amount"])
            if not amount.is_finite():
                return True
            scaled = amount * Decimal(10) ** token["weiDecimals"]
            return amount != 0 and scaled == scaled.to_integral_value()
        except (InvalidOperation, KeyError, TypeError):
            return True

    def load(self, root):
        for capture in self.sources:
            if capture.root == root:
                return capture
        capture = Capture(root)
        self.sources.append(capture)
        self.recorder.event(
            {
                "state": "source_provenance",
                "suite": self.suite,
                "root": str(root),
                "manifest_sha256": capture.manifest_hash,
                "run_sha256": digest((root / "run.json").read_bytes()),
            }
        )
        if capture.incomplete or not capture.finished:
            self.outcome(
                root.name + "/source-completion",
                "gap",
                category="transport-evidence",
                reason="source recording incomplete or lacks completion event",
                pending=capture.incomplete,
            )
            for case in capture.incomplete:
                self.outcome(
                    root.name + "/" + case, "skipped", reason="pending source request"
                )
        return capture

    def episode(self, name, root):
        self.sources = []
        self.records = []
        if name == "recovery-chain":
            for member in (
                "accounts-dust-admission",
                "accounts-transfer",
                "accounts-nonce-floor",
                *RECOVERY_CHAIN,
                "late-contract",
            ):
                capture = self.load(root.parent / member)
                for original in capture.records:
                    if member == "late-contract" and not original["case"].startswith(
                        "recovery-"
                    ):
                        continue
                    row = {
                        **original,
                        "source_case": original["case"],
                        "case": member + "/" + original["case"],
                    }
                    self.records.append(row)
            dust_recovery = self.load(root.parent / "dust-followup-02")
            self.records.extend(
                {
                    **row,
                    "source_case": row["case"],
                    "case": "dust-followup-02/" + row["case"],
                }
                for row in dust_recovery.records
                if row["case"] in ("old-a-dust-status", "old-d-dust-status")
            )
            self.source = self.sources[0]
        else:
            self.source = self.load(root)
            if name == "refinements-signed":
                for member in (
                    "accounts-dust-admission",
                    "accounts-transfer",
                    "accounts-nonce-floor",
                ):
                    prerequisite = self.load(root.parent / member)
                    if prerequisite.incomplete or not prerequisite.finished:
                        raise Gap(f"nonce prerequisite capture is incomplete: {member}")
                    for row in prerequisite.records:
                        self.records.append(
                            {
                                **row,
                                "source_case": row["case"],
                                "case": "nonce-setup/" + member + "/" + row["case"],
                                "setup": True,
                            }
                        )
            self.records.extend(self.source.records)
            if name in ("quote-activation", "rounding-canonical"):
                recovery = self.load(root.parent / "late-contract")
                if recovery.incomplete or not recovery.finished:
                    raise Gap("late recovery capture is not immutable/completed")
                prefix = (
                    "recovery-quote-status-"
                    if name == "quote-activation"
                    else "recovery-rounding-status-"
                )
                self.records.extend(
                    {
                        **row,
                        "source_case": row["case"],
                        "case": "late-contract/" + row["case"],
                    }
                    for row in recovery.records
                    if row["case"].startswith(prefix)
                )
                self.outcome(
                    "recovery-snapshots",
                    "unusedcontext",
                    reason="full recovery snapshots require recovery-chain: intervening account mutations are not fixtures",
                )
            if name == "quote-volume-usdyp":
                recovery = self.load(root.parent / "quote-volume-usdyp-recovery")
                if recovery.incomplete or not recovery.finished:
                    raise Gap("quote-volume recovery is not immutable/completed")
                self.records.extend(
                    {
                        **row,
                        "source_case": row["case"],
                        "case": "quote-volume-usdyp-recovery/" + row["case"],
                    }
                    for row in recovery.records
                )
        self.records.sort(key=lambda row: milliseconds(row["meta"]["started_at"]))
        return self.records

    def normalize_book(self, row, pair):
        book = deepcopy(row["value"])
        token = next(
            item for item in self.meta["tokens"] if item["index"] == pair["tokens"][0]
        )
        with localcontext() as context:
            context.prec = 100
            lot = Decimal(10) ** -token["szDecimals"]
            for side, levels in enumerate(book["levels"]):
                for index, level in enumerate(levels):
                    size = Decimal(level["sz"])
                    nearest = (size / lot).to_integral_value() * lot
                    if size == nearest:
                        continue
                    count = level.get("n")
                    binary = float(size)
                    if (
                        type(count) is not int
                        or count < 1
                        or not math.isfinite(binary)
                        or size <= 0
                    ):
                        raise Gap(
                            "non-lot depth lacks finite positive size and aggregation count"
                        )
                    bound = Decimal(count) * Decimal.from_float(math.ulp(binary))
                    error = abs(size - nearest)
                    # Unique lot inside the permitted summation-error interval.
                    if error > bound or bound >= lot - error or nearest <= 0:
                        raise Gap(
                            f"depth size {size} is genuine excess precision or has no unique bounded lot"
                        )
                    level["sz"] = format(nearest, "f")
                    self.recorder.event(
                        {
                            "state": "book_lot_normalization",
                            "suite": self.suite,
                            "source": self.provenance(row),
                            "json_path": f"$.levels[{side}][{index}].sz",
                            "original": str(size),
                            "normalized": level["sz"],
                            "lot": str(lot),
                            "n": count,
                            "error": str(error),
                            "bound_n_ulp": str(bound),
                        }
                    )
        return book

    def historical_marks(self, contexts, dependencies):
        groups = {}
        known = {pair["name"] for pair in self.meta["universe"]}
        for row in contexts:
            if (
                row["meta"]["status"] != 200
                or row["meta"].get("transport_error")
                or not isinstance(row["value"], list)
            ):
                raise Gap("independent historical trade observation unavailable")
            used = 0
            for index, fill in enumerate(row["value"]):
                if (
                    not isinstance(fill, dict)
                    or fill.get("coin") not in known & dependencies
                ):
                    continue
                timestamp = fill.get("time")
                try:
                    price = Decimal(fill["px"])
                except (InvalidOperation, KeyError, TypeError):
                    raise Gap("historical spot trade has invalid price")
                if (
                    type(timestamp) is not int
                    or timestamp < 0
                    or not price.is_finite()
                    or price <= 0
                ):
                    raise Gap("historical spot trade has invalid timestamp/price")
                body = row["body"]
                if (
                    not body.get("startTime", 0)
                    <= timestamp
                    <= body.get("endTime", float("inf"))
                ):
                    raise Gap(
                        "historical spot trade is outside its recorded query interval"
                    )
                group = groups.setdefault((timestamp, fill["coin"]), [])
                group.append(
                    {
                        "price": price,
                        "value": fill["px"],
                        "hash": fill.get("hash"),
                        "oid": fill.get("oid"),
                        "row": row,
                        "index": index,
                    }
                )
                used += 1
            self.outcome(
                row["case"],
                "auxiliary" if used else "unusedcontext",
                reason="retrospective spot mark evidence only; maker/account-specific feed may omit intervening trades; no fills or balances imported",
                source=self.provenance(row),
                dependency_events=used,
            )
        # Equal timestamps are a group, never ordered by non-monotonic tid/hash.
        return [
            (timestamp, coin, events)
            for (timestamp, coin), events in sorted(groups.items())
        ]

    def install_historical_mark(self, group, marks, action, applied_at):
        timestamp, coin, events = group
        previous = marks.get(coin)
        if previous and previous["captured_ms"] >= timestamp:
            return  # Explicit context wins ties; stale events never rewind marks.
        evidence = [
            {
                "source": self.provenance(event["row"]),
                "json_path": f"$[{event['index']}]",
                "time": timestamp,
                "hash": event["hash"],
                "oid": event["oid"],
            }
            for event in events
        ]
        own_hashes = action.get("execution_hashes", set())
        own_oids = action.get("execution_oids", set())
        own = any(
            event["hash"] in own_hashes or event["oid"] in own_oids for event in events
        )
        ambiguous = len({event["price"] for event in events}) != 1
        if own or ambiguous:
            marks[coin] = {
                "captured_ms": timestamp,
                "ambiguous": True,
                "source": self.provenance(events[0]["row"]),
                "events": evidence,
            }
            self.outcome(
                action["case"],
                "gap",
                category="external-evidence",
                coin=coin,
                reason="historical event belongs to tested execution"
                if own
                else "different historical prices share a timestamp; their last-event order is unknown",
                events=evidence,
            )
            return
        event = events[0]
        self.control(
            "historical-mark",
            "/_test/book",
            {"coin": coin, "markPx": event["value"]},
            event["row"],
        )
        marks[coin] = {
            "value": event["value"],
            "captured_ms": timestamp,
            "source": self.provenance(event["row"]),
            "events": evidence,
            "history_incomplete": True,
        }
        self.recorder.event(
            {
                "state": "historical_mark_fixture",
                "suite": self.suite,
                "coin": coin,
                "markPx": event["value"],
                "event_time": timestamp,
                "applied_at": applied_at,
                "age_ms": applied_at - timestamp,
                "events": evidence,
                "history_imported": False,
                "limitation": "account-specific history is not a complete market feed",
            }
        )

    def install_marks(self, row, dependencies, marks, applied_at=None):
        value = row["value"]
        if (
            row["meta"]["status"] != 200
            or row["meta"].get("transport_error")
            or not isinstance(value, list)
            or len(value) != 2
        ):
            raise Gap("independent market context unavailable")
        contexts = value[1]
        used = []
        for market in contexts:
            coin = market.get("coin") if isinstance(market, dict) else None
            if coin not in dependencies or market.get("markPx") is None:
                continue
            captured = milliseconds(row["meta"]["started_at"])
            if coin in marks and marks[coin]["captured_ms"] > captured:
                continue
            self.control(
                "captured-mark",
                "/_test/book",
                {"coin": coin, "markPx": market["markPx"]},
                row,
            )
            marks[coin] = {
                "value": market["markPx"],
                "captured_ms": captured,
                "source": self.provenance(row),
            }
            used.append(coin)
        self.outcome(
            row["case"],
            "fixture" if used else "unusedcontext",
            reason="independent dependency marks only; unrelated statistics are not tested",
            markets=used,
            source=self.provenance(row),
            age_ms=None
            if applied_at is None
            else applied_at - milliseconds(row["meta"]["finished_at"]),
        )

    def install_book(self, row, dependencies, books, applied_at):
        book = row["value"]
        coin = book.get("coin") if isinstance(book, dict) else row["body"].get("coin")
        if coin not in dependencies:
            self.outcome(
                row["case"],
                "unusedcontext",
                reason="helper market outside order/transfer dependencies; not installed or tested",
                source=self.provenance(row),
            )
            return
        if (
            row["meta"]["status"] != 200
            or row["meta"].get("transport_error")
            or not isinstance(book, dict)
        ):
            self.outcome(row["case"], "gap", reason="book observation is unavailable")
            return
        captured = milliseconds(row["meta"]["started_at"])
        if coin in books and books[coin]["captured_ms"] > captured:
            self.outcome(
                row["case"],
                "unusedcontext",
                reason="newer independent book already installed",
                source=self.provenance(row),
            )
            return
        pair = next(pair for pair in self.meta["universe"] if pair["name"] == coin)
        try:
            book = self.normalize_book(row, pair)
        except Gap as exc:
            self.outcome(
                row["case"],
                "gap",
                category="external-evidence",
                reason=str(exc),
                source=self.provenance(row),
            )
            return
        self.control(
            "captured-book",
            "/_test/book",
            {"coin": coin, "bids": book["levels"][0], "asks": book["levels"][1]},
            row,
        )
        books[coin] = {
            "captured_ms": captured,
            "source": self.provenance(row),
        }
        self.outcome(
            row["case"],
            "fixture",
            reason="independent depth input, not a tested transition output",
            book_time=book.get("time"),
            source=self.provenance(row),
            age_ms=applied_at - milliseconds(row["meta"]["finished_at"]),
        )

    def call(self, label, path, body, source=None):
        self.sequence += 1
        metadata = {"replay_suite": self.suite, "purpose": label}
        if self.source:
            metadata["source_episode"] = {
                "root": str(self.source.root),
                "manifest_sha256": self.source.manifest_hash,
            }
        if source:
            metadata["source"] = self.provenance(source)
        result = self.recorder.request(
            f"{self.sequence:06d}-{re.sub(r'[^a-zA-Z0-9_.-]', '-', label[:100])}",
            path,
            body,
            metadata=metadata,
            content_type=source["meta"].get("content_type", "application/json")
            if source
            else "application/json",
        )
        if result.transport_error:
            raise Gap(f"local transport failure: {result.transport_error}")
        return result

    def control(self, label, path, body, source=None):
        result = self.call(label, path, body, source)
        if result.status != 200:
            raise Gap(
                f"required {path} control rejected: HTTP {result.status}: {result.body!r}"
            )
        return result

    def clock(self, record, now=None):
        now = milliseconds(record["meta"]["started_at"]) if now is None else now
        self.control("captured-clock", "/_test/time", {"now_ms": now}, record)

    def execution_clock(self, record, records):
        start = milliseconds(record["meta"]["started_at"])
        if record["path"] != "/exchange" or not isinstance(record["body"], dict):
            return start
        body = record["body"]
        action = body.get("action", {})
        response = record["value"]
        if (
            not isinstance(action, dict)
            or not isinstance(response, dict)
            or response.get("status") != "ok"
        ):
            return start  # Rejected/replayed envelopes have no new execution identity.
        finish = milliseconds(record["meta"]["finished_at"])
        signer = record.get("signer")
        candidates = []
        oids, hashes = set(), set()
        if action.get("type") == "order" and isinstance(response, dict):
            result = response.get("response")
            if isinstance(result, dict):
                for status in result.get("data", {}).get("statuses", []):
                    if isinstance(status, dict):
                        for kind in ("filled", "resting"):
                            if (
                                isinstance(status.get(kind), dict)
                                and type(status[kind].get("oid")) is int
                            ):
                                oids.add(status[kind]["oid"])
        response_oids = set(oids)
        cloids = {
            order["c"].lower()
            for order in action.get("orders", [])
            if isinstance(order, dict) and isinstance(order.get("c"), str)
        }
        observations = [
            row
            for row in records
            if row["path"] == "/info"
            and isinstance(row["body"], dict)
            and row["meta"]["status"] == 200
            and not row["meta"].get("transport_error")
            and address(row["body"].get("user")) == signer
            and signer is not None
        ]

        def candidate(row, timestamp, identity, path):
            if type(timestamp) is int and timestamp >= 0:
                candidates.append(
                    {
                        "time": timestamp,
                        "identity": identity,
                        "source": self.provenance(row),
                        "json_path": path,
                    }
                )

        # Resolve order identity first; economics never participate in correlation.
        if action.get("type") == "order":
            for row in observations:
                if row["body"].get("type") != "orderStatus" or not isinstance(
                    row["value"], dict
                ):
                    continue
                status = row["value"].get("order")
                order = status.get("order") if isinstance(status, dict) else None
                if not isinstance(order, dict):
                    continue
                # A terminal cloid can be reused: only this request's interval
                # can establish a new identity when the response omits its OID.
                timestamp = status.get("statusTimestamp")
                by_cloid = (
                    not response_oids
                    and type(timestamp) is int
                    and start <= timestamp <= finish
                    and (
                        (order.get("cloid") or "").lower() in cloids
                        or str(row["body"].get("oid", "")).lower() in cloids
                    )
                )
                if order.get("oid") in oids or by_cloid:
                    if type(order.get("oid")) is int:
                        oids.add(order["oid"])
                    candidate(
                        row,
                        status.get("statusTimestamp"),
                        {"oid": order.get("oid"), "cloid": order.get("cloid")},
                        "$.order.statusTimestamp",
                    )
        for row in observations:
            if not isinstance(row["value"], list):
                continue
            kind = row["body"].get("type")
            for index, item in enumerate(row["value"]):
                if not isinstance(item, dict):
                    continue
                if (
                    action.get("type") == "sendAsset"
                    and kind == "userNonFundingLedgerUpdates"
                    and response == {"status": "ok", "response": {"type": "default"}}
                ):
                    delta = item.get("delta", {})
                    nonce = action.get("nonce", body.get("nonce"))
                    if (
                        delta.get("type") == "send"
                        and address(delta.get("user")) == signer
                        and delta.get("nonce") == nonce
                    ):
                        candidate(
                            row,
                            item.get("time"),
                            {"signer": signer, "nonce": nonce},
                            f"$[{index}].time",
                        )
                elif (
                    action.get("type") == "order"
                    and kind in ("userFills", "userFillsByTime")
                    and item.get("oid") in oids
                ):
                    candidate(
                        row, item.get("time"), {"oid": item["oid"]}, f"$[{index}].time"
                    )
                    if item.get("hash") is not None:
                        hashes.add(item["hash"])
        record["execution_oids"], record["execution_hashes"] = oids, hashes
        times = {item["time"] for item in candidates}
        aligned = len(times) == 1 and start <= next(iter(times)) <= finish
        now = next(iter(times)) if aligned else start
        reason = (
            "unique native execution timestamp inside recorded HTTP interval"
            if aligned
            else "missing native execution timestamp; retaining request-start clock"
            if not times
            else "ambiguous native execution timestamps"
            if len(times) > 1
            else "native execution timestamp outside recorded HTTP interval"
        )
        self.recorder.event(
            {
                "state": "execution_clock_alignment",
                "suite": self.suite,
                "case": record["case"],
                "source": self.provenance(record),
                "http_interval_ms": [start, finish],
                "now_ms": now,
                "aligned": aligned,
                "reason": reason,
                "candidates": candidates,
            }
        )
        if times and not aligned:
            self.outcome(
                record["case"],
                "gap",
                category="execution-time-evidence",
                reason=reason,
                candidates=candidates,
                http_interval_ms=[start, finish],
                source=self.provenance(record),
            )
        return now

    def compare(self, record, actual, expected=None, observed=None):
        errors = []
        if actual.status != record["meta"]["status"]:
            errors.append(f"HTTP status {record['meta']['status']} != {actual.status}")
        left = record["value"] if expected is None else expected
        right = actual.json if observed is None else observed
        # Error strings/envelopes are never normalized, including HTTP-200 action errors.
        if record["meta"]["status"] != 200 or left is None:
            if record["response"] != actual.body:
                errors.append(
                    f"exact error bytes differ: {record['response']!r} != {actual.body!r}"
                )
        else:
            errors.extend(self.mapping.compare(left, right))
        causes = []
        body = record["body"] if isinstance(record["body"], dict) else {}
        user = address(body.get("user"))
        if errors and all(
            ".usdcValue:" in error or ".entryNtl:" in error for error in errors
        ):
            causes = list(self.unknown_valuation.get(user, []))
        if errors and body.get("type") in ("spotClearinghouseState", "userFills"):
            causes.extend(self.unknown_dust.get(user, []))
        if errors and all(".error:" in error for error in errors):
            causes.extend(record.get("market_causes", []))
        if (
            errors
            and body.get("type") == "orderStatus"
            and all(
                error.startswith(("$.order.status:", "$.status:", "$: fields differ:"))
                for error in errors
            )
        ):
            causes.extend(record.get("market_causes", []))
        if causes:
            self.outcome(
                record["case"],
                "gap",
                category="external-evidence",
                reason="comparison depends on non-atomic or missing external context; exact failure retained",
                fixture_causes=causes,
                source=self.provenance(record),
            )
        self.outcome(
            record["case"],
            "mismatch" if errors else "fixture" if record.get("setup") else "passed",
            errors=errors,
            category="fixture-dependent-comparison"
            if causes
            else "observed-comparison"
            if errors
            else "comparison",
            fixture_causes=causes,
            source=self.provenance(record),
        )

    def seed(self, records):
        signed = [row for row in records if row["path"] == "/exchange"]
        known = {}
        for row in signed:
            signer = address(row["meta"].get("parameters", {}).get("signer"))
            if signer and isinstance(row["body"], dict):
                known[action_key(row["body"])] = signer
        first_touch = {}
        for index, row in enumerate(records):
            if (
                row["path"] != "/exchange"
                or not isinstance(row["body"], dict)
                or not isinstance(row["body"].get("signature"), dict)
            ):
                continue
            signer = address(
                row["meta"].get("parameters", {}).get("signer")
            ) or known.get(action_key(row["body"]))
            nonce_source = row["meta"].get("parameters", {}).get("nonce_source")
            if nonce_source:
                original_path = (
                    row["origin"].root.parent
                    / "accounts-nonce-floor"
                    / Path(nonce_source).name
                )
                original = original_path.read_bytes()
                if digest(original) != row["meta"]["parameters"]["source_sha256"]:
                    raise Gap("nonce signed-file source hash mismatch")
                original_body = decoded(original)
                signer = signer or known.get(action_key(original_body))
            episode_signers = {
                address(item["meta"].get("parameters", {}).get("signer"))
                for item in row["origin"].records
                if item["path"] == "/exchange"
            }
            episode_signers.discard(None)
            if signer is None and len(episode_signers) == 1:
                signer = next(iter(episode_signers))
            row["signer"] = signer
            action = row["body"].get("action", {})
            # Zero/invalid-precision sends are rejected before touching a recipient.
            participants = [signer]
            if action.get("type") == "sendAsset" and self.recipient_touched(action):
                participants.append(address(action.get("destination")))
            for user in participants:
                if user:
                    first_touch.setdefault(user, index)
        initial = {}
        initial_history = {}
        for index, row in enumerate(records):
            body = row["body"]
            if row["path"] != "/info" or not isinstance(body, dict):
                continue
            user = address(body.get("user"))
            kind = body.get("type")
            if (
                user
                and kind in ACCOUNT_INFO
                and row["meta"]["status"] == 200
                and not row["meta"].get("transport_error")
                and index < first_touch.get(user, len(records))
            ):
                initial.setdefault(user, {}).setdefault(kind, row)
                if kind in HISTORY and isinstance(row["value"], list):
                    initial_history.setdefault((user, kind), []).append(row)
        for user, states in sorted(initial.items()):
            balances, role = (
                states.get("spotClearinghouseState"),
                states.get("userRole"),
            )
            if role and role["value"] == {"role": "missing"}:
                continue
            if not balances or not role:
                raise Gap(f"missing pre-touch balances/role for {user}")
            if role["value"] != {"role": "user"}:
                raise Gap(f"unsupported initial account role for {user}")
            pretransfer = states.get("preTransferCheck")
            if (
                not pretransfer
                or type(pretransfer["value"].get("userHasSentTx")) is not bool
            ):
                raise Gap(f"missing independent pre-touch userHasSentTx for {user}")
            self.recorder.event(
                {
                    "state": "account_flag_fixture",
                    "suite": self.suite,
                    "user": user,
                    "source": self.provenance(pretransfer),
                    "userHasSentTx": pretransfer["value"]["userHasSentTx"],
                }
            )
            rows = balances["value"]["balances"]
            if not rows:
                raise Gap(
                    f"existing account {user} has no visible token row; cannot invent one"
                )
            for balance in rows:
                if Decimal(balance["hold"]) != 0:
                    raise Gap("resting-order holds are outside IOC fixtures")
                self.control(
                    "seed-balance",
                    "/_test/fund",
                    {
                        "address": user,
                        "token": str(balance["token"]),
                        "amount": balance["total"],
                        "entryNtl": balance["entryNtl"],
                        "mode": "transfer",
                        "userHasSentTx": pretransfer["value"]["userHasSentTx"],
                    },
                    balances,
                )
        for user in first_touch:
            if user not in initial:
                # Invalid aliases are not addresses; genuine addresses must have evidence.
                raise Gap(f"no pre-touch snapshot for participating address {user}")
        self.initial = initial
        self.histories = {}
        self.fee_baselines = {}
        for row in records:
            body = row["body"]
            if (
                row["path"] != "/info"
                or not isinstance(body, dict)
                or body.get("type") not in HISTORY
                or row["meta"]["status"] != 200
                or not isinstance(row["value"], list)
            ):
                continue
            user, kind = address(body.get("user")), body["type"]
            key = self.history_key(body)
            if key in self.histories:
                continue
            baseline = self.history_baseline(initial_history.get((user, kind), []), key)
            if baseline is None:
                continue
            prior, grouped = baseline
            start, end = key[3:]
            expected = [item for item in prior["value"] if start <= item["time"] <= end]
            local = self.call("seed-history-baseline", "/info", row["raw"], prior)
            if local.status != 200 or not isinstance(local.json, list):
                raise Gap("cannot establish local history baseline")
            self.histories[key] = (expected, local.json, grouped)
            self.recorder.event(
                {
                    "state": "history_baseline",
                    "suite": self.suite,
                    "user": user,
                    "type": kind,
                    "request": body,
                    "source": self.provenance(prior),
                    "source_rows": len(expected),
                    "local_fixture_rows": len(local.json),
                    "history_imported": False,
                    "baseline_identity": "order/time groups"
                    if grouped
                    else "complete rows",
                }
            )

    @staticmethod
    def history_key(body):
        kind = body["type"]
        if kind == "userFills":
            return (
                address(body["user"]),
                kind,
                body.get("aggregateByTime") is True,
                0,
                float("inf"),
            )
        return (
            address(body["user"]),
            kind,
            None,
            body.get("startTime") or 0,
            body.get("endTime") if body.get("endTime") is not None else float("inf"),
        )

    @classmethod
    def history_baseline(cls, priors, key):
        # Prefer an exact pre-query over a covering range or raw-to-grouped fallback.
        for prior in sorted(
            priors, key=lambda row: cls.history_key(row["body"]) != key
        ):
            prior_key = cls.history_key(prior["body"])
            grouped = key[1] == "userFills" and key[2] and not prior_key[2]
            if (
                (key[2] != prior_key[2] and not grouped)
                or key[3] < prior_key[3]
                or key[4] > prior_key[4]
            ):
                continue
            # Aggregation precedes the raw 2000-row cap, so capped raw data can
            # omit older groups. Oldest-first ledger saturation hides new rows
            # behind unseeded history; neither is a complete comparison fixture.
            if (grouped and len(prior["value"]) >= 2000) or (
                key[1] == "userNonFundingLedgerUpdates" and len(prior["value"]) >= 500
            ):
                continue
            return prior, grouped
        return None

    def fee_snapshot(self, row):
        user = address(row["body"]["user"])
        expected = row["value"]
        # Exchange activity and schedules are external observations, not user state.
        fixture = deepcopy(expected)
        for volume in fixture.get("dailyUserVlm", []):
            volume["userCross"] = volume["userAdd"] = "0.0"
        self.control("external-fee-context", "/_test/fees", {"fees": fixture}, row)
        local = self.call(row["case"], "/info", row["raw"], row)
        if not isinstance(local.json, dict):
            self.compare(row, local)
            return
        baseline = self.fee_baselines.get(user)
        if baseline is None:
            initial = self.initial.get(user, {}).get("userFees")
            if initial is None or initial["case"] != row["case"]:
                self.outcome(
                    row["case"],
                    "gap",
                    reason="no pre-touch userFees baseline; donor history is not imported",
                )
                return
            baseline = (expected["dailyUserVlm"], local.json.get("dailyUserVlm", []))
        left, right = deepcopy(expected), deepcopy(local.json)
        left["dailyUserVlm"] = {
            "delta": volume_delta(expected["dailyUserVlm"], baseline[0]),
            "external": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in ("userCross", "userAdd")
                }
                for item in expected["dailyUserVlm"]
            ],
        }
        right["dailyUserVlm"] = {
            "delta": volume_delta(local.json.get("dailyUserVlm", []), baseline[1]),
            "external": [
                {
                    key: value
                    for key, value in item.items()
                    if key not in ("userCross", "userAdd")
                }
                for item in local.json.get("dailyUserVlm", [])
            ],
        }
        self.compare(row, local, left, right)
        self.fee_baselines[user] = (
            expected["dailyUserVlm"],
            local.json.get("dailyUserVlm", []),
        )

    def run_suite(self, name, root):
        self.suite = name
        self.mapping = Mapping()
        self.unknown_valuation = {}
        self.unknown_dust = {}
        order_causes = {}
        replayed_cloids = set()
        records = self.episode(name, root)
        self.control("reset", "/_test/reset", {})
        if not records:
            raise Gap("empty capture episode")
        if not any(row["path"] == "/exchange" for row in records):
            raise Gap("suite contains no recorded signed actions")
        meta = next(
            (
                row["value"]
                for row in records
                if isinstance(row["body"], dict)
                and row["body"].get("type") == "spotMeta"
            ),
            None,
        )
        if meta is None:
            # Some signed boundary suites intentionally omit spotMeta. Independently
            # captured metadata from another suite is allowed, never synthesized.
            metadata_paths = sorted(root.parent.glob("*/setup-spot-meta/response.body"))
            if not metadata_paths:
                raise Gap("no independent spotMeta capture")
            raw = metadata_paths[0].read_bytes()
            meta = json.loads(raw)
            self.recorder.event(
                {
                    "state": "external_metadata_source",
                    "suite": name,
                    "path": str(metadata_paths[0]),
                    "sha256": digest(raw),
                }
            )
        pairs = {pair["index"] + 10000: pair for pair in meta["universe"]}
        usdc_markets = {
            pair["tokens"][0]: pair["name"]
            for pair in meta["universe"]
            if pair["tokens"][1] == 0
        }
        self.meta = meta
        self.clock(records[0])
        self.seed(records)
        dependencies = set()
        for row in records:
            if row["path"] != "/exchange" or not isinstance(row["body"], dict):
                continue
            action = row["body"].get("action", {})
            for order in action.get("orders", []):
                pair = pairs.get(order.get("a"))
                if pair:
                    dependencies.add(pair["name"])
                    quote_market = usdc_markets.get(pair["tokens"][1])
                    if quote_market:
                        dependencies.add(quote_market)
            token = self.token(action.get("token"))
            if token and (market := usdc_markets.get(token["index"])):
                dependencies.add(market)
        self.recorder.event(
            {
                "state": "dependency_markets",
                "suite": name,
                "markets": sorted(dependencies),
            }
        )
        for index, row in enumerate(records):
            if row["path"] != "/exchange" or not isinstance(row["value"], dict):
                continue
            response = row["value"].get("response")
            if not isinstance(response, dict):
                continue
            statuses = response.get("data", {}).get("statuses", [])
            filled = [
                status["filled"]["oid"]
                for status in statuses
                if isinstance(status, dict) and isinstance(status.get("filled"), dict)
            ]
            if not filled:
                continue
            later = [
                item
                for item in records[index + 1 :]
                if item["path"] == "/info"
                and isinstance(item["body"], dict)
                and address(item["body"].get("user")) == row.get("signer")
                and item["meta"]["status"] == 200
                and not item["meta"].get("transport_error")
            ]
            observed = {item["body"].get("type") for item in later}
            missing = sorted(
                {"spotClearinghouseState", "userFills", "userFees"} - observed
            )
            terminal = set()
            for item in later:
                if item["body"].get("type") == "orderStatus" and isinstance(
                    item["value"], dict
                ):
                    order = item["value"].get("order")
                    if isinstance(order, dict) and isinstance(order.get("order"), dict):
                        terminal.add(order["order"].get("oid"))
            if missing or set(filled) - terminal:
                self.outcome(
                    row["case"],
                    "gap",
                    reason="successful IOC lacks complete recorded post-state",
                    missing_endpoints=missing,
                    missing_terminal_oids=sorted(set(filled) - terminal),
                )
        books = {}
        marks = {}
        histories = []
        external = []
        for path in self.external_contexts:
            capture = self.load(path.parent.parent)
            if not capture.finished or capture.incomplete:
                raise Gap(
                    "explicit external context is not a completed immutable capture"
                )
            context = next(
                (item for item in capture.records if item["case"] == path.parent.name),
                None,
            )
            if (
                not context
                or not isinstance(context["body"], dict)
                or not (
                    context["body"] == {"type": "spotMetaAndAssetCtxs"}
                    or (
                        context["body"].get("type") == "l2Book"
                        and set(context["body"]) == {"type", "coin"}
                    )
                    or (
                        context["body"].get("type") == "userFillsByTime"
                        and context["body"].get("aggregateByTime") is not True
                        and address(context["body"].get("user")) is not None
                    )
                )
            ):
                raise Gap(
                    f"external context is not captured raw l2Book/spotMetaAndAssetCtxs/unaggregated userFillsByTime: {path}"
                )
            if context["body"].get("type") == "userFillsByTime":
                histories.append(context)
            else:
                external.append(context)
        external.extend(
            row
            for row in records
            if isinstance(row["body"], dict)
            and row["body"].get("type") in ("l2Book", "spotMetaAndAssetCtxs")
            and not row["meta"].get("transport_error")
            and row["meta"]["status"] == 200
        )
        external.sort(key=lambda row: milliseconds(row["meta"]["finished_at"]))
        historical = self.historical_marks(histories, dependencies)
        for row in records:
            now = self.execution_clock(row, records)
            self.clock(row, now)
            body = row["body"]
            while external and milliseconds(external[0]["meta"]["finished_at"]) <= now:
                context = external.pop(0)
                if context["body"]["type"] == "l2Book":
                    self.install_book(context, dependencies, books, now)
                else:
                    self.install_marks(context, dependencies, marks, now)
            if row["path"] == "/exchange":
                latest = {}
                while historical and historical[0][0] < now:
                    event = historical.pop(0)
                    latest[event[1]] = event
                for event in latest.values():
                    self.install_historical_mark(event, marks, row, now)
            if isinstance(body, dict) and body.get("type") in AUXILIARY:
                self.outcome(
                    row["case"],
                    "auxiliary",
                    reason="recorded diagnostic outside selected simulator endpoint scope; not a parity comparison",
                    status=row["meta"]["status"],
                    transport_error=row["meta"].get("transport_error"),
                    source=self.provenance(row),
                )
                continue
            if row["meta"].get("transport_error") or row["meta"]["status"] == 429:
                self.outcome(
                    row["case"],
                    "gap",
                    category="transport-evidence",
                    reason="source transport/rate-limit failure; not semantic evidence",
                )
                continue
            if not isinstance(body, dict):
                self.compare(row, self.call(row["case"], row["path"], row["raw"], row))
                continue
            if row["path"] == "/exchange":
                try:
                    unsigned(row["raw"])
                except ValueError:
                    unauthenticated = False
                else:
                    unauthenticated = True
                if unauthenticated or not isinstance(body.get("signature"), dict):
                    self.compare(
                        row, self.call(row["case"], "/exchange", row["raw"], row)
                    )
                    continue
                action = body.get("action", {})
                if action.get("type") not in ("order", "sendAsset"):
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="only signed order/sendAsset envelopes supported",
                    )
                    continue
                if not row.get("signer"):
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="signer not established by source metadata or an unambiguous prior envelope",
                    )
                    continue
                if action["type"] == "order":
                    row["market_causes"] = []
                    for order in action.get("orders", []):
                        pair = pairs.get(order.get("a"))
                        if pair and pair["name"] not in books:
                            self.outcome(
                                row["case"],
                                "gap",
                                reason="unrecorded-market-context: no pre-action book",
                                coin=pair["name"],
                            )
                            row["market_causes"].append(
                                {
                                    "case": row["case"],
                                    "coin": pair["name"],
                                    "reason": "missing pre-action book",
                                }
                            )
                        if pair and (
                            pair["name"] not in marks
                            or marks[pair["name"]].get("ambiguous")
                        ):
                            self.outcome(
                                row["case"],
                                "gap",
                                category="external-evidence",
                                reason="unrecorded-market-context: no unambiguous independent reference mark",
                                coin=pair["name"],
                            )
                            row["market_causes"].append(
                                {
                                    "case": row["case"],
                                    "coin": pair["name"],
                                    "reason": "missing or ambiguous independent reference mark",
                                }
                            )
                        quote_index = pair["tokens"][1] if pair else 0
                        quote_coin = usdc_markets.get(quote_index)
                        if quote_index != 0 and (
                            not quote_coin
                            or quote_coin not in marks
                            or marks[quote_coin].get("ambiguous")
                        ):
                            cause = {
                                "case": row["case"],
                                "coin": quote_coin,
                                "quote_token_index": quote_index,
                                "reason": "missing or ambiguous independent quote-to-USDC mark",
                            }
                            row["market_causes"].append(cause)
                            self.outcome(
                                row["case"],
                                "gap",
                                category="external-evidence",
                                reason=cause["reason"],
                                fixture_causes=[cause],
                            )
                        if order.get("c") is not None:
                            key = row["signer"], order["c"]
                            replayed_cloids.add(key)
                            order_causes[key] = row["market_causes"]
                    if any(
                        order.get("t", {}).get("limit", {}).get("tif", "Gtc") != "Ioc"
                        for order in action.get("orders", [])
                    ):
                        self.outcome(
                            row["case"],
                            "gap",
                            reason="non-IOC action outside supported transitions",
                        )
                if row["meta"].get("parameters", {}).get("nonce_source") and not any(
                    source.root.name == "accounts-nonce-floor"
                    for source in self.sources
                ):
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="prior nonce-window episode not replayed",
                    )
                if action["type"] == "sendAsset" and row["value"] == {
                    "status": "ok",
                    "response": {"type": "default"},
                }:
                    registered = self.token(action.get("token"))
                    valuation_pair = next(
                        (
                            pair
                            for pair in meta["universe"]
                            if registered and pair["tokens"] == [registered["index"], 0]
                        ),
                        None,
                    )
                    if registered and registered["name"] != "USDC":
                        coin = valuation_pair["name"] if valuation_pair else None
                        mark = marks.get(coin)
                        cause = {
                            "case": row["case"],
                            "coin": coin,
                            "reason": "missing or ambiguous independent mark"
                            if not mark or mark.get("ambiguous")
                            else "prior mark is not atomic execution-time evidence",
                            "mark": mark,
                            "age_ms": now - mark["captured_ms"] if mark else None,
                        }
                        if not mark or mark.get("ambiguous"):
                            self.outcome(
                                row["case"],
                                "gap",
                                category="external-evidence",
                                reason="unrecorded-market-context: live non-USDC transfer valuation price is not independently captured; book midpoint/last local fill is not an oracle substitute",
                                fixture_causes=[cause],
                            )
                        participants = (
                            row.get("signer"),
                            address(action.get("destination")),
                        )
                        for user in participants:
                            if user:
                                self.unknown_valuation.setdefault(user, []).append(
                                    cause
                                )
                        # A captured one-sided book establishes ineligibility;
                        # absence of a book cannot establish either outcome.
                        amount = Decimal(action["amount"])
                        if (
                            0 < amount < Decimal(10) ** -registered["szDecimals"]
                            and coin not in books
                        ):
                            dust_cause = {
                                "case": row["case"],
                                "coin": coin,
                                "reason": "sub-lot transfer lacks an independent dust-eligibility book",
                            }
                            self.outcome(
                                row["case"],
                                "gap",
                                category="external-evidence",
                                reason=dust_cause["reason"],
                                fixture_causes=[dust_cause],
                            )
                            for user in participants:
                                if user:
                                    self.unknown_dust.setdefault(user, []).append(
                                        dust_cause
                                    )
                local = self.call(row["case"], "/exchange", row["raw"], row)
                self.compare(row, local)
                if action["type"] == "order" and isinstance(row["value"], dict):
                    response = row["value"].get("response")
                    statuses = (
                        response.get("data", {}).get("statuses", [])
                        if isinstance(response, dict)
                        else []
                    )
                    for status in statuses:
                        if isinstance(status, dict):
                            for result in ("filled", "resting"):
                                if (
                                    isinstance(status.get(result), dict)
                                    and "oid" in status[result]
                                ):
                                    order_causes[
                                        row["signer"], status[result]["oid"]
                                    ] = row.get("market_causes", [])
                continue
            kind = body.get("type")
            if kind in ("spotMetaAndAssetCtxs", "l2Book"):
                # Install only once this independent response has finished, never
                # at its earlier request start or retroactively for an action.
                if row["meta"]["status"] != 200:
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="independent context observation is unavailable",
                    )
                continue
            if kind == "spotMeta":
                self.compare(row, self.call(row["case"], "/info", row["raw"], row))
                continue
            if kind not in ACCOUNT_INFO:
                self.outcome(
                    row["case"], "gap", reason=f"unsupported observation {kind!r}"
                )
                continue
            if kind == "userFees" and row["meta"]["status"] == 200:
                self.fee_snapshot(row)
                continue
            request = row["raw"]
            if kind == "orderStatus":
                key = address(body.get("user")), body.get("oid")
                if (
                    isinstance(body.get("oid"), str)
                    and key not in replayed_cloids
                    and isinstance(row["value"], dict)
                    and row["value"].get("status") == "order"
                ):
                    self.outcome(
                        row["case"],
                        "gap",
                        category="external-evidence",
                        reason="cloid refers to an unreplayed historical action; use chronological recovery evidence",
                        source=self.provenance(row),
                    )
                    continue
                row["market_causes"] = order_causes.get(key, [])
            if kind == "orderStatus" and isinstance(body.get("oid"), int):
                mapped = self.mapping.forward["oid"].get(body["oid"])
                if mapped is None:
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="order ID refers to unrecorded/unmapped order",
                    )
                    continue
                request = {**body, "oid": mapped}
            local = self.call(row["case"], "/info", request, row)
            if (
                kind in HISTORY
                and row["meta"]["status"] == 200
                and isinstance(row["value"], list)
                and isinstance(local.json, list)
            ):
                key = self.history_key(body)
                baseline = self.histories.get(key)
                if baseline is None:
                    self.outcome(
                        row["case"],
                        "gap",
                        reason="no covering pre-touch history baseline for this query; cannot distinguish old and episode rows; post-state is never seeded",
                        request=body,
                        source=self.provenance(row),
                    )
                    continue
                self.compare(
                    row,
                    local,
                    difference(row["value"], baseline[0], baseline[2]),
                    difference(local.json, baseline[1], baseline[2]),
                )
            else:
                self.compare(row, local)
        self.recorder.event(
            {
                "state": "dynamic_mappings",
                "suite": name,
                "mappings": self.mapping.forward,
            }
        )
        for context in external:
            self.outcome(
                context["case"],
                "unusedcontext",
                reason="context finishes after all replay request starts; never used as a fixture",
                source=self.provenance(context),
            )
        for source in self.sources:
            if (
                digest((source.root / "manifest.jsonl").read_bytes())
                != source.manifest_hash
            ):
                self.outcome(
                    "source-mutated",
                    "gap",
                    reason="source manifest changed during replay",
                    root=str(source.root),
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--root",
        type=Path,
        help="Output evidence root; simulator will be reset;; defaults to a fresh temporary directory",
    )
    parser.add_argument("--source-root", type=Path, default=Path("captures/testnet"))
    parser.add_argument(
        "--suites",
        help="Comma-separated supported subset; absent defaults to all supported suites",
    )
    parser.add_argument(
        "--external-context",
        type=Path,
        action="append",
        default=[],
        help="Immutable raw l2Book, spotMetaAndAssetCtxs, or unaggregated userFillsByTime response.body; repeatable, dependency markets only",
    )
    parser.add_argument(
        "--capture",
        action="append",
        default=[],
        metavar="SUITE=PATH",
        help="Explicit immutable capture directory for a supported suite",
    )
    args = parser.parse_args()
    args.root = run_root(args.root, "golden-extended")
    base = validate_base_url(args.base_url)
    if base == TESTNET:
        parser.error("recorded replay is loopback-only")
    source_root, output_root = args.source_root.resolve(), args.root.resolve()
    if (
        output_root == source_root
        or source_root in output_root.parents
        or output_root in source_root.parents
    ):
        parser.error("output and source roots must be disjoint")
    available = (*SUPPORTED, *OPTIONAL)
    overrides = {}
    for item in args.capture:
        name, separator, path = item.partition("=")
        if (
            not separator
            or name not in available
            or name in overrides
            or name == "recovery-chain"
        ):
            parser.error("--capture requires a unique supported non-chain SUITE=PATH")
        overrides[name] = Path(path).resolve()
        if (
            output_root == overrides[name]
            or output_root in overrides[name].parents
            or overrides[name] in output_root.parents
        ):
            parser.error("capture and output roots must be disjoint")
    selected = args.suites.split(",") if args.suites else list(SUPPORTED)
    if len(selected) != len(set(selected)) or any(
        name not in available for name in selected
    ):
        parser.error("--suites must be unique names from: " + ",".join(available))
    recorder = Recorder(args.root, base)
    for path in args.external_context:
        if path.name != "response.body":
            parser.error("--external-context must name a captured response.body")
        if output_root == path.resolve() or output_root in path.resolve().parents:
            parser.error("external input cannot be inside output root")
    replay = Replay(recorder, args.external_context)
    configuration = {
        "source_root": str(source_root),
        "capture_overrides": {name: str(path) for name, path in overrides.items()},
        "external_contexts": [str(path.resolve()) for path in args.external_context],
    }
    recorder.event(
        {
            "state": "replay_plan",
            "supported_suites": list(available),
            "selected_suites": selected,
            "source_root": str(source_root),
            "configuration": configuration,
            "discovery": "explicit suite order; completed manifest order within each suite",
        }
    )
    for name in selected:
        replay.suite = name
        root = overrides.get(name, source_root / name)
        start = len(replay.outcomes)
        try:
            replay.run_suite(name, root)
        except (Gap, OSError, ValueError, KeyError, TypeError) as exc:
            replay.outcome("episode-blocked", "gap", reason=str(exc))
            visited = {row["case"] for row in replay.outcomes[start:]}
            for row in replay.records:
                if row["case"] not in visited:
                    replay.outcome(
                        row["case"],
                        "skipped",
                        reason="episode prerequisite failed; see episode-blocked",
                    )
    counts = Counter(row["outcome"] for row in replay.outcomes)
    code = 1 if counts["mismatch"] else 2 if counts["gap"] or counts["skipped"] else 0
    report = {
        "exit_code": code,
        "counts": dict(counts),
        "supported_suites": list(available),
        "selected_suites": selected,
        "configuration": configuration,
        "coverage_scope": "selected suites only"
        if args.suites
        else "default supported suites; optional runs excluded",
        "requests_recorded": replay.sequence,
        "outcomes": replay.outcomes,
        "external_evidence_gaps": [
            row for row in replay.outcomes if row["outcome"] in ("gap", "skipped")
        ],
        "observed_failures": [
            row for row in replay.outcomes if row["outcome"] == "mismatch"
        ],
        "engine_failures": [
            row
            for row in replay.outcomes
            if row["outcome"] == "mismatch"
            and row.get("category") != "fixture-dependent-comparison"
        ],
        "fixture_dependent_failures": [
            row
            for row in replay.outcomes
            if row.get("category") == "fixture-dependent-comparison"
        ],
        "normalization": {
            "identity": "bijective oid/tid/hash maps",
            "time": "consistent non-injective timestamp map",
            "book_sizes": "unique positive nearest lot within recorded n * binary64 ulp(size), input fixtures only",
            "history": "ordered multiset episode deltas; initial aggregated queries exclude pre-existing order/time groups without synthesized prices; no history seeding",
            "fees": "exact Decimal daily userCross/userAdd deltas; external schedule/global volume fixtures",
        },
    }
    create(recorder.root / "golden-extended.json", encode(report))
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key
                not in (
                    "outcomes",
                    "configuration",
                    "external_evidence_gaps",
                    "observed_failures",
                    "engine_failures",
                    "fixture_dependent_failures",
                )
            },
            indent=2,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
