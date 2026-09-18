"""Read-only contract matrix; all inputs and exact outputs remain in captures."""

from .recorder import Recorder, create

FRESH = "0x" + "1" * 40


def report(case_id, result):
    print(
        f"{case_id}: HTTP {result.status or '-'}; {len(result.body)} bytes; "
        f"{'transport error' if result.transport_error else 'captured'}",
        flush=True,
    )


def info(recorder, case_id, body, **kwargs):
    result = recorder.request(case_id, "/info", body, **kwargs)
    report(case_id, result)
    return result


def run_matrix(recorder: Recorder, user: str, fresh_user: str):
    metadata = info(recorder, "spot-meta", {"type": "spotMeta"})
    if isinstance(metadata.json, dict) and "universe" in metadata.json:
        create(recorder.root / "spotMeta.json", metadata.body)
        universe = sorted(metadata.json["universe"], key=lambda item: item["index"])
        coin = universe[0]["name"] if universe else "@0"
    else:
        recorder.event(
            {
                "state": "setup_failed",
                "reason": "spotMeta unavailable; l2Book uses documented fallback @0",
            }
        )
        coin = "@0"
    base = [
        ("spot-state", {"type": "spotClearinghouseState", "user": user}),
        ("pre-transfer", {"type": "preTransferCheck", "user": user}),
        (
            "ledger",
            {"type": "userNonFundingLedgerUpdates", "user": user, "startTime": 0},
        ),
        ("fees", {"type": "userFees", "user": user}),
        ("fills", {"type": "userFills", "user": user}),
        ("order-unknown-oid", {"type": "orderStatus", "user": user, "oid": 1}),
        ("book", {"type": "l2Book", "coin": coin}),
        ("role", {"type": "userRole", "user": user}),
        ("meta", {"type": "spotMeta"}),
    ]
    for case_id, body in base:
        if case_id != "meta":
            info(recorder, case_id, body)
        for key in body:
            if key == "type":
                continue
            missing = {k: v for k, v in body.items() if k != key}
            info(recorder, f"{case_id}-missing-{key}", missing)
            info(recorder, f"{case_id}-wrong-{key}", {**body, key: []})
        info(recorder, f"{case_id}-wrong-type", {**body, "type": 42})
        if "user" in body:
            info(recorder, f"{case_id}-fresh", {**body, "user": fresh_user})
            info(
                recorder, f"{case_id}-invalid-user", {**body, "user": "not-an-address"}
            )
    for case_id, body in [
        ("malformed-json", b'{"type":"spotMeta"'),
        ("trailing-json", b'{"type":"spotMeta"} trailing'),
        ("json-null", b"null"),
        ("json-array", b"[]"),
        ("json-string", b'"spotMeta"'),
        ("missing-type", {}),
        ("invalid-type", {"type": "notAnInfoType"}),
        (
            "order-unknown-cloid",
            {"type": "orderStatus", "user": user, "oid": "0x" + "1" * 32},
        ),
        ("order-invalid-cloid", {"type": "orderStatus", "user": user, "oid": "0x12"}),
        ("book-unknown", {"type": "l2Book", "coin": "@999999999"}),
        (
            "fills-aggregate",
            {"type": "userFills", "user": user, "aggregateByTime": True},
        ),
        (
            "ledger-range",
            {
                "type": "userNonFundingLedgerUpdates",
                "user": user,
                "startTime": 0,
                "endTime": 1,
            },
        ),
    ]:
        info(recorder, case_id, body)
    info(
        recorder, "wrong-content-type", {"type": "spotMeta"}, content_type="text/plain"
    )
    for digits in (None, 2, 3, 4, 5, 1, 6):
        info(
            recorder,
            f"book-sigfigs-{digits}",
            {"type": "l2Book", "coin": coin, "nSigFigs": digits},
        )
    for mantissa in (1, 2, 5, 3):
        info(
            recorder,
            f"book-mantissa-{mantissa}",
            {"type": "l2Book", "coin": coin, "nSigFigs": 5, "mantissa": mantissa},
        )
    info(
        recorder,
        "book-mantissa-without-sigfigs",
        {"type": "l2Book", "coin": coin, "mantissa": 2},
    )
    recorder.event(
        {
            "state": "matrix_completed",
            "user": user,
            "fresh_user": fresh_user,
            "freshness_claim": "Address is a supplied candidate; inspect role/preTransferCheck responses",
        }
    )
