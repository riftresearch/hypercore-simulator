# Late live parity campaign

`probes.late_campaign` records recovery, batch, price-band, reference/minimum-notional, transfer-valuation, fee-rounding and decimal-parser discriminators. It does not install simulator rules or label a hypothesis as a live conclusion. Only the parent/operator executes it, **after dust recovery restores A's inventory**, with all other A/B/C activity stopped. No random wallet, activation funding, faucet or balance drain is used.

## Invocation

Run from the repository root with the existing controlled signers already exported through the usual secure environment mechanism. A is `HYPERLIQUID_PRIVATE_KEY`; B is `HYPERLIQUID_B_PRIVATE_KEY`; C is `HYPERLIQUID_C_PRIVATE_KEY`. The script never reads an environment file or prints keys. Recovery alone needs no private key: A's public address comes from the original capture metadata.

```sh
uv run python -m probes.late_campaign --root captures/testnet/late-contract --execute
```

`--cases recovery,batch,price,fee,decimal` remains the default for compatibility. The complete case set additionally includes opt-in `reference` and `valuation`; a unique comma-separated subset runs in supplied order. Signed cases require `--execute`; `--cases recovery` is read-only and may omit it. There is no signed-plan-only mode. `--root` must be new or empty and is never reused. `--nonce-file` defaults to `captures/testnet/nonces.jsonl`; keep the shared existing journal. It reserves and fsyncs a fresh nonce per signer before every submission, including each return. Never reset that journal to rerun a rejected probe.

```sh
uv run python -m probes.late_campaign --root captures/testnet/late-reference-01 --cases reference --execute
uv run python -m probes.late_campaign --root captures/testnet/late-valuation-01 --cases valuation --execute
```

The Recorder is the only HTTP transport, with its normal pacing and byte-preserving immutable request/response, HTTP status, trace and timing captures. There are no parallel requests, retries, replay, polling loops or cleanup requests after an ambiguous failure. The campaign halts on a transport failure, missing status, HTTP429, HTTP5xx or interruption, even within a snapshot's optional pre-transfer endpoint. Other missing required reads also halt.

## Cases

### Recovery (no mutation)

The original `request.body`, `response.body` and `pending.json` from `captures/testnet/rounding-canonical/rounding-buy` and `captures/testnet/quote-activation/buy-quote` are copied verbatim into the new root with source paths and SHA256 provenance events. Originals are never edited or resubmitted.

Capture A's full snapshot (balances, role, pre-transfer check, non-funding ledger from zero, fills and fees). Query original rounding order status by both returned oid and original cloid, and quote-activation purchase status by its returned oid. That original quote purchase had no cloid; none is invented. These are terminal-status recovery observations, not an assertion that a successful exchange response proves a final fill. Capture separate fresh `spotMetaAndAssetCtxs` and `l2Book` responses for PURR, @49 and @1302; source contexts are not inferred from the exchange's rejection strings.

### Batch (C, three signed IOC batches)

C must already be an ordinary user, have zero total PURR, no held USDC, and 0–2 total USDC. The captured best ask must exceed C's entire quote balance, so C cannot afford a whole PURR lot at that observation. Require whole-PURR lots, best ask above 4 and at most 5, and both prices 4 and 5 inside the 20%–180% bands for mark, mid and best ask.

Submit, serially:

1. Buy 3 PURR at 5, then the otherwise identical invalid size 3.1.
2. Invalid size 3.1 first, valid 3 last.
3. Two individually valid runtime-rejection candidates: buy 3 at crossing 5, then buy 3 at noncrossing 4.

Each batch gets fresh contexts/book, full before/after snapshots and unique cloids for both orders. Query **every cloid**, including whole-batch validation rejections; query returned oids when available. Observations distinguish all-batch prevalidation from sequential statuses without assuming either outcome. The size-invalid order is intentionally not normalized.

### Price (C, seven signed IOC probes)

The same C guards apply. For each reference (context mark, context mid, independent best ask), independently recapture a full before snapshot, contexts and book, then select one legal price strictly below and one strictly above 1.8 times that probe's fresh reference. Quantization obeys spot decimal places and at most five significant digits. Every order has size 2; refuse any candidate at or below the current ask or below 10 USDC limit notional. Price levels on opposite sides are based on different fresh observations, not falsely described as one atomic market snapshot.

Finally submit price 0.1, size 100 only if 0.1 is below all three observed 20% lower bands. This is an 80%-distance-error discriminator, not a promised error string. Capture full after snapshots and cloid/returned-oid statuses for each order.

The rejection probes' balance guard is contemporaneous evidence, not a market lock: a subsequent drastic price fall could make a lot affordable. C's existing <=2USDC balance is the ultimate financial bound. All orders are IOC, never resting GTC orders.

### Reference (C, up to eleven signed IOC probes)

The existing immutable `captures/testnet/late-contract` price evidence remains interpretable as the original size-2/1.8-reference experiment: limits around8.24–8.33 produced `minTradeNtlRejected` despite limit notional above16, while0.1×100 reached an80%-reference guard and unknown cloid. These observations motivate a reference-capped minimum hypothesis; they do **not** establish the chosen reference, cap method, side treatment or all admission precedence.

Before **each** new action independently snapshot C and capture fresh contexts/book. Reapply existing-user, zero total PURR, no held USDC, total USDC<=2 and inability to afford one whole PURR at the captured ask. Query every submitted cloid and every returned oid after full post-state snapshots.

- Buy3 at100 and sell3 at100 discriminate side treatment and upper-band behavior.
- Buy2 at5, buy3 at3.4 and sell2 at100 distinguish minimum valuation using `min(limit,reference)`, reference alone and side-dependent caps. These are hypotheses only.
- For each reference pair mark/mid, mark/ask and mid/ask, buy3 at a canonical midpoint strictly between5 times those fresh references.
- For the same three pairs, buy `ceil(12/price)` whole PURR at a canonical midpoint strictly between0.2 times the fresh references; require that price to be below the captured ask.

Midpoints obey spot decimal precision and at most five significant digits. Coincident thresholds or intervals with no representable strict midpoint produce a recorded `precondition_unavailable`, not an invented discriminator or retry. Every action uses its own fresh reference values; comparisons are not falsely atomic. C's <=2USDC inventory remains the hard financial bound if the book moves; its zero PURR prevents funded sells. No rejection label is predetermined.

### Valuation (A ↔ existing B, exact one-token round trips)

Require existing ordinary A and B, A>=3 free PURR and>=1 free NQ, and B initially zero total PURR **and** NQ. Validate @1302=NQ/USDC. The case refuses to replenish inventory or activate an account.

Send exactly1 PURR A→B, then exactly1 PURR B→A; next send exactly1 NQ A→B and exactly1 NQ B→A. For **every** send, capture full bilateral before snapshots, verify current inventory/roles, persist its nonce and prepare the envelope, then capture independent `spotMetaAndAssetCtxs` and `l2Book` as the last network reads immediately before submission. Retain full bilateral after snapshots (including balances/entryNtl, ledger, fills and fees). Context/book provenance is stored with the pending request, never reconstructed from its resulting basis change.

Require the successful default response and exact opposite one-token total deltas after every leg before any next mutation. B must have exactly the observed one-token total and sufficient free inventory before return. A changed guard, rejected/ambiguous response, mismatched delta or transport failure halts without retry or cleanup, potentially leaving the already observed forward credit at B. Transfer valuation against mark, mid or book is explicitly a hypothesis to compare with raw snapshots, not a formula assumed by the probe.

### Fee (A, three controlled IOC orders)

Validate @1302=NQ/USDC and @49=TEST11/USDC against current metadata. Before each order record full account state and independent contexts/book:

1. Buy exactly 10 NQ at the observed best ask; require 10–15 USDC limit cost and at least 15 free USDC.
2. Sell exactly 10 NQ at the newly observed best bid; require 10–15 USDC captured limit notional and sufficient currently available NQ. A previous buy is never presumed filled.
3. Sell TEST11 at its newly observed best bid. Round up the size needed for 10 USDC to a legal lot and require resulting captured notional <=15 USDC. Size cannot exceed either currently available TEST11 or the original controlled rounding purchase's 1.57 quantity. No replenishment purchase is made.

Buy limit notional is <=15 USDC; the two sells reserve <=30 USDC combined **at their captured limit prices**, from bounded controlled inventory. **An IOC sell limit cannot impose an upper bound on realized proceeds:** better bids appearing before matching can increase gross above that number. The campaign records observed per-order fill gross and aborts if it exceeds 15. It cannot retroactively prevent price improvement. If acceptance requires a hard <=30 realized-gross guarantee rather than this captured-price/inventory bound, do not execute the fee case without resolving that exchange-order-type limitation. Likewise, buy quote principal is limit-bounded; fee-token observations must establish any separately charged fee rather than assuming all-in spending equals principal.

Full before/after snapshots include fills and userFees. Query every cloid and returned oid, recording matched fill entries and observed gross even if no fill occurs. Endpoint-limited histories are not claimed complete. Metadata records base/quote wei precision and these **hypotheses only**:

- Quote fee units: `floor((qty * px * 10**quoteWeiDecimals) * f64(rate))`.
- Buy base fee units: `floor((quoteFeeUnits / 10**quoteWeiDecimals / px) * 10**baseWeiDecimals)`.

Actual fills, fee token, rate context and balance deltas decide interpretation. Aggregate responses or an IOC request alone are not proof of execution, per-fill rounding or a fee model.

### Decimal (C → existing A, observed-credit-only returns)

Capture full before/after snapshots of **both** accounts for each forward send. Require C's 0–2USDC, zero-PURR/no-held-USDC state and sufficient quote balance for the declared cap; A must already exist and match the captured controlled-inventory owner.

1. Send raw `0.00000000000000000000000000011e28` unchanged. Its intended decimal value is 1.1; require C>=1.1, cap 1.1USDC. Do not canonicalize the scientific string before signing.
2. If guards still permit, send malformed raw `0.000_000_1` unchanged, with declared cap 0.0000001USDC, to discriminate parser acceptance only. No parser compatibility shim is supplied.

Forward declared exposure is <=1.1000001USDC. A successful default response does not authorize draining A: compute only the **observed USDC total credit delta** against its immediately preceding snapshot, require it to be nonnegative, <= that forward cap and available, then submit exactly that amount back A→C using A's signer and a new journaled nonce. No return can exceed 1.1USDC. Zero observed credit authorizes no return. Capture both full returned snapshots and require the return to reconcile exactly; rejection or mismatch halts without another attempt. Out-of-cap observed credit/debit, a rejection with balance movement or an unclassified response halts rather than guessing. A malformed parser is under investigation, so the declared cap is an input guard and checked postcondition, not a promise that an unknown remote parser cannot misinterpret it; C's <=2USDC balance is the ultimate source bound.

An accepted forward send with zero observed credit halts for separately authorized read-only recovery, rather than continuing to the next mutation or assuming eventual credit.

Returns circulate only observed credited funds and do not enlarge the net-spend cap. The journal prevents local nonce reuse, not outside account activity; manual transfers, trading bots and sibling campaigns must remain stopped throughout the before/after/return windows.

## Completion and partial abort

`results.json` contains completed case names, action responses, status observations, fee hypotheses and bounded-action metadata. `partial-abort.json` instead identifies the active case, last attempted request, completed cases, recorded actions and exact abort reason. An action left `submission_pending` is ambiguous, not rejected. The immutable signed envelope and raw request/response remain available even if later snapshots or status reads fail. No report claims a submitted purchase, returned transfer or terminal order state without its corresponding observation.

On abort, stop mutations and inspect captured evidence. Resolve ambiguity through separately authorized read-only queries; never resubmit the original scientific transfer, rerun a used root, retry a failed return, or drain a displayed balance. This campaign has no automatic recovery after transport/rate-limit failure.

Implementation delivery deliberately performs no live execution or validation; parent owns execution and validation.
