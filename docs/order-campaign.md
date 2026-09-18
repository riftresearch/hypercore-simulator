# Signed IOC order campaign

`probes.order_campaign` collects one-shot evidence for cloid lifetime, batch admission precedence, fee rounding, partial IOC terminal status, and multiple fills/prices. It does not implement simulator behavior or assert parity. Run one slice at a time against the funded account, inspect the captures, and only then schedule another slice. Do not run other mutations concurrently against that account.

## Invocation and limits

The existing official SDK signs every action; `Recorder` is the only HTTP transport. Only testnet or an explicit loopback `--base-url` is accepted. The SDK is pinned by the repository. Keep signing keys in the environment; never put a key in command arguments, captures, or logs. `--key-env` names an environment variable (default `HYPERLIQUID_PRIVATE_KEY`), not a key value.

```sh
uv run --env-file .env python -m probes.order_campaign --root captures/testnet/order-cloid-01 --cases cloid --execute
uv run --env-file .env python -m probes.order_campaign --root captures/testnet/order-batch-01 --cases batch --max-spend 150 --execute
uv run --env-file .env python -m probes.order_campaign --root captures/testnet/order-rounding-01 --cases rounding --execute
uv run --env-file .env python -m probes.order_campaign --root captures/testnet/order-partial-31-180 --cases partial --market-offset 30 --market-count 150 --execute
uv run python -m probes.order_campaign --root captures/testnet/order-multilevel-01 --cases multilevel --coin @49 --max-spend 16 --execute
```

Each root must be new or empty. The comma-separated `--cases` value is an ordered unique subset of `cloid,batch,rounding,partial,multilevel`; its default remains the first four. `multilevel` is opt-in and requires explicit `--coin @49`. `--coin` selects one exact USDC-quoted spot universe name, restricting the rounding/partial scans as well as selecting the cloid/batch market. Otherwise cloid/batch use the first USDC market, and scans use the configured window in the USDC market list sorted by index. The zero-based `--market-offset 30` skips precisely the first30 USDC markets used by the existing partial probe. `--market-count` defaults to30 and is capped at150; no market is revisited within a scan. A supplied `--coin` bypasses that offset/window and reads only that market.

Without `--execute`, the campaign still captures read-only state/books and reserves nonces, but only writes signed plans. Dependent actions requiring an observed fill, including cloid reuse/replay and the rounding sell, are explicitly unavailable in this mode. Signed plans are sensitive operational artifacts: do not submit them manually without applying the same budget and balance checks.

`--max-spend` defaults to100 USDC. It caps **cumulative potential limit-price quote notional times1.01**, not observed net spending. Every order, including malformed variants, has10–25 USDC limit notional. The exact signed replay reserves its full potential fill independently. Sells also consume this quote-notional budget; sale proceeds and rejected orders never replenish it. Each action independently checks fresh observed free source-token balances, including1% source-token headroom and the sum of all orders in a batch. An observed fee rate above1% stops the run. No amount calculation uses binary floating point.

The two five-order batch arrays need strictly more than101 USDC of reservation in total, depending on market lot size. Consequently the default100 budget cannot run both arrays: it captures the empty/grouping cases and records `precondition_unavailable` for the arrays without submitting either. Use a separately scheduled `--cases batch --max-spend 150`; coarse market granularity may still make that insufficient, in which case the recorded required reservation explains the refusal. Running all cases with100 is allowed but does not promise that every slice's preconditions/budget fit.

No exchange submission is automatically retried. Existing recorder pacing remains in force. Fresh nonces are persisted before submission in the shared `--nonce-file` (default `captures/testnet/nonces.jsonl`). Exact replay retains the original envelope/nonce and records its distinct reservation before submitting. Ambiguous transport outcomes stop the workflow after an attempted read-only post-observation; HTTP429/5xx also stop without retry.

## Cases and evidence

### `cloid`

1. Capture a book and construct a10–15 USDC buy, targeting12 USDC with a small bounded crossing limit and a new cloid.
2. Only after an observed positive fill, submit the exact same order/cloid with a fresh nonce. This can cause a second fill; it is not treated as free.
3. Submit the original signed envelope unchanged as a separately budgeted exact replay.

After every submission the campaign captures post-state/fills, queries the cloid, and queries every numeric oid discovered so far from exchange replies or cloid lookups. Thus a second fill cannot hide the first order's terminal status. Compare both returned oids and the cloid mapping to determine lifetime/reuse behavior. No positive first fill means the dependent probes are unavailable, not successful. The already measured uppercase-canonicalization case is intentionally not duplicated.

### `batch`

Two zero-order signed actions independently probe `orders:[]` with `grouping:"na"` and `grouping:"invalid"`. Neither can create financial flow, even if the invalid grouping unexpectedly passes admission. Empty-order/grouping interaction is visible in the evidence; an error alone does not establish which validation ran first.

A captured two-sided book determines a buy limit at roughly half the best bid. Two arrays contain the same five semantic variants, in forward and reverse order, with fresh per-order cloids:

- valid no-crossing IOC with at least10 USDC notional;
- invalid market size precision (one tenth of a size lot added, within eight wire decimals);
- invalid market price precision (within eight wire decimals, so the intent is market validation rather than the previously captured ninth-decimal HTTP failure);
- `reduceOnly:true`;
- a positive integer asset sentinel absent from captured spot metadata.

All five candidates retain10–25 USDC potential notional, including malformed ones. The known-market limits are below the captured best ask, but live prices can move: every candidate is still fully budgeted and balance-guarded as if it could execute. If the selected market cannot isolate the precision faults within these bounds, the arrays are explicitly unavailable. The invalid asset is not assumed to permit any financial flow and is nevertheless conservatively reserved using the selected market's USDC notional.

Inspect response status ordering, state/fill changes, and each cloid lookup to distinguish whole-action versus per-order rejection and whether earlier orders commit. Any oid discovered by a cloid lookup is also queried numerically, allowing rejected-order retention to be distinguished from unknown cloids. Forward/reverse evidence explores precedence among these variants; it is not exhaustive proof of every permutation.

### `rounding`

The bounded scan considers USDC-quoted markets with base `szDecimals>=2`. It requires a size lot whose fee at the observed `userSpotCrossRate` is not exactly representable at base `weiDecimals`. A10–15 USDC buy size (near12) is chosen so its unrounded total base fee also needs rounding, with enough captured depth at the selected best-ask limit. Metadata, fee rate, raw hypothetical fee, quantum and source book are retained. Actual fills, including per-fill fee fields, decide what rounding happened; book stability and a full fill are never assumed.

After an observed positive buy fill, compute newly acquired free base from actual pre/post balances, thereby incorporating actual fees. A refreshed book/balance snapshot determines whether some of that acquired balance can safely fund a sell of at least10 and at most15 USDC with1% base headroom. The sell never borrows against expected proceeds or spends preexisting base instead of the newly observed acquisition. If minimum notional or available balance fails, record an unavailable sell and leave the acquired balance untouched by this slice. Buy/sell fills and snapshots support comparison of fee rounding, sold basis, remaining basis and quote proceeds.

### `partial`

The read-only scan captures each source book in the requested window. A candidate must have strictly less than25 USDC total ask depth at its exact best ask. The IOC limit is **exactly** that best ask, and requested lot-aligned size must exceed the sum of all captured ask size at that price while remaining10–25 USDC. Source book ID/time, visible size/notional, requested size and the explicit precondition are recorded before the one permitted attempt.

A moving live book can produce a full fill, no fill, rejection, or actual partial fill. Only captured fills/order status establish the outcome. If no book fits the scan, size, and remaining budget bounds, record `precondition_unavailable` and submit no partial action. The scan never widens or restarts automatically.

### `multilevel` (explicit @49 only)

Capture a fresh @49 book and require two distinct ask prices. Sum all visible quantity at the best ask; request exactly that quantity plus one base lot, with IOC buy limit equal to the second ask. Require a canonical lot-aligned quantity and legal limit price, at least one lot visible at the second ask, and resulting **limit-price** cost between10 and15 USDC. Reserve the full cost plus1% through the same `can_reserve`/`send` path; the attempt independently refuses any reservation above16 USDC. There is one attempt, no market scan, retry or replenishment.

Capture the existing full before/after balances, fees, ledger, fills, and all cloid/discovered-oid statuses. Additionally query `userFills` with `aggregateByTime:false` and `aggregateByTime:true` explicitly after the attempt, retaining both raw responses. Associate individual fills only with this attempt's cloid or newly discovered oids, then record positive fill count, distinct observed execution prices, `actual_multi_fill` and `actual_multi_price`. Multiple fills at one price are **not** classified as multiple prices. A moving book can produce rejection, no fill, one fill, or fills at prices differing from the original two levels. Captured intent is never proof of multilevel execution; endpoint-limited histories are not completeness guarantees.

The minimum-notional reference method remains uncertain: late PURR probes rejected size2 at limits above8 despite limit cost above16. A10–15 USDC limit cost here is a spending/preselection bound, **not** proof that admission's possibly reference-capped minimum passes. Inspect raw exchange/status results before drawing a method conclusion.

## Reading completion

Every attempted action has a pre-snapshot, a signed envelope, a reservation event, exact HTTP evidence, and attempted post-state/status observations. Snapshots use the existing balances, role, pre-transfer, ledger, fills, and fees queries. All files remain in the immutable recorder root; manifest events append without replacing prior evidence.

`workflow_completed` means the selected workflows ended, **not** that every requested financial outcome occurred. Check `precondition_unavailable`, exchange responses, and the actual fill/order-status captures. Failures append `workflow_aborted` with the retained cumulative reservation; failed post-observation is separately recorded. Inspect ambiguous outcomes using read-only queries before considering any new action. No finite successful campaign establishes universal parity.

This change adds campaign code and operator documentation only. Execution, tests, formatting, builds and linting are deliberately left to the parent/integration workflow.
