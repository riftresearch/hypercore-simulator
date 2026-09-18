# Unsigned protocol campaign

`probes.protocol` defines **150 deterministic cases**: **58 info** and **92 exchange**. It records observations, not expected upstream results. This is parser/transport evidence, not proof of trading, transfer, or universal simulator parity.

## Commands

Supply a public 20-byte address, never a private key. The example address is a syntactically valid candidate, not a claim that an account exists.

```sh
uv run python -m probes.protocol --root captures/testnet/protocol-info-001 --suite info --user 0x1111111111111111111111111111111111111111
uv run python -m probes.protocol --root captures/testnet/protocol-exchange-001 --suite exchange --user 0x1111111111111111111111111111111111111111
```

Alternatively, run both suites serially in one new root:

```sh
uv run python -m probes.protocol --root captures/testnet/protocol-all-001 --suite all --user 0x1111111111111111111111111111111111111111
```

`--suite all` is the default. Do not run `all` plus both subsuites unless duplicate observations are intentional. Recorder requires a new or empty root and will not overwrite an existing run. Use `--base-url http://127.0.0.1:PORT` to target a local simulator; Recorder rejects non-testnet, non-loopback targets.

Inspect exact definitions without requests, Recorder construction, or capture-file creation:

```sh
uv run python -m probes.protocol --root /tmp/protocol-list-only --suite all --user 0x1111111111111111111111111111111111111111 --list
```

`--root` remains required for listing but is not used. `--list` also accepts either subsuite. No credentials, signing SDK calls, `.env` loading, or `--execute` flag are needed. A parent invocation may supply `uv run --env-file .env`, but this campaign neither needs nor reads key values itself. Existing Recorder secret filtering still applies.

## Safety and execution

Every exchange template has `r:"0x0"`, `s:"0x0"`, and `v:27`. Scalar-shape cases change only one scalar, leaving the other exactly zero. Cases that remove/null the entire signature cannot authorize anything. Recovery-ID changes, duplicate nonce keys, sequence-root envelopes, unknown fields, and every order/sendAsset variation retain zero scalars. There is no signer, wallet lookup, nonce reservation, automatic retry, funding call, or mutating test control. No authorized action exists for which balance reservations or pre/post mutation snapshots would be appropriate.

The runner uses the existing `matrix.info`, `matrix.report`, and `Recorder.request` interfaces. It executes one request per case in definition order and inherits Recorder's testnet minimum interval of 1.5 seconds and zero retries. A transport failure is captured, classified, and not retried; later independent cases continue. Interrupting leaves the existing recorder evidence intact. Restart with a new root rather than appending to an interrupted run.

## Coverage

| Suite | Cases | Boundaries |
| --- | ---: | --- |
| info | 58 | Nine named queries including spotMeta and the eight account/book queries; missing/null/numeric/unknown discriminator; null/array roots, truncated JSON, trailing second JSON value, duplicate type/user keys; JSON-suffix, text, and absent Content-Type; address presence/type/prefix/width/hex; oid presence/null/boolean/signedness/u64/float/string/cloid; optional start/end times and u64 bounds; aggregation boolean; coin shape; nSigFigs and mantissa dependence; unknown query fields. |
| exchange | 92 | Order/sendAsset nominal zero-signature templates; missing/null action/nonce/signature; nonce u64/signedness/string/float; vault/expiry shapes; unknown action variant; r/s missing/null/numeric/empty/unprefixed/padded/overflow and v missing/string/negative/byte-overflow/zero; order a/b/p/s/r/t/c, grouping and batch shape; sendAsset destination/token/sourceDex/destinationDex/action nonce/fromSubAccount/signatureChainId/hyperliquidChain/amount shapes; unknown fields at envelope/action/order nesting; array root and duplicate nonce. |

These are intentionally different semantic boundaries rather than every Cartesian combination of JSON types and fields. Existing `matrix` and `shapes` campaigns retain their independent evidence; this module does not alter or replace their captures.

## Case schema

`cases(user)` returns fresh ordered `Case` definitions. Each has:

- `name`: stable unique `info-...` or `exchange-...` capture-directory identifier.
- `suite`: `info` or `exchange`, also selecting the endpoint.
- `body`: JSON object or exact bytes. Bytes preserve duplicate keys, invalid JSON, array roots, and trailing input without normalizing them.
- `changed_field`: JSON-pointer-style field path, `/` for the root, or `header:Content-Type`; coupled aggregation uses both field paths.
- `hypothesis`: the boundary being investigated, not an expected response or an assertion that the service behaves that way.
- `content_type`: request header value, default `application/json`; empty means suppress the Content-Type header through curl.

`Case.metadata` adds `campaign`, `suite`, `changed_field`, `hypothesis`, and `intended_mutation` to Recorder's `parameters`. Baseline `changed_field` identifies the discriminating query/signature rather than implying that a previous request was mutated. Each variant copies its template; no request depends on a prior response.

`--list` emits a JSON array with `case_id`, `path`, `content_type`, `parameters`, and `request_body`. The latter is a string containing the exact UTF-8 request text, including duplicates or invalid syntax where applicable. It is not a parsed/normalized request object.

## Evidence interpretation

Recorder preserves `request.body`, `response.body`, response headers, curl trace, status, timing, transport errors, and request/response hashes. Bodies are never rewritten or replaced by a predicted result; `expected_status` is not supplied.

Additional `protocol_observation` manifest events classify observed HTTP 400 as `http-400-parser-candidate`, 422 as `http-422-schema-candidate`, and exchange HTTP 200 as `http-200-crypto-candidate`. Other status codes and transport failures remain distinct. These are **status-based candidates only**: read the raw response to establish whether a 200 is actually signature recovery failure or whether another stage produced a 400/422. A status alone does not establish validation precedence, accepted business behavior, or parity.

The templates deliberately use nonce zero, market `@0`/asset 10000, a shape-only `USDC:0x00000000000000000000000000000000` token identifier, amount/price/size decimal strings, and SDK-style `signatureChainId:"0x66eee"` / `hyperliquidChain:"Testnet"`. No claim is made that the placeholder token exists, the selected market is liquid, the supplied address exists, or stale nonce zero would pass authenticated admission. Invalid signatures cannot establish post-authentication business rules. Tests of malformed fields establish only the actually observed rejection stage.

Offline verification ran the real CLI's `--list`: 150 cases, 58/92 split, no duplicate names or byte-identical endpoint/header/body triples, and every exchange request had either an absent/null signature or at least one unchanged `"0x0"` scalar. No live requests, signed actions, or tests were run for this implementation.
