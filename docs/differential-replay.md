# Unsigned HTTP differential replay

`probes.differential` replays every completed request in one Recorder manifest against a **literal loopback HTTP address only**. It supports the 150-case `protocol-parity` run and the older `readonly-contract` and `unsigned-shapes` runs. It does not replace `probes.golden`, authorize actions, load credentials, modify source captures, retry requests, or contact testnet.

## Run

Start the simulator with the exact captured metadata, then run the replay in another terminal:

```sh
cargo run -- --network testnet --meta captures/testnet/protocol-parity/info-valid-spot-meta/response.body --bind 127.0.0.1:3000
uv run python -m probes.differential --source captures/testnet/protocol-parity
```

The default `--base-url` is `http://127.0.0.1:3000`. Only the Recorder literal-loopback URL form is accepted; testnet is explicitly rejected. Use a dedicated simulator: replay clears its state with `/_test/reset`, fixes its clock from each request's `started_at` metadata, and injects local fixtures. The evidence root must be new or empty and outside the source run.

Older captures use the same CLI, one source run and new output root per invocation:

```sh
uv run python -m probes.differential --source captures/testnet/readonly-contract
uv run python -m probes.differential --source captures/testnet/unsigned-shapes
```

Restart the simulator with the corresponding captured `spotMeta.json` or spotMeta `response.body` when changing metadata snapshots. A spotMeta response is compared in full, not patched or filtered; a mismatch explicitly identifies the metadata prerequisite. A shape-only source without a metadata query does not independently establish metadata parity.

## Comparison contract

- Manifest `completed` request rows determine order and membership. Duplicate completed IDs and invalid IDs are rejected. Pending-only IDs are listed as coverage gaps, never silently treated as completed. Request and response SHA256 values must match the recorded manifest.
- The original `request.body` bytes and recorded request Content-Type are passed directly to Recorder. Duplicate keys, malformed JSON, trailing input, numeric representations, and absent Content-Type are not regenerated from parsed JSON.
- HTTP status and final response Content-Type values are compared exactly. Header names are identified case-insensitively; header value spelling is retained. Other transport headers are not part of the comparison contract.
- JSON responses compare every value and field, with array order and numeric types retained. JSON serialization differences (object ordering, insignificant whitespace, string escapes, and equivalent numeric lexemes) are normalized, explicitly recorded under `normalized_paths`. Non-JSON bodies compare byte-for-byte. `response_bytes_equal` separately reports literal byte equality, including for JSON. No IDs, timestamps, fees, balances, error strings, or response fields are dropped.
- Source `/exchange` requests must be demonstrably unsigned before submission. Every duplicate named signature is inspected without collapsing object keys; sequence-root signatures are inspected too. A signature must be absent/null or have at least one missing or provably zero scalar. Potentially signed or opaque malformed-signature input is refused and recorded as a failure; other cases continue. Only captured POST `/info` and `/exchange` paths are accepted.
- A failed source transport has no authoritative response and is `not_covered`; a failed local transport is an unexpected mismatch. Setup failures also produce a nonzero exit. There is no stop-on-first-mismatch behavior.

## World-state boundaries

| Case | Coverage |
| --- | --- |
| Parser/schema errors and unsigned exchange outcomes | Exact status, Content-Type, and full body comparison. |
| spotMeta | Exact JSON metadata; wrong simulator metadata is a failure with a restart instruction. |
| l2Book returning `null` | Exact comparison, including market-name/alias behavior. Null is not classified as a dynamic book. |
| Aggregated l2Book (`nSigFigs` set) with a same-run raw snapshot | `raw_depth_prefix`: the nearest unaggregated two-sided snapshot of the same coin from the same run is seeded through `/_test/book`, then `coin`, `spread` and every bucket that snapshot fully covers (bids at or above its deepest bid, asks at or below its deepest ask) compare exactly. Deeper buckets are listed under `skipped_paths`; `time` is skipped. Still counted as a coverage gap, never as full parity. |
| Other l2Book returning an object | `status_schema_only`: exact HTTP status and Content-Type plus the documented coin/time/optional-spread/two-sided-level response shape. Market values are explicitly skipped. No book is seeded from the target's aggregated response. |
| Fresh account queries | Exact full output after reset. Freshness is established by captured `userRole:{role:"missing"}` **and** `preTransferCheck.userExists:false`, or by an explicit repeatable `--fresh-user ADDRESS` assertion. The protocol-parity fresh address is established by those captures, not by its appearance. |
| Existing account queries | `not_covered` without an explicit account fixture covering that address and query. Status and Content-Type still compare; a coincidentally empty result is not counted as full parity. |
| Valid userFees | Before each query, `/_test/fees` receives `{ "fees": <complete captured userFees object> }`. This supplies the external fee schedule and global daily exchange volumes, not user trading history. User daily volumes remain engine-computed. Full body comparison still requires fresh-account or explicit account-fixture context. |
| Numeric `/info` discriminator `42` | Explicitly `excluded`, with status/body/Content-Type listed as skipped and the reason “outside selected spot scope.” The request is not replayed. |

An aggregated live book does not reveal the atomic orders that produced it, and the raw feed is truncated to its top levels, so only the bucket prefix that a same-run raw snapshot fully covers is compared; the snapshot's time offset is recorded as `book_fixture.offset_ms`, and a moved market between the two captures surfaces as a mismatch rather than being hidden. A temporally exact, untruncated raw-depth fixture would be needed for full book coverage.

## Explicit source account fixture

`--account-fixture FILE.json` supplies local funding operations and declares exactly which source account/query combinations they reproduce:

```json
{
  "accounts": [
    {
      "address": "0x2222222222222222222222222222222222222222",
      "queries": ["spotClearinghouseState", "userRole", "preTransferCheck"]
    }
  ],
  "controls": [
    {
      "path": "/_test/fund",
      "now_ms": 1789680000000,
      "body": {
        "address": "0x2222222222222222222222222222222222222222",
        "token": "USDC",
        "amount": "101",
        "mode": "deposit"
      }
    }
  ]
}
```

This is an illustrative local fixture, not a reconstruction of any existing capture. Controls execute after reset, in order, at their supplied times. Only `/_test/fund` is permitted, with the existing simulator funding semantics (including activation fee and generated ledger entries). Query names must belong to the seven supported account queries. The fixture is copied into the evidence root.

Declare a query only when the fixture actually represents its source state. A funding snapshot does **not** reconstruct fills, order history, arbitrary ledger hashes, or daily user trading volume. Such queries should remain uncovered when that state is unavailable. Declaring them forces full comparison and reports mismatches rather than injecting captured answers. Adding all query names does not make an inadequate fixture pass.

## Evidence and exit status

`comparison.json` at the output root contains all case records, setup control results, counts, pending-only source IDs, and `full_parity`. Every case has `coverage_kind`, `outcome`, all `failures`, `normalized_paths`, `skipped_paths`, and `external_context_requirement`. JSON value differences name exact response paths. No normalization/exclusion is implicit in a success count.

Each `case-NNNN/` contains its own `comparison.json` and copies of `source.request.body`, `source.response.body`, `source.response.headers`, and `source.metadata.json` when available. Replayed cases additionally have the normal Recorder raw request/response/header/trace artifacts. Excluded and refused cases still receive comparison/source evidence. Controls have separate `control-NNNN-*` captures. Comparison events also appear in the append-only manifest.

Exit codes:

- **0:** Every completed case fully compared with no mismatch, pending source case, exclusion, or coverage gap.
- **1:** At least one unexpected case mismatch, rejected unsafe source request, source integrity failure, or setup failure. All reachable cases are still saved.
- **2:** No unexpected mismatch, but exclusions, incomplete source requests, or world-state coverage gaps remain. This is not full parity.

Consequently the protocol campaign's explicit numeric-42 exclusion prevents an all-green exit even if every selected spot case matches. Inspect `mismatched_cases` separately from `coverage_gaps`; do not treat exit 2 as proof of complete coverage.

The completed `captures/local/integrated-protocol-v8/comparison.json` replay matched149 in-scope cases with zero mismatches and one numeric-42 scope exclusion. It exits2 and reports `full_parity:false`; this is a measured spot-protocol result, not a claim to support lending.
