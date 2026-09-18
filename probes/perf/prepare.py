"""Generate public local-only fixture signatures; no environment keys or HTTP."""
import hashlib
import json
import os
import platform
import resource
import sys
import time
from pathlib import Path

from eth_account import Account
from hyperliquid.utils.signing import sign_l1_action

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
users = 1000
per_user = 20
now = 1800000000000
start = time.perf_counter()
rows = []
for user in range(users):
    key = hashlib.sha256(f"hypercore-local-only-performance-fixture-{user}".encode()).digest()
    wallet = Account.from_key(key)
    requests = []
    for sequence in range(per_user):
        action = {
            "type": "order",
            "orders": [{"a": 10000, "b": True, "p": "10", "s": "2", "r": False,
                        "t": {"limit": {"tif": "Ioc"}},
                        "c": f"0x{user + 1:016x}{sequence + 1:016x}"}],
            "grouping": "na",
        }
        nonce = now + sequence
        envelope = {"action": action, "nonce": nonce,
                    "signature": sign_l1_action(wallet, action, None, nonce, None, False),
                    "vaultAddress": None, "expiresAfter": None}
        requests.append(json.dumps(envelope, separators=(",", ":")))
    rows.append({"address": wallet.address.lower(), "requests": requests})
metadata = {
    "users": users, "orders_per_user": per_user, "fixed_time_ms": now,
    "market": "PURR/USDC", "asset": 10000, "price": "10", "size": "2",
    "python": platform.python_version(), "platform": platform.platform(),
    "cpu_count": os.cpu_count(), "cpu_affinity_count": len(os.sched_getaffinity(0)),
    "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
    "file_descriptor_limits": resource.getrlimit(resource.RLIMIT_NOFILE),
    "signature_preparation_seconds": time.perf_counter() - start,
    "key_policy": "Public deterministic local fixture scalars, no real wallet keys, no upstream requests",
    "signing_in_timed_region": False,
}
(root / "payloads.json").write_text(json.dumps({"metadata": metadata, "users": rows}, separators=(",", ":")))
(root / "preparation.json").write_text(json.dumps(metadata, indent=2))
print(json.dumps(metadata, indent=2))
