"""Byte-preserving, single-attempt HTTP evidence recorder."""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

TESTNET = "https://api.hyperliquid-testnet.xyz"


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def encode(value) -> bytes:
    return json.dumps(
        value, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create(path: Path, value: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    sync_directory(path.parent)


def validate_base_url(url: str) -> str:
    if url == TESTNET:
        return url
    parsed = urlsplit(url)
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError:
        loopback, port = False, None
    if not (
        parsed.scheme == "http"
        and loopback
        and port
        and not parsed.username
        and not parsed.password
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    ):
        raise ValueError(
            "Only exact testnet URL or http://<literal-loopback-IP>:<port> is permitted"
        )
    return url


def reject_secrets(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.sub(r"[^a-z]", "", str(key).lower()) in {
                "privatekey",
                "secretkey",
                "mnemonic",
                "seedphrase",
                "password",
            }:
                raise ValueError("Secret-bearing fields must not enter evidence")
            reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, (str, bytes)):
        raw = value.encode() if isinstance(value, str) else value
        for name, secret in os.environ.items():
            if (
                ("PRIVATE_KEY" in name.upper() or "SECRET_KEY" in name.upper())
                and len(secret) >= 16
                and (
                    secret.encode() in raw or secret.removeprefix("0x").encode() in raw
                )
            ):
                raise ValueError("Private key material must not enter evidence")


@dataclass(frozen=True)
class Result:
    status: int | None
    body: bytes
    json: object | None
    directory: Path
    transport_error: str | None


def provenance(root: Path) -> dict:
    project = Path(__file__).resolve().parent.parent
    destination = root / "provenance"
    destination.mkdir()
    sources = sorted((project / "probes").glob("*.py"))
    sources.extend(sorted((project / "src").glob("*.rs")))
    sources.extend(
        project / name
        for name in ("pyproject.toml", "uv.lock", "Cargo.toml", "Cargo.lock")
        if (project / name).is_file()
    )
    hashes = {}
    for source in sources:
        relative = source.relative_to(project)
        raw = source.read_bytes()
        reject_secrets(raw)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        create(target, raw)
        hashes[str(relative)] = hashlib.sha256(raw).hexdigest()
    try:
        sdk_version = importlib.metadata.version("hyperliquid-python-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = None
    result = {
        "python": sys.version,
        "platform": platform.platform(),
        "hyperliquid_python_sdk": sdk_version,
        "source_sha256": hashes,
    }
    binary = project / "target/debug/hypercore-simulator"
    if binary.is_file():
        result["simulator_binary_sha256"] = hashlib.sha256(
            binary.read_bytes()
        ).hexdigest()
    create(destination / "metadata.json", encode(result))
    return result


class Recorder:
    def __init__(self, root: Path, base_url: str):
        self.base_url = validate_base_url(base_url)
        self.root = Path(root).absolute()
        self.root.mkdir(parents=True, exist_ok=True)
        sync_directory(self.root.parent)
        if any(self.root.iterdir()):
            raise FileExistsError("Run root must be new or empty; use a new run ID")
        source_provenance = provenance(self.root)
        self.minimum_interval = 1.5 if self.base_url == TESTNET else 0.0
        create(
            self.root / "run.json",
            encode(
                {
                    "started_at": timestamp(),
                    "base_url": self.base_url,
                    "transport": "curl HTTP/1.1",
                    "retries": 0,
                    "redirects": False,
                    "minimum_interval_seconds": self.minimum_interval,
                    "provenance": source_provenance,
                    "intended_mutation": "Per-case metadata; /info read-only; /exchange explicitly signed actions",
                }
            ),
        )
        create(self.root / "manifest.jsonl", b"")
        self._last_request = 0.0

    def event(self, value: dict) -> None:
        reject_secrets(value)
        with (self.root / "manifest.jsonl").open("ab") as stream:
            stream.write(encode({"recorded_at": timestamp(), **value}) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def request(
        self,
        case_id: str,
        path: str,
        body: dict | str | bytes,
        *,
        expected_status: int | None = None,
        metadata: dict | None = None,
        content_type: str = "application/json",
    ) -> Result:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", case_id):
            raise ValueError("Invalid case ID")
        if path not in (
            "/info",
            "/exchange",
            "/_test/fund",
            "/_test/book",
            "/_test/reset",
            "/_test/account",
            "/_test/time",
            "/_test/fault",
            "/_test/fees",
            "/_test/dust",
            "/_test/upgrade",
        ):
            raise ValueError("Unsupported path")
        if path.startswith("/_test/") and self.base_url == TESTNET:
            raise ValueError("Test controls are loopback-only")
        if "\r" in content_type or "\n" in content_type:
            raise ValueError("Invalid content type")
        reject_secrets(metadata)
        if isinstance(body, dict):
            reject_secrets(body)
            raw = encode(body)
        else:
            raw = body.encode() if isinstance(body, str) else body
            try:
                reject_secrets(json.loads(raw))
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        reject_secrets(raw)
        directory = self.root / case_id
        directory.mkdir(exist_ok=False)
        sync_directory(self.root)
        create(directory / "request.body", raw)
        pending = {
            "case_id": case_id,
            "state": "pending",
            "started_at": timestamp(),
            "method": "POST",
            "url": self.base_url + path,
            "content_type": content_type,
            "expected_status": expected_status,
            "parameters": metadata or {},
            "intended_mutation": (metadata or {}).get(
                "intended_mutation", "none" if path == "/info" else "unspecified action"
            ),
            "request_sha256": hashlib.sha256(raw).hexdigest(),
        }
        create(directory / "pending.json", encode(pending))
        self.event(pending)
        command = [
            "curl",
            "--disable",
            "--http1.1",
            "--silent",
            "--show-error",
            "--noproxy",
            "*",
            "--proto",
            "=http,https",
            "--max-redirs",
            "0",
            "--connect-timeout",
            "10",
            "--max-time",
            "30",
            "--retry",
            "0",
            "--request",
            "POST",
            "--header",
            "Content-Type: " + content_type,
            "--data-binary",
            "@" + str(directory / "request.body"),
            "--dump-header",
            str(directory / "response.headers"),
            "--output",
            str(directory / "response.body"),
            "--trace-time",
            "--trace",
            str(directory / "transport.trace"),
            "--write-out",
            "%{json}",
            self.base_url + path,
        ]
        create(directory / "command.json", encode(command))
        time.sleep(
            max(0.0, self.minimum_interval - (time.monotonic() - self._last_request))
        )
        self._last_request = time.monotonic()
        exit_code = None
        stdout = stderr = b""
        error = None
        interrupted = False
        try:
            completed = subprocess.run(command, capture_output=True, check=False)
            exit_code, stdout, stderr = (
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
            if exit_code:
                error = f"curl exited {exit_code}; see curl.stderr (submission may be ambiguous)"
        except KeyboardInterrupt:
            error = "interrupted; submission may be ambiguous"
            interrupted = True
        except OSError as exc:
            error = f"curl launch failed: {exc}"
        create(directory / "curl.stdout", stdout)
        create(directory / "curl.stderr", stderr)
        for filename in ("response.body", "response.headers", "transport.trace"):
            if not (directory / filename).exists():
                create(directory / filename, b"")
            with (directory / filename).open("rb") as captured:
                os.fsync(captured.fileno())
        sync_directory(directory)
        response = (directory / "response.body").read_bytes()
        try:
            transfer = json.loads(stdout)
        except (ValueError, UnicodeDecodeError):
            transfer = None
        status = (
            int(transfer.get("http_code", 0)) or None
            if isinstance(transfer, dict)
            else None
        )
        try:
            parsed = json.loads(response)
        except (ValueError, UnicodeDecodeError):
            parsed = None
        outcome = "transport_error" if error else "observed"
        if not error and expected_status is not None:
            outcome = (
                "expected_status" if status == expected_status else "unexpected_status"
            )
        record = {
            **pending,
            "state": "completed",
            "finished_at": timestamp(),
            "status": status,
            "curl_exit_code": exit_code,
            "transfer": transfer,
            "transport_error": error,
            "outcome": outcome,
            "response_sha256": hashlib.sha256(response).hexdigest(),
        }
        create(directory / "metadata.json", encode(record))
        self.event(record)
        if interrupted:
            raise KeyboardInterrupt
        return Result(status, response, parsed, directory, error)


def run_root(root, suite):
    """Resolve a local suite's evidence root: the given path, else a fresh temp dir.

    Local replay output is a reproducible byproduct, so it defaults to a temporary
    directory rather than the repository. The chosen path is announced on stderr.
    """
    import sys
    import tempfile

    if root is None:
        root = Path(tempfile.mkdtemp(prefix=f"hypercore-{suite}-"))
    print(f"evidence root: {root}", file=sys.stderr)
    return root
