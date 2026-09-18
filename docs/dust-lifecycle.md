# Controlled PURR dust lifecycle

`probes.dust_lifecycle` captures a bounded A/B/D comparison of an existing holder's residual sub-lot, an existing recipient's new sub-lot, and an existing recipient's whole-lot control. It does not install a simulator rule or presume why the previously removed 0.9972 PURR disappeared.

## Run contract

Only the parent/operator executes this campaign, serially with all other account mutations stopped. A, B, and D must have no other activity for the entire run, including orders, transfers, or automated jobs. The nonce journal prevents local nonce reuse, not outside activity or concurrent account use.

Use the existing controlled signers in `HYPERLIQUID_PRIVATE_KEY` (A), `HYPERLIQUID_B_PRIVATE_KEY` (B), and `HYPERLIQUID_D_PRIVATE_KEY` (D). Keys are loaded only through the existing SDK `wallet()` helper. Never pass keys as CLI values or log them. Three distinct existing ordinary user accounts are required; the script neither generates wallets nor activates recipients. It uses testnet only and has no USDC purchase, faucet, external recipient, or order path.

```sh
uv run --env-file .env python -m probes.dust_lifecycle --root captures/testnet/dust-lifecycle-01 --execute
```

`--root` must not exist. `--execute` is mandatory; there is no signed-plan-only mode for this dependent workflow. `--nonce-file` defaults to `captures/testnet/nonces.jsonl`; keep the same journal used by the other campaigns. Every action gets a persisted reserved nonce and an immutable signed envelope before its one permitted submission.

`--max-inventory` sets the original-inventory and per-send PURR cap (integer 3–20, default 10). Use `--max-inventory 20` when A's captured balance has grown above 10 PURR. This also sets the cumulative gross transfer bound to twice that cap; it does not relax the zero-initial-PURR requirements for B/D.

The default `--poll-seconds` is `0,10,30,60,120,300,600`. Custom plans must have 2–32 strictly increasing integer offsets, begin at zero, and end at or before 1200 seconds. The bound applies to scheduled observation offsets, not total wall-clock runtime: raw HTTP recording, request pacing, sequential A/B/D capture, full audits and returns take additional time. Due offsets are not silently dropped. Actual capture windows and elapsed times are recorded instead of pretending the requests occurred exactly on schedule.

## Safety and sequence

1. Capture the existing full snapshot for each actor: balances, role, pre-transfer check, non-funding ledger from time zero, fills and fees. Require A to hold at least 3 free PURR and no more than `--max-inventory`, B/D to hold zero total PURR, and no actor to have held PURR. Metadata must describe whole-PURR market lots and a fractional transfer wei. Historical fills and ledger are captured as returned by their endpoints; the script does not pretend a potentially capped endpoint response is complete all-time history.
2. Compute `floor(Afree)`. Refuse before mutation unless this leaves A strictly between zero and one PURR. Send the whole-PURR amount A→B (`setup-a-to-b`). A must still display a positive sub-lot in the post-capture; otherwise halt with the raw evidence. The stated starting A balance of 5.9958 would plan 5 PURR and leave 0.9958, but the current captured balance—not a hard-coded value—controls the run.
3. Send 0.5 PURR B→D (`setup-b-to-d`), retaining at least one whole PURR in B. A rejected setup halts. D being omitted immediately after an accepted transfer remains evidence rather than being discarded.
4. Poll A/B/D balances using deterministic IDs `poll-NN-SSSSs-actor-balances`. Schedule offsets are relative to the capture of the B→D mutation response. Record a separate conservative mutation request/response window for A's residual and for B/D. Every actor's balance request has its own capture start/end and actual poll elapsed time; these are not server deletion timestamps.
5. Capture the final full snapshots after polling and before spendability probes. Full ledger/fill queries occur only at this first/final audit boundary, not on every poll. If the final response contains new fills, halt rather than proceed with contaminated inventory. Inspect first/final raw ledger responses for unrelated activity as well; a finite response is not proof that no outside action occurred.
6. Attempt exactly one PURR wei A→B and D→B (`spend-one-wei-a-to-b`, `spend-one-wei-d-to-b`), even if their balance rows are omitted. The metadata's `weiDecimals`, not market lot size, determines this amount. Record omission separately from whether the exchange accepted or rejected spending, preserving the raw rejection. A generic rejection is not automatically called deletion.
7. Attempt to return displayed remaining probe PURR D→A then B→A. If D is omitted but its one-wei spend succeeded, attempt its accounted remaining inventory once. If B is omitted, attempt its accounted remaining inventory once so display filtering cannot silently strand the control balance. An omitted D whose tiny send was rejected is reported as unavailable rather than retried. Each return has fresh pre/post balance captures and a distinct persisted nonce. Observe final balances for all three actors.

Bounds are enforced separately: **original PURR inventory and each send <=`--max-inventory`**, and **cumulative worst-case gross transfers including returns <=2×`--max-inventory`**. Defaults are 10/10/20 PURR; `--max-inventory 20` permits 20/20/40 PURR. Gross reservation includes rejected attempts and is never replenished. Returning the same tokens is circulation, not additional net spending. `plan.json`, action metadata and `results.json` expose the bounds; results distinguish reserved gross from accepted gross. The script never drains preexisting B/D PURR because any nonzero initial balance aborts before mutation. Before every send it rejects newly observed balances above the accounted inventory or any PURR hold. The no-outside-actions assumption remains essential, since a coincident external credit could replace missing dust without exceeding an inventory cap.

## Evidence and failure handling

`Recorder` is the only transport and preserves request/response bytes, HTTP status, trace and timing. Every signed send has A/B/D balance snapshots immediately before and after its response. Full snapshots are deliberately limited to the first/final audit. Captures are sequential, not atomic.

`results.json` records each actor's observation sequence and intervals from the last positive capture to the first zero/omitted capture, or `no_observed_removal`. If omission is already present at the first post-mutation capture, the lower bound is the start of the mutation request window, not a fabricated last-positive timestamp. No post-mutation capture is reported separately. The bounds conservatively span client capture windows. Reappearance remains visible in the sequence and later transitions are retained. `spendability` pairs source omission with accepted/rejected one-wei sends and recipient pre/post captures; `returns` records each return or why none was available. Accounted inventory is an upper bound, not a claim that omitted tokens remain spendable.

Any transport failure, HTTP429, HTTP5xx, missing HTTP status or interrupt stops immediately, with no automatic cleanup, retry, replay, or new HTTP request. The pending action and raw recorder evidence remain available; ambiguous action records are not converted into a rejection. Other unclassified exchange responses likewise halt. An ordinary explicit exchange rejection in a tiny probe or return is recorded, and the remaining independent steps can proceed; it is never retried. On a halted run, inspect existing evidence and make a separately authorized recovery plan rather than rerunning this script against the same root or changed B/D balances.

A completed workflow is evidence only for those actors, amounts, captured windows and spend attempts. Comparing A/D with the B whole-lot control can discriminate observed account-specific or delayed behavior within the window, but it cannot establish a universal expiry threshold or deletion policy. Repetition requires a new capture root and all stated balance/account preconditions again; no automatic rebuy or dust replenishment is provided.

This deliverable intentionally performs no execution, tests, build, lint or formatting; the parent owns live execution and validation.
