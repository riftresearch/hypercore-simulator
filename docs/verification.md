# Verification and offline reproduction

## Current scoped evidence

Local replay and verification run directories under `captures/local/` are reproducible byproducts and are not stored in the repository (only upstream captures are). The paths below name the archived runs these numbers came from; regenerate a run with the commands further down.

These immutable reports establish only their named scopes, not perfect parity. The activation-gas counterexample was reproduced as six mismatches before the fix; the four-scenario follow-up now passes201 comparisons across516 HTTP requests. Non-USDC quote calibration now passes all recorded financial comparisons. A native transient unknown-cloid response remains an exact status mismatch.

| Report | Recorded result | Scope / qualification |
| --- | --- | --- |
| `captures/local/actor-http-v1/report.json` | Passed with admission limit2 | Incomplete bodies occupy admission; overload consumes no nonce; the rejected envelope later fills once. During a6second delayed acknowledgment, committed state was visible in2.2ms; post-commit disconnect preserves it. |
| `captures/local/actor-shutdown-v1/report.json` | Passed; process exit0 | SIGINT after an observed fill but before its delayed acknowledgment still delivered the filled response after6seconds, then joined the actor and exited. |
| `captures/local/actor-parity-v1/golden-extended.json` | 3,405 passed; 11,587 HTTP requests; 1 mismatch; 13 gaps | All33 selected suites replayed. Counts and every mismatch/gap case and reason are identical to `integrated-parity-v8`; exit1 remains explicit. |
| `captures/local/actor-runtime-v1/verification.json` | 270 assertions; 146 HTTP requests; passed | Actor executable, testnet signature mode, including all four injected faults. |
| `captures/local/actor-mainnet-v1/verification.json` | 270 assertions; 146 HTTP requests; passed | Local SDK mainnet-signature mode only, **not live mainnet parity**. |
| `captures/local/actor-original-v1/golden.json` | 150 checks; 68 HTTP requests; passed | Original signed IOC/transfer/sell sequence and omitted-default signature recovery. |
| `captures/local/actor-protocol-v1/comparison.json` | 149 matched; 0 mismatches; 1 excluded | Unchanged numeric lending discriminator42 exclusion; exit2, not full parity. |
| `captures/local/actor-performance-v1/report.json` | 101,000 filled orders; 6 trials; 0 errors | Every trial reconciled all1,000 accounts and remaining book depth; workload and limitations below. |
| `captures/local/integrated-parity-v8/golden-extended.json` | 3,405 passed; 11,587 HTTP requests; 1 mismatch; 13 gaps | 33 selected suites, omitting only the duplicate multilevel alias. The mismatch is native transient unknown-cloid visibility. 2,697 fixtures and 6,214 unused contexts are not parity assertions. Exit1. |
| `captures/local/integrated-runtime-v8/verification.json` | 270 assertions; 146 HTTP requests; passed | Actual testnet-mode executable, including accounting, signature recovery and fault controls. |
| `captures/local/mainnet-parity-v8/verification.json` | 270 assertions; 146 HTTP requests; passed | Current executable's local SDK domain separation only, **not live mainnet parity**. |
| `captures/local/integrated-original-v8/golden.json` | 150 checks; 68 HTTP requests; passed | Original IOC/transfer/sell sequence plus omitted-default signature recovery against the original admitted nonce. |
| `captures/local/integrated-fees-v8/results.json` | 520 assertions; 161 HTTP requests; passed | Controlled fee/volume behavior. VIP equality remains an explicitly unmeasured strict-`>` assumption. |
| `captures/local/integrated-protocol-v8/comparison.json` | 149 in-scope matched; 0 mismatches; 1 excluded | Numeric lending discriminator42 is outside spot scope; report exits2, not full parity. |
| `captures/local/transition-parity-v8/results.json` | 62 assertions; 37 HTTP requests; passed | Upgrade admission and current-mark supersession. |
| `captures/local/validator-parity-v8/results.json` | 7 checks; 52 HTTP requests; passed | Missing-fill detection, raw JSON arrays and capped-history baseline protection. |
| `captures/local/calibrated-quotes-v1/golden-extended.json` | 379 passed; 990 HTTP requests; 1 mismatch; 1 gap | Quote accounting, alternate gas and fee-volume controls. The mismatch is immediate native unknown-cloid visibility; the gap preserves its aborted source workflow. |
| `captures/local/activation-priority-after-fix/golden-extended.json` | 201 passed; 516 HTTP requests; 0 mismatches/gaps | Same-quote gas, USDC priority for nonquote sends, and mandatory-gas rejection without fallback. |
| `captures/local/cloid-visibility-analysis-v1/analysis.json` | Offline source-hash, identical-request and timing analysis; 0 HTTP requests | Client-boundary delay guesses fail. Server lookup time remains unobserved; a common server threshold is not ruled out or identified. |
| `captures/local/clean-start-v8/` | 4 HTTP requests; reset acknowledged; clock `fixed:false`; former signer missing with empty balances | Primary loopback simulator left clean. Secondary verification servers stopped. |

Live dust allocation evidence spans [`dust-lifecycle-01`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-lifecycle-01/), [`dust-followup-02`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-followup-02/) and [`dust-split-asymmetric`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-split-asymmetric/). The original lifecycle classification failure is retained; later analysis does not rewrite it as a completed run. Live partial fills, NQ activation, static batch validation, terminal cloid reuse and first-rejection sent flags are indexed in [observations](observations.md).

The HTTP replays exercise the actual executable rather than mocked handlers. [Extended replay](golden-extended.md) uses chronological signed episodes, explicit initial balances/basis/sent flags, source clocks and independent book/mark context. Fees import external schedules/global activity, never donor user volume. Initial fills/ledger are comparison baselines, not invented history. Identity/timestamp mapping is explicit; monetary quantities, errors and state transitions are not normalized away. Missing atomic depth/reference context and historical oid mappings remain reported gaps, not inferred from the response being tested.

The [unsigned differential replay](differential-replay.md) separately compares HTTP status, content type and response content. Numeric lending is an explicit scope exclusion. A local fixture round trip proves fixture handling, not reconstruction of external exchange activity.

## Actor runtime and performance

The actor revision passed `cargo clippy --locked --all-targets -- -D warnings`, all10 Rust tests, and debug/release builds. Four actor regressions exercise FIFO funding/query/reset, queued work after reply cancellation, admission retained by an unread reply, and draining accepted work after the last actor handle drops. The isolated HTTP smoke also exited cleanly on SIGINT.

Same archived driver and workload as the mutex baseline (`captures/local/actor-performance-v1/baseline.json`), on an Intel i9-14900K with32 logical CPUs, default Tokio runtime and release builds:

| Concurrent requests | Mutex fills/s | Actor fills/s | Mutex p99 | Actor p99 |
| --- | --- | --- | --- | --- |
| 1 | 5,359–6,254 | 4,508 | 0.41–0.74ms | 1.18ms |
| 10 | 1,449 | 8,516 | 12.65ms | 1.35ms |
| 100 | 1,873 | 8,556 | 136.35ms | 12.54ms |
| 1,000 | 1,603–1,931 | 8,114–8,221 | 2,615–4,078ms | 131–139ms |

The two sustained1,000-concurrency actor trials each filled20,000 orders; a separate1,000-order burst completed in125ms. The actor removes the concurrent mutex collapse but **reduces single-client throughput**. The previous single-Tokio-worker mutex configuration already reached7,705–7,924 fills/s; do not attribute the entire default-runtime improvement to faster matching.

These are pre-signed, closed-loop HTTP/1.1 keep-alive IOC buys across1,000 distinct local fixture users, one shared PURR/USDC market and one ask level. Setup, signing, connection warmup and financial reconciliation are outside the timer. Fixed clock; no periodic dust sweep during measurement; at most20 orders per user, not a long-history soak. RSS includes setup and previous reset cases, so these results establish neither a memory bound nor a memory improvement. No upstream requests or funded-account private keys were used.

The benchmark drivers live in `probes/perf/` (`prepare.py`, `bench.mjs`); the archived run also kept the baseline, source/executable hashes and per-trial results. Run the preparation script from the repository root with a new temporary directory, then invoke `bench.mjs ROOT BASE_URL SERVER_PID PROFILE CASES_JSON` against a separate loopback release process. Cases have `name`, `concurrency` and `per_user`; the driver resets the target between trials.

## Historical implementation runs

Earlier reports are retained unchanged and establish only their then-current source/coverage:

- `captures/local/golden-final/golden.json`:145 checks across66 recorded requests for the original IOC/transfer/sell sequence.
- `captures/local/verification-final/verification.json`:270 assertions across146 requests.
- `captures/local/verification-mainnet/verification.json`:270 assertions across146 loopback requests. This is local SDK domain separation/interoperability, **not live mainnet parity**.
- `captures/local/final-reset/`: historical reset cleared fixture state, faults and fixed clock, then observed the former fixture account as role`missing`.

Earlier build/test/format/lint successes belong to their historical revisions. The original verifier exercised signature recovery, funding, controlled depth/aggregation, accounting, and refusal/rate-limit/delay/drop-after-commit controls; old filenames containing “final” do not confer current finality.

## Run it again

`just parity` from the repository root does everything below in one go: release build, a testnet-mode server on port 3999, every suite in sequence with the protocol-parity replay against its own metadata, and a verdict comparing mismatch counts to the expected baseline in the `justfile`. To run pieces by hand, start a loopback server from the repository root:

```sh
cargo run -- --network testnet --bind 127.0.0.1:3000
```

Then, in a second terminal:

```sh
uv sync --locked
uv run python -m probes.golden
uv run python -m probes.verify
cargo test --locked
```

For the newer chronological replay, select source runs explicitly where needed:

```sh
uv run python -m probes.golden_extended
uv run python -m probes.golden_extended \
  --suites dust-followup,dust-split-asymmetric \
  --capture dust-followup=captures/testnet/dust-followup-02
```

These are reproduction commands, not a claim that the default selection covers every optional suite. Inspect complete coverage, mismatches, external-context gaps and exit status; passing selected suites is not equivalent to complete parity.

Reproduce the exact33-suite selection, three capture overrides and258 explicitly ordered context files from the saved report:

```sh
uv run python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path

report = json.loads(Path("captures/local/integrated-parity-v8/golden-extended.json").read_text())
config = report["configuration"]
original = Path(config["source_root"])
local = Path("captures/testnet")
def relocate(value):
    path = Path(value)
    return str(local / path.relative_to(original)) if path.is_relative_to(original) else str(path)
command = [sys.executable, "-m", "probes.golden_extended",
           "--source-root", str(local),
           "--suites", ",".join(report["selected_suites"])]
for suite, path in config["capture_overrides"].items():
    command += ["--capture", f"{suite}={relocate(path)}"]
for path in config["external_contexts"]:
    command += ["--external-context", relocate(path)]
raise SystemExit(subprocess.call(command))
PY
```

This uses the default loopback port3000 and propagates the replay's nonzero status. The recorded run took405.89seconds with peak RSS1,730,152KiB. Its remaining status mismatch is not waived.

Use a **new run directory** each time. Replay/verifier scripts reset their target server, so do not run them concurrently against the same port or a simulator used by another test. They refuse the testnet base URL and use no funded-account private keys. Golden replay uses recorded signed envelopes with captured clocks and seeded preconditions; the verifier uses public local-only fixture keys.

For mainnet signature-mode verification, start a separate local server:

```sh
cargo run -- --meta captures/testnet/readonly-contract/spotMeta.json \
  --bind 127.0.0.1:3001 --network mainnet
uv run python -m probes.verify \
  --base-url http://127.0.0.1:3001 --network mainnet
```

Do not run golden testnet signed envelopes against a mainnet-signature server.

## Evidence integrity

```sh
uv run python -m probes.audit --env-file .env
```

The offline audit regenerates `captures/index.json`. It verifies request/response hashes, metadata versus manifest equality, required artifacts and saved source hashes; inventories pending requests and status codes; and scans evidence for the private keys supplied in the environment file without printing them. `.env` remains ignored and permission0600. The seven original baseline captures predate manifests and are called out separately in the report.

The latest audit validated62,190 completed manifest requests, scanned568,077 files against all eight configured wallet keys, and reported zero errors. Actor benchmark and HTTP smoke drivers were archived before their temporary copies were removed. Actor build, Clippy, all10 Rust tests and HTTP results are recorded above; earlier Python lint/byte-compilation successes belong to their historical revisions.

Failed and incomplete historical runs remain available. The first send run halted on429; a local intermediate verifier stopped on a provenance-directory enumeration bug. Neither was overwritten or passed off as successful proof. Source captures, manifests and stored reports remain immutable; subsequent context or fixes require a new report.

## Limits of this proof

The integrated evidence retains historical incomplete recordings, transport/rate-limit failure and isolated historical-ID prerequisites. Recovery-chain replay covers isolated late/dust IDs without inventing history; the original golden replay separately proves omitted-default signature recovery using the original admitted nonce. Native quote accounting and gas-priority cases are calibrated, but a transient native unknown-cloid observation remains unmatched: its server-side read visibility is not an input in the captures. A constant threshold on client request times does not reproduce the observed timings; the actual server lookup instants are unknown. No expected-response override or guessed delay is used. Other limits include absent atomic depth/reference snapshots, individual makers hidden by aggregate books, exact nonce-window edges, dust caps, metadata-hidden quote/alignment/volume eligibility and VIP equality. Mainnet has not been live-probed. See [simulator semantics](simulator-semantics.md#unverified-compatibility).
