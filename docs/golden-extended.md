# Extended signed golden replay

Run against a **disposable**, already-running testnet-configured simulator loaded with the captured `spotMeta`:

```sh
python -m probes.golden_extended \
  --base-url http://127.0.0.1:3000 \
  --source-root captures/testnet
```

The output directory must be new/empty and disjoint from the source tree. The simulator is reset between suites. Only literal loopback HTTP addresses are accepted; the testnet URL is explicitly rejected. No signing SDK, wallet, environment file, private key, or upstream HTTP request is used. `curl` and the existing Recorder are the transport prerequisites.

## Discovery and scope

Default suite order is explicit and deterministic:

1. `accounts-dust-admission`
2. `accounts-transfer`
3. `orders-cloid-batch`
4. `orders-edge-contract`
5. `orders-rounding-partial`
6. `quote-activation`
7. `matching-contract`
8. `rounding-canonical`
9. `refinements-aliases`
10. `refinements-signed`
11. `late-contract`
12. `orders-multilevel`
13. `recovery-chain`

`dust-followup`, `transfer-valuation`, `reference-contract`, `dust-split-asymmetric`, `minimum-contract`, `minimum-nq`, `minimum-funded-02`, `band-nq`, `band-test8-02`, `band-inside`, `orders-multilevel-02`, and `orders-multilevel-04` are optional supported suites, all using the same chronological replay path rather than case-specific outputs. Select immutable runs explicitly, for example `--suites dust-followup,dust-split-asymmetric,minimum-contract --capture dust-followup=captures/testnet/dust-followup-02`. `--capture SUITE=PATH` is repeatable; absent/incomplete captures remain gaps. Existing nonce and recovery prerequisite chains are unchanged. The aborted `band-test8-02` capture is not a completed parity episode; select its completed replacement with `--capture band-test8-02=captures/testnet/band-test8-03`.

Further optional suites are `activation-priority`, `activation-priority-usdc`, `activation-priority-purr`, `activation-fallback`, `activation-alternate-gas`, `quote-accounting`, `fee-share-one`, `fee-share-half`, and `quote-volume-usdyp`. They use the same replay machinery. The USDYP episode appends its immutable `quote-volume-usdyp-recovery` read-only observations chronologically; the original aborted workflow and transient unknown-cloid response remain visible.

Within a suite, completed manifest requests are sorted by recorded request start time (manifest order breaks ties). The suite is a continuous episode: nonce admission, terminal cloid reuse, account activation, and batch effects are not reset between actions. `--suites accounts-transfer,orders-cloid-batch` selects a smaller explicitly reported scope; success for that selection is **not** full default coverage. `refinements-unsigned` is not a standalone signed suite, but its original error responses are included in the recovery chain.

The generic signed episode path covers recorded self-send failures, decimal and token-alias errors, failed-action nonce replays, full and partial IOC results, reusable terminal cloids, empty/batch action errors, balance/liquidity precedence, alternate-quote activation, fill fees, and weighted volume deltas. A label is not evidence of a successful fill or rejection: the recorded response determines the expected result. Every captured balance, role, pre-transfer check, fills/ledger snapshot, fee snapshot, and order-status observation in a selected suite is accounted for. Missing observations are not fabricated.

`recentTrades` and in-episode `userFillsByTime` remain `auxiliary` diagnostic observations outside the selected simulator endpoint scope. They are not replayed, counted as parity passes, or reported as missing required endpoints. Explicit external `userFillsByTime` observations can additionally supply independent historical mark inputs under the restrictions below.

## Fixtures, not expected-output seeding

- Before each user's first signed action involvement, the earliest successful captured balances, role, and `preTransferCheck.userHasSentTx` establish initial account state. Each visible balance row is funded with its **exact** `total` and `entryNtl` through external `/_test/fund` transfer mode, with the independently captured boolean `userHasSentTx`. Both sources have separate provenance. Zero rows remain zero rows. Nonzero holds and existing accounts without any visible row are explicit unsupported prerequisites, not guessed balances. Missing accounts are left missing. A zero or token-precision-invalid amount cannot monetarily touch the recipient, so that recipient does not require an invented balance fixture; signer pre-state is still required.
- Signers come from recorded public signer metadata, matching prior action/nonce/envelope metadata, or the suite's sole established signer. Original `/exchange` request bytes, content type, SDK envelope fields, signatures, and nonces are replayed unchanged. Signer ambiguity is a gap.
- Provably unsigned requests are compared directly without invented signer/account state. The existing duplicate-key-aware validator recognizes missing/zero signature scalars; raw non-object JSON is also forwarded unchanged.
- Captured `l2Book` responses are independent input fixtures. Only submitted order markets, their non-USDC quote-to-USDC valuation markets, and transferred tokens' USDC valuation markets are dependencies. Unused scanned markets are `unusedcontext`, **not passing tests**. Depth is never reconstructed from expected fills, prices, balances, or residuals. Prior `spotMetaAndAssetCtxs` supplies independent `markPx` inputs; missing marks, including quote FX, remain gaps rather than inferred expected-output fixtures.
- The default clock is request metadata `started_at`, converted to milliseconds; initial fixtures retain that clock. Signed actions may align to a unique native execution time correlated solely by identity: successful `sendAsset` uses ledger signer plus nonce; orders use response OID or cloid, then `statusTimestamp` and matching fill times. Response OIDs take precedence over reusable cloids. No price, amount, size, or valuation chooses the time. The sole candidate must lie within the recorded HTTP start/finish interval. Candidate identities, source hashes/JSON paths, interval, decision, and resulting clock are recorded. Missing native time retains request start, never blindly response end; multiple candidate times or an out-of-interval candidate are explicit gaps and retain the capture clock.
- Failed exchange responses never borrow a prior successful execution's clock. A reusable cloid can identify a new order only through an observation whose native timestamp is inside the current request interval.
- Captured fee schedules, discount fields, and global exchange activity are external inputs through `/_test/fees`. Donor `dailyUserVlm.userCross/userAdd` are zeroed in the fixture; donor user volume is **never** imported. Comparisons use exact decimal differences between consecutive captured/local daily volumes, including fractional weighted-volume behavior. All remaining fee response fields are compared; schedule/global-activity equality is fixture round-trip coverage, not a claim to simulate the external exchange.
- Initial fill and ledger history is not seeded. Captured pre-action history and locally observed funding history establish **fixed pre-episode baselines**, not rolling snapshots. Every later query compares cumulative episode activity by ordered multiset subtraction; a previously observed fill disappearing therefore fails instead of silently cancelling out. JSON object-key ordering does not affect row identity. Keys use effective endpoint options: `userFills` includes its aggregation boolean (ignored time filters do not create different histories); ledger keys retain their time window.
- Exact-query pre-action history is preferred. An aggregated query may fall back to an unaggregated baseline only when it contains fewer than 2000 rows: a capped raw feed cannot cover the older groups in a capped aggregate feed. The fallback excludes pre-existing `(oid, time)` groups without inventing their aggregate prices or fees. A covering ledger baseline can establish a narrower window, but a 500-row oldest-first baseline is not assumed complete. Filtered nonce proofs never replace full-history baselines. Missing coverage remains a gap; neither old source history nor synthetic funding ledger is counted as episode activity.
- Metadata is checked against a captured `spotMeta` response. Boundary suites that omit it may use the lexicographically first independent `*/setup-spot-meta/response.body` under the source root; its path and SHA-256 are recorded. The simulator must use compatible metadata.

### Independent prior context

Repeat `--external-context captures/testnet/RUN/CASE/response.body` to opt into independently captured `spotMetaAndAssetCtxs`, raw `l2Book` (request fields exactly `type` and `coin`), or unaggregated `userFillsByTime`. The enclosing capture must be completed and its manifest/body hashes must agree. Context/book responses become available only after their recorded HTTP finish, at or before the replay clock. Their observation start times govern freshness; stale inputs never overwrite newer marks/books.

Historical fills are mark-only evidence for exact known spot coin names in the episode's dependency markets. They are not imported into user balances, fees, fills, or ledger history. Their actual event timestamps, not retrospective HTTP capture times, govern availability: the latest event must be **strictly before** the action clock. Events correlated to the tested order's OID or fill hash cannot supply its own price. Timestamps alone order events; neither `tid` nor hash establishes chronological order. Multiple prices at one timestamp remain an explicit ambiguity, not a guessed last price. A newer explicit context wins over older history (and wins ties); newer unambiguous evidence can supersede older ambiguity.

Every historical mark records source request/response/manifest hashes, HTTP interval, event timestamp, JSON row path, hash/OID identity, and event age. Maker/account-specific history can omit intervening trades even when the requested window is unsaturated; the manifest reports this limitation. It is not proof of complete execution-time market causality. For the original valuation episode, opt into `captures/testnet/independent-market-history/maker-window/response.body` and optionally its corroborating `aggressor-b-window/response.body`. The completed `independent-market-history-wide/maker-*/response.body` windows use the same repeatable option; no automatic discovery reads in-progress captures.

The NQ reference before `quote-activation/buy-quote` is independently corroborated by both `nq-prior-trade/actor-0-prior-fill/response.body` and `actor-1-prior-fill/response.body`: price1.0211, event time1788569253646, hash`0xf8f6a1cb16871cc2fa7004288d783f010500b9b0b18a3b959cbf4d1dd58af6ad`. The action starts at1789713293880. These old account-specific observations close the missing-reference prerequisite, not the possibility of unobserved intervening trades. Opt into the two files explicitly; discovery via `recentTrades` is not itself a parity assertion.

### Recovery and nonce prerequisites

Isolated `quote-activation` and `rounding-canonical` replays append their immutable terminal `orderStatus` observations from `late-contract`, at the recovery requests' actual captured times. Full recovery balances/history/fees are **not** safe isolated-suite fixtures: intervening signed actions changed the same account. An observed existing order queried by cloid requires that cloid's original signed action in the replay; otherwise the observation is an explicit historical-action gap, including isolated `late-contract/recovery-rounding-status-0`. Negative unknown-cloid probes still compare normally. Numeric IDs still require an established mapping.

`recovery-chain` therefore replays, without intermediate resets, chronological records from `accounts-dust-admission`, `accounts-transfer`, `accounts-nonce-floor`, `quote-activation`, `orders-cloid-batch`, `orders-edge-contract`, `orders-rounding-partial`, `omitted-order-defaults`, `matching-contract`, `rounding-canonical`, `refinements-unsigned`, `refinements-signed`, `refinements-aliases`, `dust-lifecycle-01`, then the `late-contract` recovery observations. Original episode/case hashes and times remain attached to every record; case names are qualified to avoid collisions. Recovery snapshots are compared with complete fields and ordered history deltas, never imported as expected post-state. Missing intervening inputs/actions remain explicit gaps. Incomplete canonical-rounding transport remains a transport-evidence gap even when later recovery supplies valid semantic evidence.

The chain also appends only `dust-followup-02/old-a-dust-status` and `old-d-dust-status`, at their actual observation times, after the original dust lifecycle establishes the IDs. Their isolated-suite gaps remain honest; both compare successfully in the complete chain.

`refinements-signed` independently prepends `accounts-dust-admission`, `accounts-transfer`, and `accounts-nonce-floor` as chronological setup. Dust admission supplies C's two admitted nonces `1789712489389` and `1789712501381`; both exceed all 105 later backdated seed nonces. Together these establish the captured 100th-largest floor `1789709474349`, rather than the incomplete 105-seed-only floor `1789709471290`. The original rejection and duplicate assertions remain exact. Original signed-file hashes are checked. Successful setup comparisons are labeled `fixture`, not added to that suite's tested-pass count. No nonce floor is invented or directly injected.

## Exact comparisons and normalization

HTTP statuses and non-200/non-JSON error bodies compare exactly, byte for byte. JSON responses compare recursively with identical fields, types, array order, and decimal strings. HTTP-200 error strings/envelopes receive no special error-text normalization.

The only generated-value substitutions are:

- `oid`, `tid`, and `hash`: consistent **bijective**, per-suite mappings, shared by exchange responses, fills, ledger entries, and order status. Changed types, invalid generated identifiers/hashes, collisions, or inconsistent reuse fail. Numeric order-status request IDs are translated through the established order-ID map; cloids are never rewritten.
- `time`, `timestamp`, and `statusTimestamp`: a shared consistent per-suite mapping. Distinct source times may map to the same local fixed-clock time. Repeated source timestamps must keep their mapping. Time fields are not deleted. This verifies timestamp presence/type/reuse, **not** live request/settlement latency or exact wall-clock values.

Economic L2 sizes alone may be canonicalized to a lot grid when the recorded aggregation count `n` provides a bound: `abs(size - nearest_lot) <= n * ulp(float(size))`, and **no other lot** lies in that error interval. Positive finite sizes, positive integer counts, and a positive resulting lot are required. Exact decimal arithmetic checks the bound and uniqueness; larger genuine excess precision stays a gap. Every change records its exact source response path/hash, JSON field path, original/normalized size, lot, count, error, and bound. This input-only normalization never changes expected balance/fill/error strings.

No balance rounding tolerance, fee tolerance, status rewriting, ignored behavioral fields, fill aggregation, or ledger `usdcValue` deletion is permitted. Partial IOC residual sizes and terminal status are ordinary exact fields. Mapping tables are persisted in the manifest.

## Gaps and result contract

The replay deliberately does not promise that a live L2/mark snapshot taken before submission is the atomic execution context. Market movement can cause a mismatch; expected fills must never repair the fixture. Orders lacking a pre-action book or independent reference mark are marked `unrecorded-market-context` while exact responses are still exercised. Successful non-USDC sends without a separately captured mark are likewise explicit valuation-context gaps; complete ledger and balances still compare, without dropping `usdcValue`.

The `valuation-purr-return` context finished at `1789717710056` with `markPx=4.5795`. Independent retrospective maker and aggressor records contain a PURR/USDC trade at `1789717712202`, price `4.6252`, hash `0xa460fd954206e55ea5da0429960f71010300157add0a04304829a8e8010abf49`. Its event precedes the transfer's signer/nonce-correlated ledger execution time `1789717712406` by 204 ms. With the independent history explicitly supplied, that trade can update the replay mark before the transfer; the ledger supplies only its native time and identity, never a price fixture. Without that source the earlier context remains. All financial comparisons stay exact; residual valuation-only failures retain non-atomic/missing-context provenance rather than being suppressed. This is independent price evidence, not a claim that maker-specific history is exhaustive.

Missing depth is separately causal: `orders-edge-contract/insufficient-quote` has no recorded pre-action book, so its differing rejection and terminal status remain measured fixture-dependent failures, not conclusive engine failures. A sub-lot transfer requires an independent book to determine dust eligibility. A captured one-sided book establishes ineligibility; it is not missing evidence. Missing the book entirely is an explicit gap, with subsequent affected balance/fill discrepancies retained. A mark alone is not dust-depth evidence.

Other explicit gaps include absent/in-progress suites, pending source requests, source transport/rate-limit failures, missing pre-state, unsupported non-IOC transitions, and unmapped prior order IDs. Non-IOC requests are still exercised and compared, but no full transition-support claim is made. Recovered evidence never rewrites an earlier transport failure or supplies unknown execution-time depth/marks. New well-instrumented late, multilevel, dust-followup, and valuation episodes strengthen their own coverage without making old inputs known.

A successful recorded IOC additionally requires later captured balances, fills, fees, and terminal status for every returned filled order ID. Missing pieces generate a per-action coverage gap even if the exchange response itself matches. This includes purchases in transfer-oriented suites that did not query order status.

`golden-extended.json` records every case outcome (`passed`, `mismatch`, `fixture`, `unusedcontext`, `auxiliary`, `gap`, or `skipped`), reasons/errors, provenance, selected coverage, request counts, and summary. A case can have both a context gap and a comparison. Prerequisite failure records remaining episode cases as skipped. `external_evidence_gaps` and `observed_failures` are separate. `fixture_dependent_failures` classifies discrepancies after missing/non-atomic valuation context, missing order depth/reference, or sub-lot transfers lacking an independent dust book. Exact errors and causal cases remain visible; classification never changes comparisons or excludes possible engine bugs. Other discrepancies remain in `engine_failures`. Both retain mismatch exit precedence.

Reports save `configuration.source_root`, `configuration.capture_overrides`, and the exact ordered `configuration.external_contexts`, alongside `selected_suites`. Reuse these with a fresh output root; no directory-wide discovery silently adds later evidence.

Exit codes:

- **1**: at least one observed comparison mismatch, even if gaps also exist.
- **2**: no mismatch, but at least one gap/skip; never full parity.
- **0**: complete comparisons for the explicitly selected supported scope, with no reported gaps.

The USDYP episode's immediate native `unknownOid` and later filled recovery are both compared. Identical user/cloid request bodies returned different statuses at different times. Native server lookup/index visibility is not captured; no fixed client-time delay or expected-response override is supplied to manufacture parity.

Recorder captures **every local request**, including setup, clock, baselines, and controls. Request metadata carries the source episode's manifest hash and, where applicable, exact source request/response hashes and case ID. Source run and metadata provenance are recorded. Source body hashes are checked, source captures are opened read-only, and a changed manifest at episode completion becomes a gap. Run only against completed immutable capture sets.

Within each episode, multiple external files from one capture root share one validated `Capture`; caches are discarded between episodes and completion still checks source manifests. The 24-suite `integrated-parity-v7` run took303.94seconds with peak RSS1,686,520KiB measured by `/usr/bin/time`; v6 took669.69seconds and had an observed RSS sample of18,415,096KiB (not a measured peak). This optimization changes neither fixtures nor expected outputs.

The negative HTTP smoke in `captures/local/validator-parity-v8/results.json` passed7 checks across52 requests. It deliberately reset the simulator between repeated fill queries and confirmed missing-fill detection; it also exercised raw-array comparison, rejected capped raw history as an aggregate baseline, and accepted a captured aggregate baseline. This proves validator boundaries, not simulator parity. Current results and reproduction commands are in [verification](verification.md).
