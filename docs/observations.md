# Measured testnet observations

These are the recorded observations from this account experiment, not assumptions supplied by the initial design. Every referenced directory contains exact request/response artifacts. The original funded-account snapshot remains in [testnet-baseline.md](testnet-baseline.md).

## Capture index

Paths below are under [`captures/testnet/`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/). The offline audit regenerates [`captures/index.json`](../captures/index.json) with counts, status distributions, unresolved pending requests and integrity errors.

| Run | Purpose / outcome |
| --- | --- |
| `20260918T045515.026265Z-baseline` | Seven initial read-only captures; A had500 USDC and no sent transaction. Predates the reusable manifest format. |
| `20260918T050235.485816Z-contract` | Initial malformed bodies, fresh B, zero signatures and JSON framing probes. |
| `readonly-contract` | All eight required info queries plus userRole; valid/malformed/fresh inputs, order lookup and book aggregation. |
| `send-contract` | Fresh/existing transfers, nonce replay, tampering, different key, balance/token/DEX errors. Halted on a captured429 during a post-state query; not silently retried. |
| `ioc-contract` | Full buy, no fill, minimum notional, invalid size, and HTTP-level invalid price precision. |
| `send-boundaries` | Amount errors and nonce-window rejection messages. |
| `unsigned-shapes` | Order p/s wire parser boundaries and content-type/JSON framing. Zero signatures cannot authorize an action. |
| `reordered-order`, `padded-order`, `uppercase-cloid` | Equivalent signed order representations all recovered the original signer and returned duplicate nonce. |
| `failed-nonce-replay` | Previously rejected bad-token action had consumed its nonce. |
| `nonce-interior` | Five-minute-interior samples on both sides of the documented time window were accepted. |
| `activation-refusal` | Non-USDC activation failure, rejection replay, transfer valuation/basis, and return of probe funds. |
| `ioc-sell` | Full sell, USDC fee, proceeds, fills and cost-basis output. |
| `partial-fill` | Scanned30 books within a25-USDC cap. No suitable thin book; **no exchange action submitted**. |
| `protocol-parity`, `refinements-unsigned`, `info-edge-contract` | Contextual duplicate-field handling, ignored extensions, array forms, addresses without `0x`, ledger bounds and exact HTTP responses. |
| `accounts-dust-admission`, `accounts-nonce-floor`, `refinements-signed` | First admitted rejection sets sent flag; duplicate history survives beyond the 100th-largest admission floor. |
| `accounts-transfer`, `refinements-aliases`, `late-contract`, `transfer-valuation` | Self-send rejection, exact scientific amounts, canonical aliases, six-decimal ledger valuation and independent mark/basis observations. |
| `quote-activation` | Live fresh-recipient activation paid in NQ; alternate payment is no longer merely a local assumption. |
| `orders-cloid-batch`, `orders-edge-contract`, `omitted-order-defaults` | Terminal cloid reuse, whole-batch static validation, typed defaults, vault/expiry precedence. |
| `orders-rounding-partial`, `matching-contract`, `rounding-canonical` | Positive partial IOC status `filled`, actual-price affordability, quote-first fee quantization, stable-pair weighting and fractional volume. |
| `dust-lifecycle-01`, `dust-followup-02`, `dust-split-asymmetric` | Automatic minute-boundary dust fills and three split-allocation roots, including equal and asymmetric holders. The first lifecycle's classification abort is retained, not rewritten as a completed workflow. |
| `minimum-contract`, `minimum-nq`, `minimum-funded-02` | No-cross/zero-balance/minimum precedence; one-wei funding distinguishes first execution price from limit and mark. |
| `band-nq`, `band-test8-03`, `band-inside` | Strict lower `0.2 × mark` and inclusive upper `5 × mark`, including exact `mark=1` and adjacent legal prices. |
| `orders-multilevel-02`, `orders-multilevel-04` | Two-level fills distinguish six-place order-average truncation from ten-place aggregate-price rounding. |
| `independent-market-history`, `independent-market-history-wide` | Public maker/aggressor history independently explains the transfer mark transition; maker-specific history is not a complete tape. |
| `minimum-funded`, `upgrade-replay` | Two upgrade-gated IOC rejections consumed their nonces; later raw replays returned duplicate nonce. |
| `fill-limit-readonly`, `fill-limit-history` | Raw and aggregated feeds each return 2000 rows; aggregation precedes limiting. Independent older fill history reconstructs all 2000 aggregate groups exactly from 2268 individual fills. |

These paths are immutable evidence roots, not guarantees that every named scenario completed or had sufficient context. `band-test8` stopped on a one-sided-book precondition; `band-test8-02` stopped on a recorded info HTTP500 before submitting an action. `band-test8-03` completed after the band-only probe explicitly allowed ask-only liquidity under its zero-funds guard.

## HTTP and parsing

- Invalid JSON syntax: HTTP400, plain text `Failed to parse the request body as JSON`.
- Wrong/missing schema fields or unknown string query type: HTTP422, plain text `Failed to deserialize the JSON body into the target type`.
- These parsing failures apply to `/exchange` too. “All exchange errors are HTTP200” is false.
- Wrong content type: HTTP415, plain text `Expected request with \`Content-Type: application/json\``. `application/*+json` was accepted.
- One valid JSON object followed by garbage or a second object was accepted; the server consumed the first value.
- Structurally valid zero signatures: HTTP200, `{"status":"err","response":"Unable to recover signer."}`.
- A real rate limit returned HTTP429 with JSON `null`. Its raw headers/trace remain in `send-contract/send-bad-destination-dex-after-sender-role`.
- Numeric info type42 selected a lending response rather than failing deserialization. Lending/numeric dispatch is outside the selected simulator scope.
- Duplicate fields are schema-contextual: known fields of the selected query/action reject with422 even if equal; unknown info/order fields and ignored nested extensions are not rejected merely for duplicates. Unknown exchange envelope/action fields still reject. See `protocol-parity` and `refinements-unsigned`.
- Captured positional `/info` arrays are accepted with query-specific lengths; `/exchange` arrays reject. Twenty-byte user addresses without `0x` are accepted. Uppercase hexadecimal digits normalize, while uppercase `0X` is not the accepted prefix.
- `preTransferCheck`'s array form supplies an optional source address: a missing recipient with a source reports fee`"1.0"`, while ordinary object requests report`"0.0"`. Guessed object source fields are ignored (`pretransfer-fields`, `pretransfer-fields-v2`).

## Account existence and activation

- A funded account can be role`user` with `userHasSentTx:false`.
- Fresh B returned role`missing` and no balances. Its first `preTransferCheck` returned500/null; a later matrix call returned `userExists:false`, `userHasSentTx:false`, `isSanctioned:false`, **`fee:"0.0"`**. The transient500 is retained but not hard-coded as fresh-account behavior.
- Sending2 USDC A→fresh B debited3 USDC from A and credited2 USDC to B. Subsequent sends to B cost no activation fee.
- A PURR send from an existing account with PURR but no USDC to fresh C returned exactly:

```text
Insufficient quote token (e.g., 1 USDC or USDT) balance for token transfer gas.
```

- C stayed missing, neither principal nor activation was charged, and replay returned duplicate nonce.
- Later `quote-activation` paid one NQ to activate a fresh recipient while delivering full principal. Registration as a quote token, not a USDC/USDT name list, controls eligibility. `spotMeta` does not expose every quote/alignment flag; replay must supply that context explicitly. The relative priority of several simultaneously affordable quote assets is not established.

## Signatures and nonces

- A valid signature from B authorizes B, not A. The “wrong-key” probe succeeded as a B→A transfer.
- Tampering with a signed transfer amount recovered a different, unfunded address and returned `Must deposit before performing actions. User: {recoveredAddress}`.
- Exact replay: `Invalid nonce: duplicate nonce {nonce}`.
- Bad-token business rejection also consumed its nonce, as did the activation refusal.
- Nonce too-low/high errors report both submitted nonce and comparison bound. Comparing those bounds with capture timestamps implies an approximately120-second inward admission margin relative to the documented -2days/+1day window. One-second-“inside” probes were rejected; five-minute-interior probes succeeded. Exact endpoint inclusivity is not proven by wall-clock probes.
- Recursive JSON key reordering, nine-decimal zero padding, and uppercase cloid hex did **not** change the recovered order signer. The server canonicalizes typed wire representation before MessagePack hashing; hashing the raw parsed JSON insertion order is wrong.
- `accounts-dust-admission` measured `userHasSentTx:true` after the first admitted business rejection; balance funding alone does not set it.
- `accounts-nonce-floor` and `refinements-signed` distinguish replay memory from admission: all valid-window seen values still return duplicate, including values below the 100th-largest admission floor; a new value below that floor returns too-low. Two admitted high nonces from the earlier dust-admission run precede and exceed all105 backdated seed nonces, producing the captured floor`1789709474349`, not the seed-only floor`1789709471290`.
- `orders-edge-contract` and `late-contract` establish vault/expiry distinctions: vault errors and unsupported transfer expiry precede admission; an expired order consumes its admitted nonce. Transfer action/envelope mismatch is checked before recovery. These finite precedence cases are not an exhaustive permutation proof.

## Transfers and ledger

- Both parties receive the same positive `delta.type:"send"` entry, with user/destination, spot dex names, plain token name, amount, USDC value, fee, nativeTokenFee, nonce, and feeToken.
- `feeToken` is`"USDC"` when activation charged USDC and **`""` when fee is zero**.
- Unknown token: `Unknown token {submittedToken}`.
- Bad source/destination dex: `Invalid perp DEX` even for this spot action.
- Insufficient principal: `Insufficient balance for token transfer`.
- Bad decimal text: `Invalid decimal number`; zero:`Send amount cannot be zero`; negative:`Invalid number of decimals`.
- Self-send rejects with `Invalid send`, rather than netting principal. Transfers accept exact scientific notation within token precision; order price/size wire strings do not.
- `refinements-aliases` proves bare canonical `PURR` and `USDC`, not arbitrary bare token names or token IDs.
- The original PURR value was4.6252 USDC per token; later `transfer-valuation` records independent mark context. Ledger `usdcValue` truncates to six decimals, while recipient basis uses the full valuation and sender remaining basis is proportional with eight-decimal truncation. Ledger display precision must not be reused as cost-basis precision.

## IOC execution

- Buy4 PURR at4.6252:18.5008 USDC debited,0.0028 PURR fee,3.9972 PURR credited.
- Sell3 PURR at4.5795:13.7385 USDC gross,0.00961695 USDC fee,13.72888305 USDC credited.
- Sell `closedPnl` excludes the separately reported fee. Sold and remaining basis exhibit separate eight-decimal truncation in this sample.
- Filled responses include cloid when supplied; fills also contain cloid and `twapId:null`.
- No match: `Order could not immediately match against any resting orders. asset=10000`; cloid lookup status`iocCancelRejected`.
- Minimum: `Order must have minimum value of 10 USDC. asset=10000`; cloid lookup status`minTradeNtlRejected`.
- Invalid market size precision: per-order `Order has invalid size.`; lookup`unknownOid`. The engine rejects rather than rounding that wire size.
- Nonzero ninth decimal in either p or s: HTTP422 before recovery. Nine trailing zero decimals were accepted through recovery. Scientific notation, negative values, NaN, empty strings, JSON numbers and an overflowing numeric string were rejected at the same HTTP layer.
- The early `partial-fill` scan was inconclusive, but later `orders-rounding-partial` and `matching-contract` obtained positive partial execution with terminal status`filled`. Affordability uses actual execution prices and permits affordable lot-aligned partial fills rather than requiring the full limit notional.
- `orders-cloid-batch` and `late-contract` establish reusable terminal cloids and static validation before any order in the batch executes. The latest terminal order owns cloid lookup; prior oids remain addressable.
- `rounding-canonical` and `late-contract` distinguish quote-first fee quantization: `floor(f64(gross × quoteWeiUnits) × f64(rate))` quote units, then division by execution price and base-precision truncation for buys. Directly multiplying base size by rate misses measured wei edges.
- Minimum ordering: absent crossing liquidity wins over minimum/funding; zero paying balance then wins over minimum. With positive funding, full requested size times the first crossing price determines the minimum. One USDC wei with NQ ask`1.024` yields minimum rejection for size`9.76`, but insufficient balance for`9.77`.
- Exact band: `0.2 × mark < limit ≤ 5 × mark`. NQ mark`1.0211` rejects`0.20422`, accepts`0.20423` through static validation, accepts`5.1055`, and rejects`5.1056`. TEST8 mark`1.0` rejects`0.2`, accepts`0.20001`, accepts`5`, and rejects`5.0001`; the excluded lower endpoint is not merely an NQ floating-point artifact.
- TEST11 multi-level fills: `3.07@7.6889 + 0.01@7.7119` returns order average`7.688974` and aggregate price`7.6889746753`. `3.32@7.7119 + 0.01@7.735` returns`7.711969` and`7.7119693694`, distinguishing ten-place rounding from truncation.
- The original upgrade-only errors allocated no observable terminal order, but later replay of both signed envelopes returned duplicate nonce. The local upgrade control models this admitted rejection; historical enable times are not inferred from the errors.
- Independent maker and aggressor histories agree on a PURR trade at`1789717712202`, price`4.6252`. The return transfer executed at`1789717712406`, 204 ms later; its earlier captured mark was`4.5795`. This supplies an independent price input, not a price reverse-engineered from the tested ledger.
- Both participants' independent NQ fills corroborate price1.0211 at1788569253646 before `quote-activation/buy-quote` at1789713293880. The approximately13-day age is recorded; account-specific history cannot prove absence of intervening trades.

## Fill history limits

- The public maker's raw `userFills` response contains 2000 fills but only 1732 order/time groups. Its aggregate response contains 2000 groups and reaches older history, proving aggregation happens before the cap.
- The independent `userFillsByTime` history plus the overlapping raw snapshot supplies 2268 individual fills. The regression compares their 2000 aggregate groups against every field of the captured aggregate response, including weighted prices and first-fill identities.
- These captures contain consecutive groups; they do not establish behavior for interleaved fills of the same order and timestamp.

## Activation gas selection

- [`activation-priority`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-priority/report.json): A had affordable USDC and NQ; sending1 NQ to fresh E debited2 NQ, with ledger fee1 NQ. E returned exactly1 NQ fee-free.
- [`activation-priority-usdc`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-priority-usdc/report.json): under the same competing assets, a1-USDC activation send charged1 USDC.
- [`activation-priority-purr`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-priority-purr/report.json): a1-PURR activation send selected USDC rather than NQ. The principal was returned fee-free.
- [`activation-fallback`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-fallback/report.json): existing B held exactly1 NQ and1 USDC. Its1-NQ send to fresh H rejected with `Insufficient NQ balance for token transfer gas.`; balances, ledger and fills were unchanged and H remained missing. Both prefunds were returned. Thus same-quote gas is mandatory, not merely preferred with an alternate-asset fallback.
- [`activation-alternate-gas`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/activation-alternate-gas/report.json): empty B received NQ first, then USDEEE and PURR. Its1-PURR activation send selected1 USDEEE(index1295) over available NQ(index1424), with no USDC. The PURR and untouched NQ were returned; B/H ended empty. This supports index ordering rather than prefunding order for those candidates.
- The original simulator's USDC-first selection reproduced six exact financial/ledger mismatches in `captures/local/activation-priority-before-fix/golden-extended.json`. These new immutable captures are regression inputs, not expected post-state fixtures.

## Non-USDC quote accounting

- [`quote-accounting`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/quote-accounting/final-report.json) acquired16 USDEEE at0.9 for14.4 USDC, with fee0.00223998 USDEEE. It retained13.06994802 USDEEE and0.039104 LMA after the bounded sequence; no cleanup trade was submitted.
- One USDEEE wei at mark0.9 credited zero recipient entry basis. Returned basis did not restore the sender's truncated wei:14.4 became14.39999999 USD.
- At an11-USDEEE ask, requested LMA sizes0.89/0.95/1.02 produced minimum/insufficient/insufficient rejections with only one quote wei available.10.45 USDEEE qualified despite being worth9.405 USDC; the minimum error names USDEEE.
- Buying1.28 LMA for14.08 USDEEE created12.672 USD LMA basis and reduced USDEEE basis to1.72622568 USD. Selling1.24 LMA at9 yielded11.16 USDEEE gross, fee0.007812 USDEEE, remaining LMA basis0.38740078 USD, received-quote basis11.77022568 USD, and quote-denominated `closedPnl:-2.48955467`.
- LMA/USDEEE and independently recovered KRWIN/USDYP fills contributed zero daily fee-tier volume. USDC controls [`fee-share-one`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/fee-share-one/) and [`fee-share-half`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/fee-share-half/) contributed18.12 and23.88 respectively despite deployer shares1.0 and0.5. Deployer share therefore does not explain the quote-market exclusion.
- [`quote-volume-delayed-recovery`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/quote-volume-delayed-recovery/reconciliation.json) repeated only fills and fees2,736.05seconds after the earlier recovery. Fill history was identical and daily `userCross` remained364.46, corroborating exclusion rather than credit delayed within that45.6-minute interval. No signed action or retry was submitted.

## Native cloid visibility boundary

[`quote-volume-usdyp`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/quote-volume-usdyp/) acknowledged filled order60429691261, then its immediate cloid query returned `unknownOid`. The probe halted; no order was resubmitted. [`quote-volume-usdyp-recovery`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/quote-volume-usdyp-recovery/) later confirmed both cloid and numeric OID, complete balances/fills/fees, and unchanged fee-tier volume after the KRWIN fill.

The unknown query's recording started399.682ms after the native event; positive LMA and USDYP queries had recording starts331.819ms and370.971ms after their respective events. These are not the instants at which visibility was observed. A fixed threshold evaluated at recorded request-start time cannot reproduce the responses. Actual server lookup instants remain unknown; these data do **not** prove inherent randomness. No guessed delay, status normalization, or expected-response override repairs this case.

The offline `captures/local/cloid-visibility-analysis-v1/analysis.json` checks identical original/recovery request bytes and source hashes. Recorder `started_at` precedes its pacing wait, so it is an operation boundary, not server receipt. A hypothetical common server threshold remains feasible within the unobserved metadata windows `(399.682,1092.529]` milliseconds; these samples do not identify one. Curl trace header clocks, interpreted as UTC, also exceed metadata finish times by80.699–85.288ms, so those absolute clocks cannot safely be combined without calibration. None of this changes the exact unknown→filled observation.

## Fees and volume

- `matching-contract` measures stable quote-pair fee and volume weight`0.2`. Registered quote context is not a name heuristic. The documented AQA multipliers are`0.8` for fees and`1.2` for volume; explicit alignment context is needed because ordinary metadata does not fully identify it.
- Ordinary fills contribute `2 × trunc2(gross × volumeWeight)` per fill to UTC-day user taker volume. Captured fractional changes distinguish truncating before doubling from other orders of operations.
- The engine reports today plus14 preceding daily rows and selects tiers from14 completed UTC days, without importing the fee fixture donor's user history. External schedules, discounts and global volume remain fixtures.
- VIP qualification at exact cutoff equality has **not** been live-measured. The local strict-`>` choice and its equality behavior are labeled assumptions in `captures/local/fee-parity-v2/results.json`, not promoted to measured behavior.

## Books, balances, and dust

- Unknown book coin returned HTTP200 JSON`null`.
- Supported aggregation samples: nSigFigs2–5; explicit mantissa2/5 with nSigFigs5. nSigFigs1/6, mantissa1/3, and mantissa without sigfigs returned500/null in this run, despite the documentation listing mantissa1.
- Balance rows include `entryNtl`. Immediate zero balances were retained on B after transfers.
- The original0.9972 PURR disappearance is no longer evidence for a generic “retain dust forever” rule. Later raw fills show `Spot Dust Conversion`; isolated historical oid queries can still lack a mapped creation episode.
- `dust-lifecycle-01` observed pooled A/D dust at a UTC minute boundary; `dust-followup-02` repeated an equal`0.5 + 0.5` split, and `dust-split-asymmetric` tested`0.25 + 0.75`. Proceeds use quote-wei floor allocations, with residual assigned to the largest holder and equal-size ties to the lexicographically lowest address. The asymmetric credits were1.14407358 and3.43222077 USDC, conserving4.57629435 net.
- Pooled whole lots execute, while a below-lot pool can burn without proceeds. Each conversion removes its base row/basis. Raw fills carry zero hash, `tid:0`, `fee:"0.0"`, and dust direction; `orderStatus` returns`unknownOid`. The aggregate-by-time feed excludes **all** dust fills. User fee volume and ledger do not change; net distribution nevertheless reflects the pool's system execution fee.
- Testnet conversion occurs near minute boundaries and includes owned balances above$1. [HIP-1](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-1-native-token-standard) documents mainnet midnight UTC and a$1 eligibility cap; neither should be silently imposed as a measured testnet rule. The exact testnet cap and atomic network-wide pool/depth remain outside these observations.
- No “last account balance” is frozen here: later immutable runs contain additional authorized activity and their own final snapshots.

## Reproduction boundaries

Read-only queries are repeatable inputs, not immutable market data. Signed reruns need fresh nonces, current balances, and fresh recipients where specified. Exact signed payload replay is deliberate and separately labeled. [Golden replay](verification.md) instead seeds captured preconditions and sets the simulator clock, producing deterministic comparisons without upstream access.

Residual uncertainties include missing historical atomic market/depth context, hidden individual makers, isolated prior dust-ID prerequisites, exact nonce-window endpoints, priority among multiple non-USDC gas candidates for nonquote sends, testnet dust safeguards, metadata-hidden quote/alignment flags, VIP cutoff equality, aggregate-price halfway rounding, other size-precision order averages, historical upgrade timing, and live mainnet behavior. Measured minimum precedence, reference endpoints, weighted-price behavior and mandatory same-quote activation gas are no longer blanket unknowns. [Scoped verification](verification.md) distinguishes exact passes from missing prerequisites.
