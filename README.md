# hypercore-simulator

A local, in-memory stand-in for the Hyperliquid spot HTTP API, for testing clients and strategies without touching the real exchange.

Its behavior is measured, not guessed. Every rule it implements was established by sending real signed requests to Hyperliquid testnet and recording the exact bytes that came back. Those recordings live in a companion repository, [hypercore-simulator-captures](https://github.com/riftresearch/hypercore-simulator-captures), and are replayed against the simulator to check that it still answers the way the exchange did.

## What it does

The simulator serves `POST /info` and `POST /exchange` with the same request shapes, response bodies, error texts and HTTP status codes as Hyperliquid, for this subset:

| Surface | Supported |
| --- | --- |
| `/info` | `spotMeta`, `l2Book` (with `nSigFigs`/`mantissa` aggregation), `spotClearinghouseState`, `preTransferCheck`, `userRole`, `userFees`, `orderStatus`, `openOrders`, `frontendOpenOrders`, `historicalOrders`, `userFills` and `userFillsByTime` (with `aggregateByTime`), `userNonFundingLedgerUpdates` |
| `/exchange` | `order` (spot limit `Ioc`, `Gtc`, `Alo` and stop/take-profit triggers), `cancel`, `cancelByCloid`, `modify`, `batchModify`, `scheduleCancel`, and `sendAsset` (spot to spot), with real EIP-712 / MessagePack signature recovery compatible with the official Python SDK |
| Accounting | balances and holds, USD cost basis, activation fees for fresh accounts, taker and maker fees with VIP tiers and rolling volume, nonce windows and replay protection, automatic dust conversion |
| Matching | price-time priority per market; users' resting orders and injected synthetic depth share the book, so accounts can build a real book with `Gtc`/`Alo` orders and trade against each other with maker and taker fills |
| Test controls | `/_test/fund`, `/_test/book`, `/_test/time`, `/_test/fees`, `/_test/dust`, `/_test/fault`, `/_test/upgrade`, `/_test/account`, `/_test/reset` to seed and inspect state |

State is in memory. A fixed clock, deterministic ids and a bounded single-writer core make runs reproducible, and the server sustains around 20,000 filled orders per second on loopback.

It runs as a binary or as a library. Both networks' `spotMeta` documents are compiled in, so nothing has to be downloaded at run time.

```sh
cargo run                        # mainnet signature domain and metadata on 127.0.0.1:3000
cargo run -- --network testnet   # testnet signatures, metadata and dust policy
cargo run -- --help
```

```rust
use hypercore_simulator::{Args, Simulator};

let simulator = Simulator::bind(Args::default()).await?;
let base_url = format!("http://{}", simulator.local_addr()?);
simulator.serve(shutdown_future).await?;
```

A minimal session: fund an account, install a book, and read it back.

```sh
curl -s localhost:3000/_test/fund -H 'content-type: application/json' \
  -d '{"address":"0x1111111111111111111111111111111111111111","token":"USDC","amount":"1000","mode":"transfer"}'
curl -s localhost:3000/_test/book -H 'content-type: application/json' \
  -d '{"coin":"PURR/USDC","mid":"4.6","depth":"100"}'
curl -s localhost:3000/info -H 'content-type: application/json' \
  -d '{"type":"l2Book","coin":"PURR/USDC","nSigFigs":3}'
```

[`docs/simulator-semantics.md`](docs/simulator-semantics.md) is the contract: every rule, which capture established it, and which parts are simulator policy rather than measurement.

## Limitations

- **Spot only.** No perpetuals, websockets, bridge, withdrawals, vaults or sub-accounts.
- **Only IOC behavior is measured.** Resting orders, post-only rejection, maker fees, self-trade prevention, cancels, modifies, triggers and scheduled cancels follow the public documentation and a matching loop recovered from the node binary, not testnet captures. Their error texts and edge cases are the simulator's best reading until a signed testnet campaign measures them; the semantics doc marks the whole section as unmeasured.
- **No market activity of its own.** Nothing trades unless a client does. Synthetic depth from `/_test/book` sits still, and there is no price drift; to simulate a moving market, re-post `/_test/book` or run accounts that quote.
- **The evidence is testnet.** Mainnet mode uses the mainnet signing domain, mainnet metadata and the documented daily dust policy, but no mainnet behavior has been probed and no mainnet parity is claimed.
- **Generated identifiers are local.** Order ids, trade ids, transaction hashes and timestamps are deterministic simulator values, not chain identifiers.
- **Some rules are assumptions.** Where testnet was not probed at a boundary (VIP cutoff equality, exact nonce window endpoints, testnet's dust cap) the semantics doc labels the choice; see its *Unverified compatibility* section.
- **The test controls are unauthenticated.** Bind to loopback unless the network is trusted.

## Parity tests

```sh
just parity
```

This builds the release binary, boots a testnet-mode simulator on port 3999, replays every suite against it, restarts with the protocol-parity metadata for that suite's replay, and prints a verdict. It takes about two minutes and needs Python 3.11+, `uv`, `just`, `curl` and `jq`.

| Suite | What it checks | Expected |
| --- | --- | --- |
| `cargo test` | engine rules, wire parsing, actor ordering, resting orders, cancels, triggers; three tests aggregate captured fills | 28 pass |
| golden | recorded signed orders and transfers replay to the captured responses, balances, fills and statuses | exit 0 |
| verify | fixture-keyed scenarios: funding, signatures, nonces, fees, depth, all four injected faults, SDK-signed resting orders, cancels, modify and a stop trigger | 303 assertions |
| differential readonly-contract | 77 recorded `/info` requests, byte-exact where fixtures allow | 0 mismatches |
| differential unsigned-shapes | parser edge cases: trailing bytes, duplicate keys, malformed decimals | 0 mismatches |
| differential protocol-parity | 150 malformed-envelope and query-shape cases | 0 mismatches |
| golden_extended | chronological account and order episodes with nonces, activation and dust carried across cases | 94 mismatches, all missing-context |

The 94 are cases whose USD valuations depend on a market snapshot the default run does not load; the archived full run with its 258 context files reports one, a genuine testnet quirk in cloid visibility. The expected counts are variables at the top of the `justfile`, and the recipe fails if any suite moves away from them.

Each suite writes its evidence to a temporary directory and prints the path. To run pieces by hand, or the mainnet-signature verification, see [`docs/verification.md`](docs/verification.md).

## Parity data

The recorded testnet sessions live in a separate repository, [hypercore-simulator-captures](https://github.com/riftresearch/hypercore-simulator-captures) (1.3 GB, about 40,000 files), so that depending on this crate does not pull them in. Each request there has its own directory with the exact `request.body`, `response.body`, `response.headers`, a curl trace and metadata; nothing is reconstructed from parsed JSON. It also holds the single public mainnet `spotMeta` fetch.

`just parity` clones it into `captures/` (gitignored) on first use; `just captures` does only that step. Point an existing checkout there with a symlink if you already have one.

The crate itself embeds three of those files, copied byte for byte into `bundled/` with their provenance and hashes in `bundled/README.md`: both networks' `spotMeta` and the testnet `userFees` baseline. Five more are copied under `tests/fixtures/` for the unit tests. Nothing else in the build or the library touches the evidence.

Regenerating evidence means spending testnet funds with the probe harness in `probes/`; [`docs/probe-harness.md`](docs/probe-harness.md) covers its safety rules. It only ever talks to testnet. Local replay output is a reproducible byproduct and is not stored.

## Layout

- `src/` – `engine/` (accounts, book, matching, transfers, dust), `wire.rs` (typed request surface), `crypto.rs` (signature recovery), `fees.rs`, `actor.rs` (single state thread and mailbox), `http.rs`, `lib.rs`.
- `probes/` – the Python recorder, replay suites and benchmark drivers.
- `docs/` – semantics contract, verification recipes, harness rules and measured observations.
- `bundled/` – the three captures compiled into the crate; `captures/` – the evidence checkout that `just parity` clones.
