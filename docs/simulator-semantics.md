# Spot simulator semantics

This is a deterministic in-memory spot simulator, not a HyperCore node or a claim of mainnet parity. It makes no upstream requests. Metadata is the bundled capture for the selected network, or a file supplied through `--meta`, and is returned unchanged. Reset preserves that initial metadata, removes accounts and books, and restarts deterministic order/trade IDs at 1.

## Evidence and boundaries

Evidence used by the engine:

- [`testnet-baseline.md`](testnet-baseline.md) and its linked raw captures: account balances, complete fee schedule, spot send ledger shape and identical positive transfer entries on both sides.
- [`readonly-contract`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/readonly-contract/): fresh/missing accounts, malformed info requests, unknown orders, books, aggregation arguments and ledger ranges. A fresh account's `preTransferCheck.fee` is `"0.0"`, not the activation fee. The first fresh-account pre-transfer query returned transient HTTP 500; the matrix subsequently returned the ordinary response, so the engine does not hard-code that transient failure.
- [`send-contract`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/send-contract/): signed fresh/existing recipient sends, duplicate nonce, unknown recovered signer, invalid token/DEX and insufficient balance. Fresh send of 2 USDC changed sender 500 → 497 and recipient 0 → 2, establishing the sender-paid 1 USDC activation fee. Successful signed sending set `userHasSentTx:true`.
- [`ioc-contract`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/ioc-contract/): full PURR buy, no-fill, minimum-notional and size-precision failures with cloid lookup and post-balances/fills. Buying 4 PURR at 4.6252 cost 18.5008 USDC, credited 3.9972 PURR, charged 0.0028 PURR and recorded entry notional 18.5008.
- [`send-boundaries`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/send-boundaries/): invalid/zero/negative transfer amounts and nonce-too-low/high error templates. Cases labelled “inside” in this first run also returned outside-window errors; their labels do not establish accepted boundary behavior.
- [`failed-nonce-replay`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/failed-nonce-replay/): replaying the previously failed bad-token send produced a duplicate-nonce error, confirming nonce consumption for that business rejection.
- [`activation-refusal`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-refusal/): non-USDC activation refusal, rejected-action replay, zero-fee ledger `feeToken:""`, and non-USDC transfer basis/valuation.
- [`ioc-sell`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/ioc-sell/): sell fee in USDC, net proceeds, remaining entry notional, and `closedPnl` excluding the separately reported fee.
- [`reordered-order`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/reordered-order/), [`padded-order`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/padded-order/) and [`uppercase-cloid`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/uppercase-cloid/): equivalent order wire forms recover the original signer and return duplicate nonce.
- [Official info API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint), [spot API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot), [exchange API](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint), [precision rules](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size), and [nonce rules](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/nonces-and-api-wallets).
- Later evidence supersedes the early unknowns: [`orders-rounding-partial`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/orders-rounding-partial/), [`orders-cloid-batch`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/orders-cloid-batch/), [`matching-contract`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/matching-contract/), [`rounding-canonical`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/rounding-canonical/), [`quote-activation`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/quote-activation/), [`accounts-nonce-floor`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/accounts-nonce-floor/), [`refinements-signed`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/refinements-signed/), and [`late-contract`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/late-contract/).
- Dust allocation evidence: [`dust-lifecycle-01`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-lifecycle-01/), [`dust-followup-02`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-followup-02/), and [`dust-split-asymmetric`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/dust-split-asymmetric/). Protocol evidence: [`protocol-parity`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/protocol-parity/) and [`refinements-unsigned`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/refinements-unsigned/).
- Primary policy references: [fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees), [HIP-1](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-1-native-token-standard), and [SDK signing](https://github.com/hyperliquid-dex/hyperliquid-python-sdk/blob/master/hyperliquid/utils/signing.py). Documentation is not a substitute for testnet measurements.

The `Simulator:` error prefix identifies simulator-specific engine limitations. Principal accounting uses exact base-10 `rust_decimal` values (96-bit coefficient, at most 28 decimal places). Checked arithmetic rejects unrepresentable principal changes atomically rather than silently rescaling them. Division is representation-limited; basis and fee quantization are explicit below. Fees intentionally include a binary64 quote-unit calculation; the separate optional f64 balance-formatting quirk changes display only.

Perpetuals, builders, grouped TP/SL orders, reduce-only orders, agents, vaults/subaccounts, and numeric info-type dispatch are outside this simulator. Testnet accepted numeric `type:42` as a lending query; this simulator returns 422 for numeric dispatch rather than implementing that unrelated endpoint. Resting (`Gtc`/`Alo`) orders, cancels, modifies, stop/take-profit triggers and scheduled cancels are implemented from the documented API and a recovered matching loop, **not from testnet measurements**; see [Resting orders](#resting-orders-cancels-and-triggers).

## Runtime ordering and overload

A dedicated OS thread owns the engine, clock and fault queue. Every `/info`, `/exchange` and `/_test/*` state operation enters the same bounded FIFO mailbox and returns through a per-request reply channel. FIFO is successful enqueue order, not TCP arrival or client-send order. Body parsing, wire-shape validation and public-key recovery run on the HTTP worker before enqueue; the recovered signer (or its failure) is sequenced inside the actor after injected faults, and clock advancement, nonce admission and state mutations remain serialized there. Reset and other controls obey the same ordering as orders and queries.

`--max-inflight N` sets a positive admission limit, default1024. Admission is nonblocking and occurs before request-body buffering. Its permit follows body processing, queued/executing work and reply handling, including injected delays. Saturation returns HTTP503 with `Simulator request limit reached`, without advancing the clock, consuming a fault or nonce, or changing engine state. This is simulator policy, not an observed Hyperliquid overload contract. It bounds admitted request work, not open TCP connections, response socket buffers or accumulated account/history state.

Enqueued work is not cancelled when its client disconnects or drops the reply receiver. JSON response encoding and injected delay/drop handling run outside the state thread; another admitted query can observe a committed order before its delayed acknowledgment arrives. Overload rejected before admission can be retried after capacity returns. Transport failures and HTTP503 `Simulator actor unavailable` do not establish whether an earlier submission committed: reconcile status/nonces rather than blindly retrying.

Ctrl-C stops accepting new connections, drains active HTTP handlers and queued commands, then joins the state thread. State remains in memory only; neither shutdown nor cancellation adds durability.

## Info requests

Send JSON objects to `POST /info`; captured positional array forms are also supported (for example `["userRole", "0x…"]`). Addresses accept 20-byte hex with or without lowercase `0x` and normalize to lowercase. Unknown info fields are ignored. Duplicate known fields are rejected contextually for the selected query; duplicate unknown fields and ignored nested extensions are not blanket errors. Exchange envelopes/actions reject unknown fields, while captured unknown order/signature extensions are ignored. Trailing bytes after the first complete JSON value are accepted. Exchange root arrays are rejected.

| `type` | Fields | Behavior |
| --- | --- | --- |
| `spotMeta` | none | Supplied metadata, unchanged. |
| `spotClearinghouseState` | `user` | `balances` ordered by token index, with `coin`, `token`, string `total`, `hold`, `entryNtl`. Never-held tokens are omitted. Immediate zero rows remain; automatic dust conversion removes eligible sub-lot rows. Unknown account returns `[]`. `hold` is the amount locked by the account's resting orders (quote notional for bids, base size for asks); untriggered stop/take-profit orders lock nothing. |
| `preTransferCheck` | `user` | `isSanctioned:false`, `userExists`, `fee:"0.0"`, `userHasSentTx`. The positional form `["preTransferCheck",user,sourceOrNull]` reports fee `"1.0"` for a missing recipient with a supplied source. Guessed object source fields are ignored. Sanctions are not modeled. |
| `l2Book` | `coin`, optional `nSigFigs`, `mantissa` | Exact metadata pair name (`PURR/USDC` or an `@index` name); unknown coin returns `null`. At most 20 levels per side, descending bids/ascending asks, each with `px`, `sz`, `n`; `time` is the simulator clock. Aggregated responses (`nSigFigs` set) also carry `spread`, the raw best-ask minus best-bid (measured in `readonly-contract`, all six captures `0.0457`); its value with an empty side is unmeasured, so the simulator omits it then. |
| `orderStatus` | `user`, `oid` | `oid` is uint64 or a 128-bit `0x` cloid. Returns documented nested `order` wrapper, or `{"status":"unknownOid"}`. `sz` is the remaining size; `status` follows the exchange's names (`open`, `filled`, `canceled`, `triggered`, `selfTradeCanceled`, `scheduledCancel`, `badAloPxRejected`, `badTriggerPxRejected`, `marketOrderNoLiquidityRejected`, and the measured IOC rejections). |
| `openOrders` | `user` | Open resting and untriggered trigger orders by oid: `coin`, `limitPx`, `oid`, `side`, `sz`, `timestamp`, `cloid` when set. Unmeasured shape from the SDK. |
| `frontendOpenOrders` | `user` | The same orders with the full order detail (`orderType`, `tif`, `isTrigger`, `triggerPx`, `triggerCondition`, `origSz`, ...). Unmeasured. |
| `historicalOrders` | `user` | Up to 2000 order records newest first, each `{order, status, statusTimestamp}`. Unmeasured. |
| `userFills` | `user`, optional boolean `aggregateByTime` | Up to 2000 most recent fills or aggregate groups, newest first; aggregation precedes the cap. Aggregation combines consecutive fills of the same order and timestamp, summing size/fee/PnL and rounding weighted price to ten fractional places. `aggregateByTime:true` excludes system dust fills; the raw feed retains them. Exact halfway rounding remains unmeasured; the implementation uses ties-to-even. |
| `userFillsByTime` | `user`, `startTime`, optional `endTime` (null or absent means now), optional boolean `aggregateByTime` | Fills with `startTime <= time <= endTime`, oldest first; both bounds inclusive and the ordering are measured in `nq-prior-history`, `fill-limit-history` and `independent-market-history`. Aggregation follows `userFills`. At most 2000 entries; which end is kept when more match is unmeasured, and the simulator keeps the most recent. |
| `userNonFundingLedgerUpdates` | `user`, optional `startTime`, `endTime` | Inclusive millisecond range, defaults to 0/current clock; up to 500 matching entries, oldest first. |
| `userFees` | `user` | External fee schedule/discounts and global exchange volume, with engine-computed per-user daily volume and rolling tier selection. See fees below; the baseline undiscounted spot taker rate is `0.0007`. |
| `userRole` | `user` | `{"role":"user"}` for an existing account, otherwise `{"role":"missing"}`. |

Malformed argument shapes and unknown string query types return HTTP 422 with exact body `Failed to deserialize the JSON body into the target type`. The HTTP layer separately handles invalid JSON/content type. Numeric book aggregation values outside the captured supported set return HTTP 500 with body `null`: `nSigFigs` permits 2–5 or null; an explicit `mantissa` permits 2 or 5 only with `nSigFigs:5`. The official documentation also lists mantissa 1, but captured testnet rejects it, so capture evidence wins. Wrong JSON types still return 422.

Book aggregation rounds bids down and asks up to the requested significant-figure bucket, sums sizes and order counts, then limits each side to 20 levels. Mantissa multiplies the bucket width. **Assumption:** bucket rounding, synthetic depth and its `n` accounting are deterministic local market mechanics, not a reconstruction of individual live resting orders.

## Signed exchange processing

The HTTP/crypto layers authenticate envelopes before calling the engine. The engine rejects a recovered signer absent from its account map. A funded account can have `userHasSentTx:false`; funding alone does not mark signed processing.

Order recovery reconstructs typed SDK field order, normalizes decimal padding and cloid case, then MessagePack-hashes the action with nonce/vault/expiry and recovers the phantom Agent signature. `sendAsset` uses its distinct user-signed EIP-712 fields. The HTTP layer rejects malformed fixed-eight-decimal unsigned `p`/`s` strings before nonce admission. JSON object order alone is not an authenticated difference.

All seen nonces still inside the valid time window are retained for duplicate detection, not just 100 values. A new nonce must exceed the 100th-largest retained nonce once that floor exists; a seen value below the floor still returns duplicate nonce. [`accounts-nonce-floor`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/accounts-nonce-floor/) and [`refinements-signed`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/refinements-signed/) establish this distinction. Two earlier admitted high nonces from `accounts-dust-admission` exceed all 105 later backdated seed nonces; replay must include them to recover the observed floor.

Official documentation describes `(T - 2 days, T + 1 day)`. Captured rejection bounds imply an approximately 120-second inward margin at both ends. The engine uses strict bounds `(T - 2 days + 120 seconds, T + 1 day - 120 seconds)`; exact live endpoint inclusivity remains unmeasured.

Admission precedes action-level validation: an admitted rejection consumes its nonce and the first such rejection sets `userHasSentTx:true` (measured in `accounts-dust-admission`). Unknown signers and failed nonce admission do not set it. Transfer action/envelope nonce mismatch is checked before recovery; vault rejection precedes admission, and any `sendAsset` expiry is unsupported before admission. An expired order is rejected after admission. Typed canonical order recovery includes vault/expiry in its signature; `sendAsset` authenticates its distinct action fields, not wrapper vault/expiry.

### IOC orders

`action.type:"order"` supports `grouping:"na"` (also the omitted default). Each order uses `a:10000+pairIndex`, boolean `b`, decimal strings `p`/`s`, `r:false` (also the omitted default), `t:{"limit":{"tif":"Ioc"}}`, and optional 128-bit `c` cloid. Asset resolution and static validation cover the whole batch before any matching: a static failure prevents earlier otherwise-valid orders from executing. An empty batch returns the observed `Orders are empty.` status. Once static validation succeeds, execution results are returned per order.

- Price: at most 5 significant figures unless an integer; at most `8 - base.szDecimals` fractional digits. Size: at most `base.szDecimals` fractional digits. Trailing zeroes do not count against normalized precision.
- Reference band: `0.2 × reference < limit ≤ 5 × reference`. The lower boundary is excluded and upper boundary included, measured on NQ at `1.0211` and TEST8 at exactly `1.0`, including one legal tick inside/outside each edge. Violations are static batch errors, with no order record.
- Positive size/price are required. Runtime precedence is no crossing liquidity, zero paying-token balance, minimum notional, then affordable whole lots. The minimum is **10 quote units**, using full requested size × first crossing execution price, not limit/reference/affordable size. NQ/USDC distinguishes `9.76 × 1.024 < 10` from `9.77 × 1.024 > 10`. LMA/USDEEE distinguishes9.79 quote units (minimum rejection) from10.45 (insufficient balance), despite the latter being worth only9.405 USDC at the independent0.9 quote mark. The error names the quote token.
- Affordability uses actual execution prices, not the full requested limit-price notional. Available source balance can fund a lot-aligned partial IOC execution; unsatisfied quantity does not rest.
- Eligible opposing levels execute in price priority until quantity, available balance, or eligible depth is exhausted. Full and positive partial fills terminate as `filled`; no crossing liquidity produces `iocCancelRejected`, while an unaffordable first lot produces `insufficientSpotBalanceRejected`. Runtime rejections retain queryable terminal records; static failures do not.
- Fees are paid in the received token: base on buys, quote on sells. Per execution, convert exact gross notional to quote wei, convert that value to binary64, multiply by the binary64 effective taker rate, and floor to integer quote wei. A sell pays that quote fee; a buy divides it by execution price and truncates to base `weiDecimals`. This measured quote-first quantization is not equivalent to directly truncating `size × rate`. Principal debit excludes the received-token fee.
- Fill records include spot `coin`, `side`, `dir` (`Buy`/`Sell`), `startPosition`, `closedPnl`, `crossed:true`, `px`, `sz`, `fee`, `feeToken`, `time`, `oid`, `tid`, `twapId:null`, optional `cloid`, and deterministic simulator `hash`. Filled action responses also include `cloid` when supplied. These hashes/IDs are not actual chain identifiers.
- `filled.avgPx` truncates the weighted execution price to `8 - base.szDecimals` fractional places. Two live TEST11 multi-level fills establish six-place truncation; generalization to other size precisions is inferred. The same fills establish ten-place rounding in aggregated `userFills`, including `7.711969369369… → 7.7119693694`.
- `entryNtl` is USD-denominated on both assets. Buys add gross quote spend converted at the quote's USDC reference to base basis; spending a non-USDC quote proportionally reduces that quote's basis. Sells proportionally reduce base basis and add gross USD proceeds to a received non-USDC quote's basis, before received-token fees. Incoming transfer valuation, sold USD basis and remaining USD basis truncate to eight decimals. Sell `closedPnl` is in quote units: convert the truncated sold USD basis back through the current quote/USDC reference, truncate to quote precision, and subtract it from gross quote proceeds. LMA/USDEEE directly distinguishes these operations. A positive quote-to-USDC reference is required before a non-USDC fill; missing context returns a `Simulator:` error without inventing an FX rate.
- Cloids are case-normalized and reusable after terminal orders. A reused cloid resolves to its latest order; earlier orders remain queryable by oid. Lifetime cloid uniqueness is not enforced.
- Local terminal state is immediately visible. One native cloid lookup returned `unknownOid` after a filled acknowledgment, then resolved as filled in a later read-only recovery. Its server-side lookup/index visibility time is not captured; the simulator does not invent a delay or override the recorded response. This remains an explicit replay discrepancy.

Captured errors include `Order has invalid size.`, `Order must have minimum value of 10 {quote}. asset={asset}`, and `Order could not immediately match against any resting orders. asset={asset}`. A captured price with nine decimal places failed HTTP deserialization (422), before the order engine; the HTTP layer owns that structural boundary. Five-significant-figure price validation otherwise follows the documented rule.

Balances, fills, book depth, order IDs and terminal records commit atomically after each order's arithmetic succeeds. Observed minimum-notional/no-fill business rejections intentionally retain terminal records without changing balances. After whole-batch static validation, an execution-stage failure in a later order does not roll back earlier successful orders. Nonce admission is a separate transition.

### Resting orders, cancels and triggers

**None of this section was probed on testnet.** It follows the public API documentation, the SDK's wire forms, and the matching loop recovered from the node binary (`can1357/hl`, `execute_book_order`). Every rule below is simulator policy until a signed testnet campaign measures it.

- **Matching.** Each market is a queue per side ordered by price, then arrival. A taker consumes the opposite side while the next entry crosses its limit; synthetic depth from `/_test/book` and users' orders share the queue. `Ioc` follows the measured section above. `Gtc` rests any remainder; `Alo` rests only when it would not cross, otherwise `badAloPxRejected` with `Post only order would have immediately matched, bbo was {bid} @ {ask}. asset={asset}`. A `Gtc`/`Alo` order needs its full notional (bids) or size (asks) available before it is accepted (`insufficientSpotBalanceRejected`) and at least 10 quote of notional at its limit (`minTradeNtlRejected`); IOC keeps its measured partial-funding behavior.
- **Holds.** A resting bid locks `sz × px` of quote; a resting ask locks `sz` of base. Locked funds cannot be spent by later orders or transfers and are released on fill or cancel. `spotClearinghouseState.hold` reports them.
- **Maker fills.** The resting side of a match receives a fill with `crossed:false`, the same `tid` and `hash` as the taker's fill, and a fee at `spotAdd` after the staking discount (referral discounts apply to takers only), quantized like taker fees and charged in the received token. Maker notional counts toward `userAdd` daily volume with the taker weighting rule, and both taker and maker volume feed the VIP tier.
- **Self-trade prevention.** When a taker meets its own resting order the resting order is canceled (`selfTradeCanceled`), its hold released, and matching continues with the next entry.
- **Cancels.** `cancel` takes `{a, o}` pairs and `cancelByCloid` `{asset, cloid}` pairs; each reports `"success"` or `{"error":"Order {id}: Order was never placed, already canceled, or filled."}`. The asset must name the order's market.
- **Modify.** `modify` and `batchModify` look up the open order by oid (or cloid), require the same market and side, cancel it, and place the replacement through the normal path under a new oid. `modify` answers `{"type":"default"}`; `batchModify` answers order statuses. A missing order answers `Cannot modify canceled or filled order.`
- **Triggers.** A `trigger` order rests off-book without a hold. A sell take-profit or buy stop fires when the reference price (mark, else mid) rises to `triggerPx`; the other two fire when it falls to it. A trigger already satisfied at placement is `badTriggerPxRejected` (`Invalid TP/SL price. asset={asset}`). Firing marks the order `triggered`, then executes it under the same oid as an IOC at its limit when `isMarket` (nothing filled: `marketOrderNoLiquidityRejected`) or as a `Gtc` otherwise. `orderType` is `Stop Market`, `Stop Limit`, `Take Profit Market` or `Take Profit Limit`; `triggerCondition` is `Price above {px}` or `Price below {px}`.
- **Scheduled cancel.** `scheduleCancel` arms a dead-man switch for accounts with at least 1,000,000 USDC of lifetime traded notional, at least five seconds ahead, at most ten firings per UTC day; a missing `time` clears it. When the clock passes the deadline every open order is canceled with status `scheduledCancel`.
- **Dust.** Sub-lot balances that are locked by a resting order are not swept. A sweep sells into users' resting bids like any taker, so those makers receive maker fills.

### `sendAsset`

Requires `sourceDex:"spot"`, `destinationDex:"spot"`, token identity, positive decimal-string `amount`, valid `destination`, matching action/envelope nonces, and `fromSubAccount:""`. Transfer amounts accept exact scientific notation; order `p`/`s` do not. Amount precision cannot exceed token `weiDecimals`. `NAME:tokenId` identifies a token; canonical bare `PURR` and `USDC` aliases are live-proven. General bare noncanonical names, indices and token IDs are not interchangeable signed aliases. Self-send is rejected with `Invalid send`, without a balance or ledger mutation.

An existing destination costs no activation fee. A fresh destination costs the sender one quote-token unit in addition to principal; the recipient receives the entire quantity. A registered quote-asset transfer must pay gas in that same asset: sending1 NQ with2 NQ and available USDC charged1 NQ; sending1 NQ with exactly1 NQ plus1 USDC rejected with `Insufficient NQ balance for token transfer gas.`, leaving the recipient missing. There is no alternate-token fallback for that quote transfer. Nonquote sends prefer available USDC, then other eligible quotes in index order: PURR selected USDC over NQ, and selected USDEEE(index1295) over NQ(index1424) without USDC, even though NQ was funded first. The earlier NQ-only activation also succeeded. These samples establish those comparisons, not every possible registry combination; metadata-hidden eligibility/alignment still requires explicit context.

Both parties receive identical positive `delta.type:"send"` entries. `feeToken` is the charged token when activation costs1 and `""` otherwise. Ledger `usdcValue` truncates valuation to six decimals, including USDC amounts; incoming non-USDC `entryNtl` truncates to eight. Remaining sender basis is proportional and truncated to eight decimals. A one-USDEEE-wei transfer at0.9 therefore credits zero entry basis, not0.000000009 USD. Valuation uses the USDC pair's latest configured mark or execution price, whichever was updated last; without either it falls back to a two-sided midpoint, otherwise zero. Independent historical feeds can be incomplete and never import tested financial outputs.

Captured action errors used verbatim: `Invalid nonce: duplicate nonce {nonce}`, `Invalid nonce: nonce too low {nonce} < {oldest}`, `Invalid nonce: nonce too high {nonce} > {newest}`, `Must deposit before performing actions. User: {signer}` for a nonexistent recovered `sendAsset` signer, `Insufficient balance for token transfer`, `Unknown token {token}`, `Invalid perp DEX`, `Invalid decimal number`, `Send amount cannot be zero`, and `Invalid number of decimals` for a negative amount. Dynamic values come from submitted/recovered data and the simulator clock.

### Fees and rolling volume

Stable quote-to-quote pairs receive fee weight0.2. Explicit **AQAv1** alignment multiplies fees by0.8 and qualifying volume by1.2; it is not the newer AQAv2 designation, which carries no such benefits. Quote registration/alignment is external context, not a symbol-name heuristic; `/_test/fees` supplies these sets. Stable weighting is measured; AQAv1 benefits follow the official policy and controlled fixtures, not a live alignment-boundary experiment.

USDC-quoted fills contribute `2 × trunc2(gross quote notional × volume weight)` to UTC-day taker volume, truncating per fill before doubling. Native LMA/USDEEE and KRWIN/USDYP contribute zero despite ordinary fees and USD cost-basis changes. USDC control markets with50%/100% deployer fee shares still contribute full volume, ruling out deployer share as the cause. The implementation qualifies USDC and explicitly configured AQAv1 quotes; undocumented eligibility of other quote regimes remains external context. `userFees` reports today plus14 preceding dates; tiers use14 completed days, not today's activity or donor-user history. Exact VIP cutoff equality remains an unmeasured strict-`>` assumption.

### Automatic spot dust conversion

Testnet captures show sub-lot balances converting at UTC minute boundaries, including individual balances worth more than $1. This differs from [HIP-1's documented mainnet policy](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-1-native-token-standard): midnight UTC and at most $1 per eligible balance. Mainnet has not been probed. Testnet's exact cap remains unmeasured; the default imposes no invented $1 cap and reports that provenance through `/_test/dust`.

Eligible positive balances smaller than one base lot are pooled by USDC market. With a two-sided book, eligible pooled whole lots sell into configured bids; a pool smaller than one lot is burned without a fill or USDC credit. Converted rows and their basis disappear. Aggregate safeguards follow HIP-1's notional caps (10,000 for canonical PURR, 3,000 otherwise); their testnet boundaries are not established by the owned samples.

The pool pays a system execution fee before distribution, but each user's raw fill reports `fee:"0.0"`, `closedPnl:"0.0"`, zero hash, `tid:0`, and `dir:"Spot Dust Conversion"`. Dust causes no user fee-volume or ledger entry, does not admit a nonce or set `userHasSentTx`, and its oid returns `unknownOid`. `aggregateByTime:true` omits these fills entirely. Net proceeds are allocated proportionally with quote-wei floors, then all residual wei go to the largest dust holder, breaking equal-size ties by lexicographically lowest normalized address. The three split roots above distinguish equal and asymmetric allocations.

The local clock advances lazily on requests or `/_test/time`. Crossing a boundary triggers a sweep; a jump across several idle intervals sweeps once using the first crossed boundary. Initial clock observation and rewinds establish a new anchor without undoing state. These clock/depth mechanics are controlled simulation, not a reconstruction of unobserved network-wide dust or atomic live liquidity.

## Test control schema

These are local simulator controls, not Hyperliquid API endpoints. Decimal values are strings.

### `POST /_test/fund`

```json
{"address":"0x1111111111111111111111111111111111111111","token":"USDC","amount":"500","mode":"transfer","serialize_f64":false}
```

- `token`: metadata token name, index as a string, tokenId, or `NAME:tokenId`.
- `mode:"transfer"`: recipient receives the full amount. Optional `sender` is an existing address that pays the principal and any activation fee atomically, using the same transfer mechanics and both ledgers. Without a sender, a synthetic external source funds the amount and covers activation; only the recipient's ledger exists, with zero-address source. Funding does not mark either account's `userHasSentTx` or consume a signed nonce.
- `mode:"deposit"`: USDC only; a new account receives amount minus 1 USDC, existing accounts receive the full amount. A fresh deposit must cover the 1 USDC fee; exactly 1 creates an account with a retained zero USDC balance. `sender` is invalid for deposits. The synthetic deposit ledger records net USDC; this is explicitly a test fixture, not a captured bridge receipt.
- Optional `serialize_f64` sets per-account binary-float-style total formatting; exact balances and arithmetic remain unchanged.
- External `mode:"transfer"` without `sender` additionally accepts additive nonnegative `entryNtl` and explicit boolean `userHasSentTx` fixture values. It permits amount `"0"` for zero-balance account setup. These seed captured preconditions without pretending funding performed a signed action.
- Response includes `ok`, `address`, `activationFee`, and current `state`.

### `POST /_test/book`

```json
{"coin":"@1","bids":[{"px":"10.0","sz":"2.0","n":1}],"asks":[{"px":"11.0","sz":"3.0","n":2}]}
```

Both arrays are required and may be empty. Prices/sizes must be positive, sizes obey base-token precision, and `n` defaults to 1 and must be a positive integer. Input is sorted; equal-price levels aggregate in info responses. Crossed books are rejected atomically. These are external liquidity fixtures; counterparties are not account balances.

Alternatively:

```json
{"coin":"@1","mid":"10.0","depth":"5.0"}
```

This replaces both sides with up to 20 symmetric levels. `depth` is size **per level**, not level count. Level spacing is max(mid × 0.001, minimum decimal tick); prices are truncated to `8 - szDecimals` fractional places, and nonpositive bids are omitted. Explicit arrays and midpoint form cannot be mixed. Response is the resulting full-precision L2 snapshot.

Optional positive `markPx` updates the current reference independently of book midpoint. `{"coin":"@1","markPx":"10.0"}` preserves depth and can also accompany a depth update. Subsequent local executions advance this mark; a later explicit mark replaces it. A depth-only update does not overwrite an existing mark. A mark alone establishes no liquidity for dust or matching.

### `POST /_test/account`

```json
{"address":"0x1111111111111111111111111111111111111111"}
```

Returns existence, normalized address, `userHasSentTx`, serialization mode, ordinary balance `state`, admitted `nonces`, fills, ledger, order map, and cloid map. Missing accounts return only address and `exists:false`.

### `POST /_test/reset`

An empty object resets accounts, nonces, orders, fills, ledgers, books, marks, sequence counters, dust overrides and fee/quote overrides, retaining initial metadata and restoring the baseline fee fixture. Reset also disables the upgrade post-only gate, clears faults and restores wall-clock time.

### `POST /_test/time`

`{"now_ms":1800000000000}` fixes the clock for deterministic timestamps and nonce tests. `{"now_ms":null}` restores wall-clock time. Reset also restores wall-clock time.

### `POST /_test/fault`

Faults queue for subsequent successfully enqueued exchange commands. Overload rejections do not consume them:

| Body | Observable behavior |
| --- | --- |
| `{"kind":"refuse","message":"reason"}` | HTTP200 in-band error, no admission or mutation. |
| `{"kind":"rate_limit"}` | HTTP429, JSON `null`, no admission or mutation, matching the captured rate limit. |
| `{"kind":"delay","delay_ms":150}` | Process the action, then delay its response; maximum60000ms. Other requests can observe committed state. |
| `{"kind":"drop_after_commit"}` | Process the action, then fail the response stream. Curl observes a transport error; ledger/status queries resolve the recorded action and replay is refused. |

Refusal defaults to `Injected action refusal`; a supplied `message` changes refusal/rate-limit body. Delay defaults to1000ms. Reset clears the queue. These controls are simulator-only and unauthenticated; the executable binds wherever `--bind` points, so a non-loopback bind belongs only on a trusted network.

### `POST /_test/upgrade`

`{}` reads `{"postOnly":false}` by default. `{"postOnly":true}` enables the persistent network-upgrade post-only gate; `{"postOnly":false}` disables it. Both setters return the current `postOnly` value. A supplied non-boolean (including `null`) or non-object body returns HTTP400 without changing the gate. Reset disables it.

While enabled, otherwise statically valid IOC orders return the native order-status error `Only post-only orders allowed immediately after network upgrade`, after nonce admission but before OID allocation or matching. Replaying the admitted envelope therefore returns duplicate nonce. No order, fill, balance, cloid or book mutation results from the gated IOC; admission still marks `userHasSentTx`. `sendAsset` is unaffected. Existing static validation errors take precedence in this simulator; combined-invalid/gate precedence has not been live measured.

This is externally supplied network context, not a queued fault: it persists across submissions until disabled or reset. Pre-admission `refuse`/`rate_limit` and post-processing `delay`/`drop_after_commit` retain their existing behavior. Use a fixed `/_test/time` when comparing account/book snapshots so automatic clock-driven dust processing is not confused with gated-order effects.

The two original minimum-funded IOC envelopes returned this exact error, and their later replays in `captures/testnet/upgrade-replay` returned duplicate nonce, establishing admission before rejection. Historical upgrade timing was not independently captured. Do not infer an enable window from historical order errors or toggle this control to force old golden cases to pass.

### `POST /_test/dust`

`{"intervalMs":60000,"maxNotional":null}` overrides the positive sweep interval and per-account eligibility cap. A nonnegative decimal-string cap constrains eligible notional; `null` explicitly removes it. `{}` reports current `intervalMs`, `maxNotional` and `maxNotionalSource` (`override`, `hip1`, or `testnet-unmeasured`). This configures automatic processing; it is not a signed manual sweep.

### `POST /_test/fees`

Accepts any combination of `fees` (a complete captured `userFees` object), `quoteTokenIndices` and `alignedQuoteTokenIndices` (arrays of registered metadata token indices); at least one is required. Each supplied set replaces its previous override. The fee object supplies schedule, discounts and dated global exchange volume, never donor `userCross`/`userAdd`. Existing engine-computed account volume remains intact. Without overrides, quote eligibility is inferred from pair quote tokens plus USDC, and the aligned set is empty.


## Unverified compatibility

No finite campaign proves universal parity. Residual gaps include native cloid index visibility at the server lookup instant, historical execution-time market/depth context, individual makers hidden by aggregate L2, isolated prior dust-ID prerequisites, exact nonce-window endpoints, testnet dust-cap/safeguard boundaries, metadata-hidden quote/alignment/volume eligibility, VIP cutoff equality, aggregate-price halfway rounding, average-price precision beyond measured size precisions, historical upgrade timing and live mainnet behavior. Measured quote accounting, activation-gas comparisons, minimum precedence, reference boundaries, partial fills, nonce retention, cloid reuse and dust conversion are implemented; they are not a universal compatibility claim. See [verification](verification.md).
