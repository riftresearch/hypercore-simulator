# Testnet probe harness

The harness signs with `hyperliquid-python-sdk==0.24.0` but never uses its HTTP client. `curl` is the only request transport. Python 3.11+, `uv`, and `curl` supporting `%{json}` are required. Commands run from the repository root.

## Safety and evidence

- Allowed upstream: exactly `https://api.hyperliquid-testnet.xyz`. The only other accepted bases are `http://<literal-loopback-IP>:<port>` (for example `http://127.0.0.1:3000` or `http://[::1]:3000`). No trailing slash, hostname, credentials, query, or path. No mainnet access.
- HTTP/1.1, no redirects, no retries, no proxy environment, no curl configuration files; 10-second connect and 30-second total timeout. Testnet request starts are spaced at least 1.5 seconds apart; loopback requests are unthrottled. The original 250 ms pacing hit a captured 429 and was replaced. Do not run concurrent testnet recorders or share the account with another writer.
- Each run root must be new or empty. Each case directory is created exclusively. A duplicate case ID refuses to overwrite any artifacts.
- New runs save Python/platform/installed SDK version, SHA256s, and source snapshots for `probes/*.py`, `src/*.rs`, project manifests and lockfiles under `provenance/`. The built simulator's SHA256 is recorded when present. Only explicit source paths are copied; `.env` and unrelated workspace files are never copied. Older captures created before provenance support remain unchanged.
- Before network I/O: exact `request.body`, immutable `pending.json`, and a fsynced pending row in append-only `manifest.jsonl`. Afterward: exact `response.body`, `response.headers`, timestamped hex `transport.trace`, `curl.stdout`, `curl.stderr`, `command.json`, immutable `metadata.json`, and a completed manifest row. Capture files and directory entries are fsynced before completion is recorded. Metadata includes timestamps, exit code, HTTP status, curl transfer data, hashes, parameters, intended mutation, and outcome. Curl trace is application-layer evidence, not packet capture.
- Launch/transport failures still complete a manifest row. Process kill or power loss can leave only a pending row; treat such a signed submission as ambiguous. Never blindly rerun it. Read-only balances, fills, ledger, order-status, and nonce evidence must resolve it first. Signed scenarios halt on transport ambiguity; they never automatically retry.
- HTTP errors and JSON exchange errors are preserved byte-for-byte, not rewritten. An HTTP 200 is not proof that a signed action succeeded. `expected_status` is only an optional comparison recorded in metadata.
- No private keys belong in evidence, arguments, metadata, or stdout. The harness rejects recognized secret field names and known private-key environment values in request/metadata content. It does not read `.env`, generate accounts, or print keys. Supply account secrets through environment variables from the existing ignored, mode-0600 `.env`; avoid shell tracing or echo. Use a trusted loader rather than sourcing an untrusted file.

Account A uses `HYPERLIQUID_PRIVATE_KEY` (the signing address is derived from the key). Account B uses `HYPERLIQUID_B_PRIVATE_KEY` and `HYPERLIQUID_B_ADDRESS`. Public `HYPERLIQUID_ADDRESS` can be used for read-only commands. Existing parent-created accounts are supported; fresh means actual chain state, not a new local run. If another fresh account is needed, generate it with `cast wallet new --json` with stdout captured **in memory**, store only its secret in ignored mode-0600 `.env`, and record only the public address plus a redacted generation outcome. Never save unredacted cast output under captures. This CLI deliberately does not generate keys.

`uv` can load the existing file without executing shell code. For example, this plans (but does not submit) an existing-recipient send using A/B directly from `.env`:

```sh
uv run --env-file .env python -m probes --root captures/testnet/send-plan-existing-001 \
  send --cases existing --amount 0.1 --max-spend 2
```

Add `--execute` only for a deliberate submission. The activation module similarly reads `HYPERLIQUID_C_ADDRESS` from the loaded environment. Shell `$VARIABLE` expansions in examples below require already-exported public variables; `uv --env-file` sets the child environment, not the parent shell, so pass public addresses literally if they are not exported.

## Read-only contract matrix

```sh
uv run python -m probes --root captures/testnet/read-only-001 matrix \
  --user "$HYPERLIQUID_ADDRESS" --fresh-user "$HYPERLIQUID_B_ADDRESS"
```

`--fresh-user` defaults to the public `0x1111111111111111111111111111111111111111` candidate. Its actual address is persisted in every relevant request; the harness does not claim the default is always fresh. Inspect `userRole` and `preTransferCheck` evidence.

The deterministic matrix covers `spotClearinghouseState`, `preTransferCheck`, `userNonFundingLedgerUpdates`, `userFees`, `userFills`, `orderStatus`, `spotMeta`, and `l2Book`, plus `userRole`. It captures valid/fresh/invalid users, missing required fields, wrong field types, unknown oid/cloid, invalid type, malformed JSON, root JSON types, wrong content type, ledger ranges and fills aggregation. It saves the exact `spotMeta` response as `spotMeta.json` and chooses the lowest-index spot universe entry for book requests. Book aggregation probes include `nSigFigs` null/2/3/4/5 and out-of-range 1/6, mantissa 1/2/5 and invalid 3, and mantissa without sigfigs. Output reports only case ID, status, and response size.

Case names describe inputs, not expected server behavior. Parent-run observations include numeric type 42 selecting a lending response and a valid object followed by trailing garbage being accepted. Fresh-account preTransferCheck has also produced both a transient 500/null and a 200 fee-zero response. These are observed captures, not hard-coded expectations or a guarantee of future behavior; consult the run evidence and baseline/verification notes.

## Importable recorder API

```python
from pathlib import Path
from probes import Recorder

recorder = Recorder(Path("captures/testnet/manual-001"),
                    "https://api.hyperliquid-testnet.xyz")
result = recorder.request(
    "fresh-role", "/info",
    {"type": "userRole", "user": "0x1111111111111111111111111111111111111111"},
    expected_status=200,
    metadata={"intended_mutation": "none", "purpose": "freshness observation"},
)
```

Constructor: `Recorder(root: Path, base_url: str)`.

Method: `request(case_id: str, path: str, body: dict | str | bytes, *, expected_status: int | None = None, metadata: dict | None = None, content_type: str = 'application/json') -> Result`.

Fields: `status: int | None`, `body: bytes`, `json: object | None`, `directory: Path`, `transport_error: str | None`. A JSON `null` and failed JSON parsing both give `.json is None`; use the raw body to distinguish them. Dict bodies are serialized once, compactly, preserving insertion order; strings/bytes are sent unchanged. Only `/info`, `/exchange`, and explicit loopback `/_test/*` control paths are permitted. `Recorder.event(dict)` adds a timestamped durable custom manifest row. The imported raw API intentionally does not impose a CLI `--execute` policy: its caller owns authorization to send signed envelopes.

## Signed send scenarios

Without `--execute`, signed commands perform read-only setup/snapshots and generate signed JSON plans and manifest rows, but never submit `/exchange`. They reserve nonces even for plans. Run again in a **new** root to execute, rebuilding signatures and confirming current preconditions.

```sh
# Plan; B must currently have role "missing".
uv run python -m probes --root captures/testnet/send-plan-001 send \
  --cases fresh,existing --initial-amount 2 --amount 0.1 --max-spend 20

# Explicitly authorized execution: fresh 2 USDC, then existing 0.1 USDC.
uv run python -m probes --root captures/testnet/send-live-001 send \
  --cases fresh,existing --initial-amount 2 --amount 0.1 --max-spend 20 --execute

# Separate, bounded follow-up subset; do not include fresh for now-existing B.
uv run python -m probes --root captures/testnet/send-errors-001 send \
  --cases replay,tamper,wrong-key,insufficient,bad-token,bad-source-dex,bad-destination-dex,bad-amount \
  --amount 0.1 --max-spend 20 --execute

uv run python -m probes --root captures/testnet/send-nonces-001 send \
  --cases old-outside,old-inside,future-inside,future-outside --max-spend 10 --execute
```

All accepted case names: `fresh`, `existing`, `replay`, `tamper`, `wrong-key`, `insufficient`, `bad-token`, `bad-source-dex`, `bad-destination-dex`, `bad-amount`, `zero-amount`, `negative-amount`, `old-outside`, `old-inside`, `future-inside`, `future-outside`, `old-interior`, `future-interior`. `--cases` order controls execution; duplicates/unknown names are rejected. `--recipient` overrides B's address. `--key-env` and `--wrong-key-env` name environment variables, not raw keys.

- `fresh` aborts unless recipient userRole is `missing`. `existing` requires an existing role; in plan-only mode an earlier planned fresh send may establish that hypothetical prerequisite. A rerun never silently reuses a fresh-account label after activation.
- `userRole` is authoritative for the freshness gate. A preTransferCheck HTTP error/null is retained and marked unavailable, not retried; the explicitly configured `--max-fee` reservation remains. A reported fee above `--max-fee` aborts. Default fee reservation is 1 USDC per request, including malformed and replay cases. `--max-spend` bounds cumulative positive principal plus these reservations; it is not a claim that an unknown server fee cannot differ.
- Token wire identity is discovered from captured spotMeta's USDC entry. Amounts use Decimal string wires. `tamper` changes the already-signed amount to half; a changed signature payload can recover an unrelated signer rather than produce an invalid-signature error.
- `wrong-key` and `insufficient` intentionally sign with B and send toward A. A different valid key is not intrinsically invalid. `insufficient` uses B's observed free USDC plus 1; it aborts if that hypothetical amount exceeds the remaining budget. This avoids attempting an unbounded amount from funded A.
- `replay` resends the exact prior fresh/existing envelope. If no such envelope exists in this run, it generates and submits a seed first, then submits the same bytes again. Both requests reserve hypothetical principal/fees against the budget.
- Near-boundary offsets are -2 days +/- 1 second and +1 day +/- 1 second. Despite the original “inside” names, both near-boundary cases were rejected. Reported bounds imply a 120-second inward admission margin. `old-interior` and `future-interior` instead use five-minute margins and were accepted. These are measured samples, not exact inclusive/exclusive boundary proofs. Future-dated accepted nonces affect retained nonce state.
- Before/after captures include sender and recipient balances, roles, pre-transfer checks, ledgers, fills, and fee schedules. Post-state exists only for executed cases. Exact errors and activation debits must be inferred from those captures, not labels.

## Signed IOC scenarios

```sh
# Choose an actual USDC-quoted universe name from the captured spotMeta.
uv run python -m probes --root captures/testnet/ioc-plan-001 ioc \
  --coin '@1' --side buy --notional 12 --max-spend 60

uv run python -m probes --root captures/testnet/ioc-live-001 ioc \
  --coin '@1' --side buy --notional 12 --max-spend 60 \
  --cases full,no-fill,minimum,size-precision,price-precision --execute

# Sell only owned base tokens, with fresh balance checks before each order.
uv run python -m probes --root captures/testnet/ioc-sell-001 ioc \
  --coin '@1' --side sell --notional 12 --max-spend 13 --cases full --execute
```

`@1` is illustrative, not a promise of liquidity. Omitting `--coin` chooses the first metadata USDC-quoted spot market and requires a two-sided book. Case names: `full`, `no-fill`, `minimum`, `size-precision`, `price-precision`, `partial-attempt`. Defaults omit `partial-attempt`.

- Official `sign_l1_action(..., is_mainnet=False)` signs already-constructed Decimal wire strings, avoiding SDK float rounding. Spot asset ID is `10000 + universe.index`.
- `full` crosses the observed best price by 0.5%; `no-fill` places a buy at half the observed bid or a sell at twice the ask. `minimum` aims at 1 USDC; when that rounds to zero size, it uses one size step only if that remains below 10 USDC. Precision cases add a size/price decimal within the configured cap. For PURR's integer lot at the captured price, use `--notional 20` for a full buy; the default 12 can fall below minimum after sizing.
- Every order is IOC, one order per envelope, with a unique tracked cloid. Every executed case captures cloid order status, oid order status when an oid is returned, fills, fees, and pre/post balances/ledger. A rejection may provide no oid; the harness never invents one.
- Source balance must cover the limit notional (buy) or actual currently owned base quantity (sell), plus conservative 1% source-token fee headroom. Fresh balances are queried before every case; a preceding buy does not imply its requested size was received. Cumulative limit notional plus the 1% reservation cannot exceed `--max-spend`; `--notional` is the per-order principal cap. The observed `userSpotCrossRate` must be available and between zero and 1%. Headroom is conservative, not a claim about the exact fee token or debit.
- Full/no-fill/partial labels are **intent, not deterministic outcomes**. Live books move. `partial-attempt` sizes just above observed top-level depth and is refused if that exceeds the per-order cap; deeper liquidity can still produce a full fill. Deterministic partial fills belong in the simulator's controlled book, not a live-testnet claim.

## Additional captured scenarios

```sh
# Zero-signature parser boundaries; cannot authorize a trade.
uv run python -m probes.shapes --root captures/testnet/shapes-002 --user "$HYPERLIQUID_ADDRESS"

# Explicit replay, never an automatic retry. This command preserves the nonce/signature.
uv run python -m probes.replay --root captures/testnet/replay-002 --execute \
  --user "$HYPERLIQUID_ADDRESS" --source captures/testnet/ioc-contract/ioc-full/request.body \
  --reverse-object-keys

# Equivalent wire representations were accepted through recovery (duplicate nonce).
# Use separate new run roots for --pad-order-decimals and --uppercase-cloid.

# A owns >=1 PURR; B is existing with <=5 USDC; controlled C is genuinely fresh.
# Returns B's USDC to A, sends1 PURR A->B, probes B->C activation, replays it,
# and returns the probe PURR to A when it remains at B.
uv run python -m probes.activation --root captures/testnet/activation-002 --execute \
  --recipient "$HYPERLIQUID_C_ADDRESS"

# Read up to30 books, then at most one IOC capped at25 test USDC.
# No suitable book means a recorded unavailable precondition, not a fabricated fill.
uv run python -m probes.partial --root captures/testnet/partial-002 --execute \
  --max-notional 25 --max-markets 30
```

Replay remains a potential mutation: previous failure does not prove nonce consumption. It checks per-envelope bounds and captures before/after state. The activation scenario changes balances as documented; it is not a read-only check. A repeat needs a newly generated, controlled C because freshness is a chain-state precondition.

The original bounded partial-fill scan found no suitable thin market and submitted no order. Later `orders-rounding-partial` and `matching-contract` captured positive partial IOC executions with terminal `filled` status. `orders-multilevel-02` and `orders-multilevel-04` additionally captured two-level execution and distinct exchange-average/aggregate-fill price precision.


## Calibrated boundary campaigns

These commands submit signed actions and require the existing controlled accounts and current on-chain preconditions. They are not automatic retries or plans. Every root must be new; a precondition failure preserves evidence and stops rather than inventing an outcome.

```sh
# B must start with zero quote/base balance. Move one USDC wei A->B,
# probe execution-price minimum precedence without affording a base lot,
# then return only that bounded seed.
uv run --env-file .env python -m probes.late_campaign \
  --root captures/testnet/minimum-funded-next --cases minimum-funded --execute

# Existing zero-funded B; each signature is guarded against affording one lot.
# Band-only probes permit an ask-only book, unlike execution campaigns.
uv run --env-file .env python -m probes.late_campaign \
  --root captures/testnet/band-edges-next --cases band-nq,band-test8 \
  --band-edges upper-equal,upper-inside,upper-outside,lower-equal,lower-inside,lower-outside \
  --execute

# Opt-in TEST11 two-level buy: at most 30 USDC limit principal,
# with the separate 31-USDC reservation covering the 1% headroom.
uv run --env-file .env python -m probes.order_campaign \
  --root captures/testnet/multilevel-next --cases multilevel \
  --coin '@49' --max-spend 31 --execute
```

The multilevel candidate is captured best ask depth plus one base lot. It must fit the per-order cap, cumulative reservation, current wallet balance, and second-level liquidity. Books can change after observation; only actual fills classify the outcome. Late-campaign cases also include read-only `recovery` and separately selected batch, price, reference, valuation, fee, and decimal discriminators; inspect `--help` and the source guards before selecting a mutation. Live upgrade-only rejections remain immutable outcomes, not invitations to resubmit.

## Archived quote and activation calibrations

The bounded one-off drivers are preserved as `scenario.py` in their immutable capture roots, not as additional permanent CLI subcommands. Run them from the repository root with the pinned environment. They require `--execute`, refuse existing output roots, preserve failures, and do not retry:

```sh
# Requires a newly provisioned controlled recipient under this environment name.
uv run --env-file .env python captures/testnet/activation-priority/scenario.py \
  --root captures/testnet/activation-priority-next --principal-token NQ \
  --e-key-env HYPERLIQUID_FRESH_PRIVATE_KEY --execute

# Existing, empty B; acquires at most16 USDEEE with a15-USDC reservation.
uv run --env-file .env python captures/testnet/quote-accounting/scenario.py \
  --root captures/testnet/quote-accounting-next --execute
```

The activation driver also accepts `--principal-token USDC` or `PURR`; each fresh activation needs a different recipient. It transfers one principal unit, permits one activation-gas unit, then returns principal. The fallback and alternate-gas roots preserve both `scenario.py` and `priority-helper.py`; pass the latter with `--helper`. They use B/H environment roles and require empty existing B and genuinely fresh H. **A–H are already activated**: use a separate protected environment with a new controlled H, never overwrite the retained H key or treat a new directory as account freshness.

`quote-accounting/declared-bounds.json` is the complete spend contract: one funding order; at most two one-wei USDEEE transfers without activation; three deliberately unaffordable B IOCs; a1.28-LMA buy capped at14.5 USDEEE principal; and a bounded sell retaining at least0.02 LMA. It spends only acquired inventory and authorizes no cleanup trades.

`quote-volume-usdyp/scenario.py` permits at most two buys, reserves at most13 USDC, acquires at most12 USDYP and16000 KRWIN, and caps the second limit principal at11 USDYP. Its original run halted after a filled acknowledgment followed by `unknownOid`; no order was retried. The separate `quote-volume-usdyp-recovery` is read-only reconciliation, not authorization to resubmit. Inspect the original abort and recovery before any further mutation.

`fee-share-one` and `fee-share-half` use the existing IOC CLI on `@1106` and `@1140`. Their actual fills, not requested intent or deployer-share names, establish volume behavior. Live liquidity and fee context remain preconditions; offline golden replay is the repeatable deterministic check.

## Nonces and reruns

The default shared nonce journal is `captures/testnet/nonces.jsonl`; `--nonce-file` overrides it. It is append-only, OS-locked while reserving, and fsynced before signing/submitting. Rows contain public signer, endpoint, unique reserved nonce, intended offset, case ID, and timestamp. Uniqueness is enforced per signer/endpoint within the journal; always reuse the same journal for that account. Exact replay deliberately reuses its prior envelope/nonce. The journal is not proof of network acceptance and cannot detect unrelated clients using the same account. Do not delete/reset it casually or share an account with another writer during experiments.

Immutable root-level `<case>.signed.json` files and generated-case manifest events are persisted before exchange I/O. Completed rows append rather than replace pending rows. A new run captures current chain state; replaying case definitions does not reset balances, activation, fees, book depth, or nonce state. Fresh-account behavior requires a new controlled recipient; simulator reruns can instead explicitly reset test state outside these live commands.

Sources: [official SDK signing](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/signing.py), [official SDK exchange wire construction](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/exchange.py), [pinned SDK release](https://pypi.org/project/hyperliquid-python-sdk/0.24.0/). See [verification.md](verification.md) for offline replay and evidence auditing.
