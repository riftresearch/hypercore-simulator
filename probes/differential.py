"""Replay unsigned captures against loopback, reporting exact parity and coverage gaps."""

from __future__ import annotations

import argparse
import hashlib
from decimal import Decimal
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from . import Recorder
from .recorder import TESTNET, create, encode, run_root, validate_base_url

ACCOUNT_QUERIES = {
    "spotClearinghouseState",
    "preTransferCheck",
    "userNonFundingLedgerUpdates",
    "userFees",
    "userFills",
    "orderStatus",
    "userRole",
}
INVALID = object()


class Pairs(list):
    """Distinguish JSON objects from arrays without discarding duplicate keys."""


def decode(raw):
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return INVALID


def request_context(raw):
    """Classify the first JSON value; replay always preserves the original bytes."""
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.decode("utf-8").lstrip())
    except (ValueError, UnicodeDecodeError):
        return INVALID
    if isinstance(value, list) and value and isinstance(value[0], str):
        fields = {
            "spotMeta": (),
            "userRole": ("user",),
            "spotClearinghouseState": ("user",),
            "preTransferCheck": ("user", "source"),
            "userFees": ("user",),
            "userFills": ("user", "aggregateByTime"),
            "orderStatus": ("user", "oid"),
            "userNonFundingLedgerUpdates": ("user", "startTime", "endTime"),
            "l2Book": ("coin", "nSigFigs", "mantissa"),
        }.get(value[0])
        if fields is not None:
            return {"type": value[0], **dict(zip(fields, value[1:]))}
    return value


def canonical_user(value):
    if isinstance(value, str):
        match = re.fullmatch(r"(?:0x)?([0-9a-fA-F]{40})", value)
        if match:
            return "0x" + match[1].lower()
    return None


def unsigned(raw):
    """Reject potentially authorized envelopes, including duplicate signature fields."""
    try:
        text = raw.decode("utf-8")
        value, _ = json.JSONDecoder(object_pairs_hook=Pairs).raw_decode(text.lstrip())
    except (ValueError, UnicodeDecodeError):
        # Incomplete JSON cannot authorize; do not accept opaque signature material.
        if b"signature" in raw or b"\\u" in raw:
            raise ValueError(
                "Cannot establish unsigned safety for malformed signature input"
            )
        return

    def zero(value):
        return (type(value) is int and value == 0) or (
            isinstance(value, str) and re.fullmatch(r"(?:0[xX])?0+", value) is not None
        )

    def signature(value):
        if value is None:
            return
        if isinstance(value, Pairs):
            r = [v for k, v in value if k == "r"]
            s = [v for k, v in value if k == "s"]
            if not r or not s or all(zero(v) for v in r) or all(zero(v) for v in s):
                return
        elif isinstance(value, list):
            if len(value) < 2 or zero(value[0]) or zero(value[1]):
                return
        raise ValueError(
            "Refusing exchange: signature lacks a provably zero or missing scalar"
        )

    def walk(value):
        if isinstance(value, Pairs):
            for key, item in value:
                if key == "signature":
                    signature(item)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(value)
    if isinstance(value, list) and not isinstance(value, Pairs) and len(value) > 2:
        signature(value[2])


def content_types(raw):
    # Ignore informational/proxy blocks; preserve final header value spelling.
    blocks = re.split(rb"\r?\n\r?\n", raw)
    final = next(
        (block for block in reversed(blocks) if block.startswith(b"HTTP/")), b""
    )
    return [
        line.split(b":", 1)[1].strip().decode("latin-1")
        for line in final.splitlines()
        if line.split(b":", 1)[0].lower() == b"content-type"
    ]


def differences(expected, actual, path="body"):
    """Exact JSON values, including numeric types; never drop response fields."""
    if type(expected) is not type(actual):
        return [path]
    if isinstance(expected, dict):
        result = []
        for key in sorted(expected.keys() | actual.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            result.extend(
                [child]
                if key not in expected or key not in actual
                else differences(expected[key], actual[key], child)
            )
        return result
    if isinstance(expected, list):
        result = [path + "/length"] if len(expected) != len(actual) else []
        for index, (left, right) in enumerate(zip(expected, actual)):
            result.extend(differences(left, right, f"{path}/{index}"))
        return result
    return [] if expected == actual else [path]


def book_schema(value):
    if not isinstance(value, dict) or set(value) - {"spread"} != {"coin", "time", "levels"}:
        return False
    if not isinstance(value["coin"], str) or type(value["time"]) is not int:
        return False
    if "spread" in value and not isinstance(value["spread"], str):
        return False
    sides = value["levels"]
    return (
        isinstance(sides, list)
        and len(sides) == 2
        and all(
            isinstance(side, list)
            and all(
                isinstance(level, dict)
                and set(level) == {"px", "sz", "n"}
                and isinstance(level["px"], str)
                and isinstance(level["sz"], str)
                and type(level["n"]) is int
                for level in side
            )
            for side in sides
        )
    )


def raw_depth_index(source, records):
    """Same-run unaggregated two-sided l2Book snapshots by coin, with start times."""
    index = {}
    for record in records:
        if record.get("status") != 200 or record.get("transport_error"):
            continue
        directory = source / record["case_id"]
        request = request_context((directory / "request.body").read_bytes())
        if not (isinstance(request, dict) and request.get("type") == "l2Book"):
            continue
        if request.get("nSigFigs") is not None or request.get("mantissa") is not None:
            continue
        response = decode((directory / "response.body").read_bytes())
        if not book_schema(response) or not all(response["levels"]):
            continue
        started = datetime.fromisoformat(record["started_at"]).timestamp() * 1000
        index.setdefault(response["coin"], []).append(
            (started, record["case_id"], response["levels"])
        )
    return index


def nearest_raw_depth(index, coin, record):
    """The raw snapshot of `coin` closest in time to `record`, if any."""
    candidates = index.get(coin) or []
    if not candidates:
        return None
    started = datetime.fromisoformat(record["started_at"]).timestamp() * 1000
    return min(candidates, key=lambda item: abs(item[0] - started))


def depth_prefix_differences(expected, actual, depth):
    """Compare the aggregated buckets that a raw top-N snapshot fully covers.

    A bid bucket at price p sums orders in [p, p + unit); every such order is in
    the snapshot when p is at or above its deepest bid. Asks mirror this. The
    first uncovered bucket and everything deeper is reported as skipped, since
    the truncated raw feed cannot reproduce it.
    """
    failures, skipped = [], []
    for field in ("coin", "spread"):
        if expected.get(field) != actual.get(field):
            failures.append(
                {
                    "path": "body/" + field,
                    "expected": expected.get(field),
                    "actual": actual.get(field),
                }
            )
    deepest = (Decimal(depth[0][-1]["px"]), Decimal(depth[1][-1]["px"]))
    for side, limit in enumerate(deepest):
        for position, bucket in enumerate(expected["levels"][side]):
            price = Decimal(bucket["px"])
            if (price < limit) if side == 0 else (price > limit):
                skipped.append(f"body/levels/{side}[{position}:]")
                break
            actual_side = actual["levels"][side]
            observed = actual_side[position] if position < len(actual_side) else None
            if observed != bucket:
                failures.append(
                    {
                        "path": f"body/levels/{side}/{position}",
                        "expected": bucket,
                        "actual": observed,
                    }
                )
    return failures, skipped


def load_source(source):
    events = [
        json.loads(line)
        for line in (source / "manifest.jsonl").read_bytes().splitlines()
        if line.strip()
    ]
    records = [event for event in events if event.get("state") == "completed"]
    if not records:
        raise ValueError("Source manifest contains no completed request records")
    seen = set()
    for record in records:
        case = record["case_id"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", case) or case in seen:
            raise ValueError(f"Invalid or duplicate completed case ID: {case!r}")
        seen.add(case)
    pending = sorted(
        {event["case_id"] for event in events if event.get("state") == "pending"} - seen
    )
    return records, pending


def fresh_accounts(source, records, explicit):
    evidence = {}
    for record in records:
        if record.get("status") != 200 or record.get("transport_error"):
            continue
        directory = source / record["case_id"]
        request = request_context((directory / "request.body").read_bytes())
        response = decode((directory / "response.body").read_bytes())
        if not isinstance(request, dict) or not isinstance(request.get("user"), str):
            continue
        user = canonical_user(request["user"])
        if user is None:
            continue
        markers = evidence.setdefault(user, {})
        if request.get("type") == "userRole" and response == {"role": "missing"}:
            markers["missing_role"] = record["case_id"]
        if (
            request.get("type") == "preTransferCheck"
            and isinstance(response, dict)
            and response.get("userExists") is False
        ):
            markers["absent_account"] = record["case_id"]
    result = {
        user: {"basis": "captured missing role and absent account", "cases": markers}
        for user, markers in evidence.items()
        if len(markers) == 2
    }
    for user in explicit:
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", user):
            raise ValueError("--fresh-user must be a public 20-byte address")
        result[user.lower()] = {"basis": "explicit --fresh-user assertion"}
    return result


def load_fixture(path):
    if path is None:
        return {"accounts": [], "controls": []}
    fixture = json.loads(path.read_bytes())
    if not isinstance(fixture, dict) or set(fixture) != {"accounts", "controls"}:
        raise ValueError("Account fixture requires accounts and controls arrays")
    if not isinstance(fixture["accounts"], list) or not isinstance(
        fixture["controls"], list
    ):
        raise TypeError("Account fixture accounts and controls must be arrays")
    for account in fixture["accounts"]:
        if (
            not isinstance(account, dict)
            or set(account) != {"address", "queries"}
            or not isinstance(account["address"], str)
            or not re.fullmatch(r"0x[0-9a-fA-F]{40}", account["address"])
            or not isinstance(account["queries"], list)
            or any(
                not isinstance(query, str) or query not in ACCOUNT_QUERIES
                for query in account["queries"]
            )
        ):
            raise ValueError(
                "Fixture account requires address and supported query names"
            )
    for control in fixture["controls"]:
        if (
            not isinstance(control, dict)
            or set(control) != {"path", "body", "now_ms"}
            or control["path"] != "/_test/fund"
            or not isinstance(control["body"], dict)
            or type(control["now_ms"]) is not int
            or control["now_ms"] < 0
        ):
            raise ValueError(
                "Fixture controls require /_test/fund, object body, nonnegative now_ms"
            )
    return fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path, help="One captured run directory"
    )
    parser.add_argument(
        "--root", type=Path, help="Evidence directory; defaults to a fresh temporary directory"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3000")
    parser.add_argument(
        "--fresh-user",
        action="append",
        default=[],
        help="Explicit known-fresh public address",
    )
    parser.add_argument(
        "--account-fixture",
        type=Path,
        help="Explicit source account initialization and coverage JSON",
    )
    args = parser.parse_args()
    args.root = run_root(args.root, "differential")
    try:
        if validate_base_url(args.base_url) == TESTNET:
            raise ValueError("Differential replay is loopback-only")
        source = args.source.resolve()
        if args.root.resolve() == source or source in args.root.resolve().parents:
            raise ValueError("Replay root must not be inside the source capture")
        records, pending = load_source(source)
        fresh = fresh_accounts(source, records, args.fresh_user)
        raw_depth = raw_depth_index(source, records)
        fixture = load_fixture(args.account_fixture)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    recorder = Recorder(args.root, args.base_url)
    fixture_coverage = {
        (account["address"].lower(), query)
        for account in fixture["accounts"]
        for query in account["queries"]
    }
    if args.account_fixture:
        create(
            recorder.root / "account-fixture.json", args.account_fixture.read_bytes()
        )
    comparisons = []
    setup = []
    sequence = 0

    def control(path, body):
        nonlocal sequence
        sequence += 1
        name = f"control-{sequence:04d}-{path.rsplit('/', 1)[-1]}"
        try:
            result = recorder.request(
                name,
                path,
                body,
                metadata={"intended_mutation": "local differential fixture"},
            )
            error = result.transport_error or (
                f"HTTP {result.status}" if result.status != 200 else None
            )
        except (OSError, ValueError) as exc:
            error = str(exc)
        setup.append({"capture": name, "path": path, "error": error})
        return error

    reset_error = control("/_test/reset", {})
    fixture_errors = []
    for item in fixture["controls"]:
        fixture_errors.extend(
            error
            for error in (
                control("/_test/time", {"now_ms": item["now_ms"]}),
                control(item["path"], item["body"]),
            )
            if error
        )

    for index, record in enumerate(records, 1):
        case = record["case_id"]
        directory = source / case
        local_id = f"case-{index:04d}"
        row = {
            "case_id": case,
            "source": str(directory),
            "local_capture": local_id,
            "coverage_kind": "exact",
            "outcome": "matched",
            "failures": [],
            "normalized_paths": [],
            "skipped_paths": [],
            "external_context_requirement": [],
            "request_bytes_preserved": False,
            "response_bytes_equal": None,
        }
        copies = {"source.metadata.json": encode(record)}
        try:
            raw = (directory / "request.body").read_bytes()
            expected_raw = (directory / "response.body").read_bytes()
            headers = (directory / "response.headers").read_bytes()
            copies.update(
                {
                    "source.request.body": raw,
                    "source.response.body": expected_raw,
                    "source.response.headers": headers,
                }
            )
            for field, value in (
                ("request_sha256", raw),
                ("response_sha256", expected_raw),
            ):
                if record.get(field) != hashlib.sha256(value).hexdigest():
                    raise ValueError(f"Source {field} integrity mismatch")
            path = urlsplit(record["url"]).path
            if record.get("method") != "POST" or path not in ("/info", "/exchange"):
                raise ValueError(
                    "Only captured POST /info and unsigned /exchange are supported"
                )
            if path == "/exchange":
                unsigned(raw)
            request = request_context(raw)
            expected = decode(expected_raw)
            kind = request.get("type") if isinstance(request, dict) else None
            user = request.get("user") if isinstance(request, dict) else None
            user = canonical_user(user)
            row["request_path"] = path
            row["expected_status"] = record.get("status")
            row["expected_content_type"] = content_types(headers)
            if path == "/info" and type(kind) is int and kind == 42:
                row.update(coverage_kind="excluded", outcome="excluded")
                row["skipped_paths"] = ["status", "body", "headers/content-type"]
                row["external_context_requirement"] = [
                    "Numeric discriminator 42 dispatches outside selected spot scope"
                ]
                continue
            if record.get("transport_error") or record.get("status") is None:
                row.update(coverage_kind="not_covered", outcome="not_covered")
                row["external_context_requirement"].append(
                    "Source transport failed; no authoritative HTTP response"
                )
                row["skipped_paths"] = ["status", "body", "headers/content-type"]
            elif path == "/info" and record["status"] == 200:
                aggregated = (
                    kind == "l2Book"
                    and isinstance(expected, dict)
                    and request.get("nSigFigs") is not None
                )
                snapshot = (
                    nearest_raw_depth(raw_depth, expected.get("coin"), record)
                    if aggregated
                    else None
                )
                if snapshot:
                    started, fixture_case, depth = snapshot
                    row.update(coverage_kind="raw_depth_prefix", outcome="matched")
                    row["book_fixture"] = {
                        "case": fixture_case,
                        "offset_ms": round(
                            datetime.fromisoformat(record["started_at"]).timestamp()
                            * 1000
                            - started
                        ),
                    }
                    row["skipped_paths"] = ["body/time"]
                    row["external_context_requirement"].append(
                        "Buckets deeper than the same-run raw top-of-book snapshot are not reproducible"
                    )
                    error = control(
                        "/_test/book",
                        {"coin": expected["coin"], "bids": depth[0], "asks": depth[1]},
                    )
                    if error:
                        row["failures"].append({"path": "fixture/book", "reason": error})
                elif kind == "l2Book" and isinstance(expected, dict):
                    row.update(
                        coverage_kind="status_schema_only", outcome="coverage_gap"
                    )
                    row["skipped_paths"] = [
                        "body/coin",
                        "body/time",
                        "body/levels/0",
                        "body/levels/1",
                    ]
                    row["external_context_requirement"].append(
                        "Unrecorded atomic raw market depth: aggregated response is not an independent book fixture"
                    )
                elif isinstance(kind, str) and kind in ACCOUNT_QUERIES:
                    if user in fresh:
                        row["account_context"] = fresh[user]
                    elif (user, kind) in fixture_coverage:
                        row["account_context"] = {
                            "basis": "explicit source account fixture",
                            "query": kind,
                        }
                        row["failures"].extend(
                            {"path": "fixture", "reason": error}
                            for error in fixture_errors
                        )
                    else:
                        row.update(coverage_kind="not_covered", outcome="not_covered")
                        row["skipped_paths"] = ["body"]
                        row["external_context_requirement"].append(
                            "Source account state/history requires --account-fixture coverage; reset is not a funded-account fixture"
                        )
                elif kind not in ("spotMeta", "l2Book"):
                    row.update(coverage_kind="not_covered", outcome="not_covered")
                    row["skipped_paths"] = ["body"]
                    row["external_context_requirement"].append(
                        "Unclassified successful query outside selected spot contract"
                    )
            if reset_error:
                row["failures"].append({"path": "fixture/reset", "reason": reset_error})
            now_ms = int(
                datetime.fromisoformat(record["started_at"]).timestamp() * 1000
            )
            clock_error = control("/_test/time", {"now_ms": now_ms})
            if clock_error:
                row["failures"].append({"path": "fixture/time", "reason": clock_error})
            row["clock_ms"] = now_ms
            if (
                path == "/info"
                and kind == "userFees"
                and record.get("status") == 200
                and isinstance(expected, dict)
            ):
                row["external_context_requirement"].append(
                    "Captured fee schedule and global daily volumes injected via /_test/fees; user volumes remain engine-computed"
                )
                fee_error = control("/_test/fees", {"fees": expected})
                if fee_error:
                    row["failures"].append(
                        {"path": "fixture/fees", "reason": fee_error}
                    )
            result = recorder.request(
                local_id,
                path,
                raw,
                content_type=record["content_type"],
                metadata={
                    "reference": str(directory),
                    "intended_mutation": "none; unsigned replay",
                },
            )
            row["request_bytes_preserved"] = (
                result.directory / "request.body"
            ).read_bytes() == raw
            row["actual_status"] = result.status
            row["actual_content_type"] = content_types(
                (result.directory / "response.headers").read_bytes()
            )
            row["response_bytes_equal"] = result.body == expected_raw
            if result.transport_error:
                row["failures"].append(
                    {"path": "transport", "reason": result.transport_error}
                )
            if not record.get("transport_error") and record.get("status") is not None:
                for field, left, right in (
                    ("status", record["status"], result.status),
                    (
                        "headers/content-type",
                        row["expected_content_type"],
                        row["actual_content_type"],
                    ),
                ):
                    if left != right:
                        row["failures"].append(
                            {"path": field, "expected": left, "actual": right}
                        )
                if row["coverage_kind"] == "status_schema_only":
                    if not book_schema(expected) or not book_schema(result.json):
                        row["failures"].append(
                            {
                                "path": "body",
                                "reason": "Expected and actual must satisfy the l2Book response schema",
                            }
                        )
                elif row["coverage_kind"] == "raw_depth_prefix":
                    if not book_schema(expected) or not book_schema(result.json):
                        row["failures"].append(
                            {
                                "path": "body",
                                "reason": "Expected and actual must satisfy the l2Book response schema",
                            }
                        )
                    else:
                        failures, skipped = depth_prefix_differences(
                            expected, result.json, depth
                        )
                        row["failures"].extend(failures)
                        row["skipped_paths"].extend(skipped)
                elif row["coverage_kind"] == "exact":
                    actual = decode(result.body)
                    if expected is not INVALID and actual is not INVALID:
                        row["normalized_paths"] = [
                            {
                                "path": "body",
                                "rule": "JSON serialization: object order, whitespace, string escapes, equivalent numeric lexemes; numeric types retained, no fields removed",
                            }
                        ]
                        changed = differences(expected, actual)
                    else:
                        changed = [] if result.body == expected_raw else ["body"]
                    row["failures"].extend(
                        {
                            "path": changed_path,
                            "reason": "Response value mismatch; see source.response.body and response.body",
                        }
                        for changed_path in changed
                    )
                    if changed and kind == "spotMeta":
                        row["failures"].append(
                            {
                                "path": "fixture/spotMeta",
                                "reason": "Simulator metadata differs from supplied capture; restart with --meta pointing at this source response.body",
                            }
                        )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row["failures"].append({"path": "replay", "reason": str(exc)})
        finally:
            local = recorder.root / local_id
            local.mkdir(exist_ok=True)
            for name, value in copies.items():
                create(local / name, value)
            if row["failures"]:
                row["outcome"] = "mismatch"
            comparisons.append(row)
            create(local / "comparison.json", encode(row))
            recorder.event({"state": "comparison", **row})

    counts = dict(Counter(row["outcome"] for row in comparisons))
    failures = sum(bool(row["failures"]) for row in comparisons)
    setup_failures = sum(item["error"] is not None for item in setup)
    gaps = sum(row["coverage_kind"] != "exact" for row in comparisons) + len(pending)
    report = {
        "source": str(source),
        "base_url": recorder.base_url,
        "completed_source_cases": len(records),
        "pending_source_cases": pending,
        "counts": counts,
        "mismatched_cases": failures,
        "coverage_gaps": gaps,
        "full_parity": not (failures or gaps or setup_failures),
        "setup": setup,
        "setup_failures": setup_failures,
        "comparisons": comparisons,
        "exit_code": 1 if failures or setup_failures else 2 if gaps else 0,
    }
    create(recorder.root / "comparison.json", encode(report))
    recorder.event(
        {
            "state": "differential_completed",
            "counts": counts,
            "coverage_gaps": gaps,
            "mismatched_cases": failures,
            "exit_code": report["exit_code"],
        }
    )
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in ("comparisons", "setup")
            },
            indent=2,
        )
    )
    raise SystemExit(report["exit_code"])


if __name__ == "__main__":
    main()
