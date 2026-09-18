"""Verify capture hashes/completeness and scan evidence for actual wallet secrets.

Offline. --env-file is read only to detect key material; keys are never printed.
The JSON report is generated outside run directories and can be regenerated.
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("captures"))
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, default=Path("captures/index.json"))
    args = parser.parse_args()
    secrets = []
    if args.env_file:
        for line in args.env_file.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if "PRIVATE_KEY" in key and value:
                    secrets.append(value.removeprefix("0x").lower().encode())
    errors = []
    runs = []
    all_statuses = Counter()
    total_completed = 0
    for manifest in sorted(args.root.rglob("manifest.jsonl")):
        rows = [json.loads(line) for line in manifest.read_text().splitlines()]
        pending = set()
        completed = set()
        statuses = Counter()
        for row in rows:
            if row.get("state") == "pending":
                pending.add(row["case_id"])
            elif row.get("state") == "completed":
                case = row["case_id"]
                if case in completed:
                    errors.append(f"Duplicate completion: {manifest.parent}/{case}")
                completed.add(case)
                pending.discard(case)
                directory = manifest.parent / case
                for name in (
                    "request.body",
                    "response.body",
                    "response.headers",
                    "transport.trace",
                    "metadata.json",
                ):
                    if not (directory / name).is_file():
                        errors.append(f"Missing artifact: {directory}/{name}")
                for name, hash_key in (
                    ("request.body", "request_sha256"),
                    ("response.body", "response_sha256"),
                ):
                    path = directory / name
                    if (
                        path.is_file()
                        and hashlib.sha256(path.read_bytes()).hexdigest()
                        != row[hash_key]
                    ):
                        errors.append(f"Hash mismatch: {path}")
                if (directory / "metadata.json").is_file() and json.loads(
                    (directory / "metadata.json").read_text()
                ) != {key: value for key, value in row.items() if key != "recorded_at"}:
                    errors.append(f"Manifest differs from metadata: {directory}")
                statuses[str(row.get("status"))] += 1
        provenance = manifest.parent / "provenance/metadata.json"
        if provenance.is_file():
            for name, digest in json.loads(provenance.read_text())[
                "source_sha256"
            ].items():
                path = provenance.parent / name
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != digest
                ):
                    errors.append(f"Provenance mismatch: {path}")
        workflow = [
            row
            for row in rows
            if row.get("state")
            in (
                "run_aborted",
                "scenario_completed",
                "matrix_completed",
                "precondition_unavailable",
            )
        ]
        runs.append(
            {
                "path": str(manifest.parent),
                "completed_requests": len(completed),
                "pending_requests": sorted(pending),
                "statuses": dict(statuses),
                "last_workflow_event": workflow[-1].get("state") if workflow else None,
                "has_source_provenance": provenance.is_file(),
            }
        )
        total_completed += len(completed)
        all_statuses.update(statuses)
    scanned_files = 0
    for path in args.root.rglob("*"):
        if path.is_file() and path != args.output:
            scanned_files += 1
            raw = path.read_bytes().lower()
            if any(secret in raw for secret in secrets):
                errors.append(f"Private key material found in {path}")
    report = {
        "completed_manifest_requests": total_completed,
        "statuses": dict(all_statuses),
        "runs": runs,
        "files_scanned_for_secret_material": scanned_files,
        "wallet_keys_checked": len(secrets),
        "errors": errors,
        "legacy_note": "Initial seven baseline HTTP captures predate manifests; their bytes remain under the timestamped baseline directory.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "runs"}, indent=2
        )
    )
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
