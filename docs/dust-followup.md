# PURR dust follow-up

`probes/dust_followup.py` is a one-shot testnet experiment. It does not implement a simulator dust sweep. Run serially after the late-contract campaign, with no other activity on A, B, or D:

```sh
uv run python -m probes.dust_followup \
  --root captures/testnet/dust-followup-01 --execute \
  --source captures/testnet/dust-lifecycle-01 \
  --authorized-run captures/testnet/late-contract
```

The output root must not exist. `--source` defaults to the old dust root. `--authorized-run` is optional and repeatable: only completed, hash-verified request/response captures can explain additional fills. A successful exchange response OID must match the submitted order cloid and signer, and the exact fill must appear in a captured `userFills` response for that signer. It is not a coin whitelist. Authorized runs may not explain PURR trades. All remaining unknown fills halt the workflow.

Keys are resolved only by `wallet(env)` for `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_B_PRIVATE_KEY`, and `HYPERLIQUID_D_PRIVATE_KEY`. The signers must match the immutable old plan. Nonces are durably reserved in `captures/testnet/nonces.jsonl` (overridable with `--nonce-file`) before signing. No activation is allowed.

## Correct interpretation of the old capture

The old workflow's “new fills contaminate” abort was a classification error, not evidence of foreign trading. Its immutable source remains untouched. At `1789714860046` (`2026-09-18T07:01:00.046Z`), A's `0.99019` PURR and D's `0.5` PURR converted under OID `60415128044`. Both fills have direction `Spot Dust Conversion`, zero hash, fee zero, and tid zero. B retained `12.5` PURR.

The owned aggregate was `1.49019` PURR. A one-lot sale at `4.5795`, charging the known `0.0007` taker rate, gives:

- Gross: `4.5795` USDC.
- Fee: `0.00320565`; net: `4.57629435` USDC.
- D: floor to USDC wei of `net × 0.5 / 1.49019` = `1.53547344`.
- A: the remainder, `3.04082091`.

Both credits match the captured old initial-to-final balance changes exactly. This is a known-price reconciliation, not independent proof of the old execution-time book or absence of other network dust. The observed testnet conversion is 46 ms after a UTC minute boundary, and each owned balance was worth more than $1 at that price. Do not substitute mainnet documentation's daily 00:00 UTC/at-most-$1 rule for this testnet observation.

## Bounded sequence

1. Capture fresh A/B/D state, book, spot contexts, and recent trades; retain old-OID statuses and fills-by-time diagnostics. `recentTrades` is a recent feed, not a historical query: missing old trades and the inability to reconstruct an old book are explicit gaps.
2. Validate the old automatic fills and USDC deltas. Require current full B PURR inventory to equal the old final `12.5`, A/D PURR to be zero, no relevant holds, and D's current USDC to equal the old final balance.
3. Submit exactly one one-wei PURR send from each of A and D to B. Both must return an explicit insufficient-balance rejection, with unchanged balances. No retries.
4. Recover only B's `12.5` PURR to A and D's proven `1.53547344` USDC credit to A. Verify each successful transfer against both actors' ledger entries by nonce, asset, amount, sender, destination, dex, and zero fees.
5. Capture the before-market diagnostics. Start setup just after a UTC minute boundary: A sends `12` PURR to B, then B sends `0.5` to D. The controlled balances are A `0.5`, D `0.5`, B `11.5`. A conversion during setup is preserved as an incomplete experiment and aborts without cleanup, not labeled foreign contamination.
6. Poll A/B/D balances and the book on five-second scheduled offsets for at most 180 seconds. Request windows, actual offsets, visible dust rows, and B's control balance are retained. During polling only, request-start spacing is explicitly recorded and reduced from 1.5 to 1 second so four sequential observations fit a normal five-second tick. Slow responses or conversion audits can still overrun a tick; actual times, not idealized times, are reported. Slow in-flight requests and final read-only diagnostics can extend wall-clock runtime beyond the observation window. HTTP 429 still halts immediately without retry.
7. Balance disappearance alone never establishes conversion. Require full owned A/D automatic fills, matching OID and time, zero dust balance, positive isolated USDC credits, and no unknown fills or ledger changes. Capture both OID statuses and final full snapshots. If both conversions were not observed within the polling window, report without automatic cleanup, even if final diagnostics see a later conversion.
8. Only after that proof, return B's `11.5` PURR and the new D USDC delta to A, again with persisted nonces and bilateral ledger proof. Never return unrelated holdings.

Every PURR transfer is at most `13`; cumulative reserved PURR, including rejected one-wei attempts, is capped at `50` (the planned total is `36.50002`). USDC returns are separately bounded to the old proven credit or isolated new D credit. Recovery is performed before the new experiment; timeout does not undo it.

## Interpretation and abort evidence

`results.json` records phase, status/reason, all attempted mutations (including ambiguous ones), nonce proofs, old-credit arithmetic, full conversion fills/statuses, poll windows, final observed state, and independent captured-book comparisons. Every request and signed envelope also retains the usual immutable recorder evidence.

The new prediction uses the controlled input of `0.5 + 0.5 = 1` PURR, **not a quantity inferred from the observed credit**. Each captured eligible book independently prices a one-lot sell against visible bids. Gross proportions and fee-net proportions are compared separately. Decimal-floor and binary64-floor fee alternatives, per-actor rounding residuals, total conservation residuals, and D-floor/A-remainder predictions are explicit. Matches are conditional evidence: the system execution fee profile, other network dust, and the exact intervening execution book are unobserved. A mismatch is reported, not “fixed” by fitting a price or an aggregate input to post-balances.

Transport errors, HTTP 429/5xx, unknown fills, unknown ledger activity, changed ownership guards, and unclassified action outcomes halt immediately. There is no retry or cleanup in an exception handler. Inspect `phase`, actions, and raw captures before any separately authorized recovery; never rerun into the same root or blindly repeat an ambiguous send.

The probe was prepared without execution, validation, or key reads; the parent campaign owns execution and verification.
