# Testnet baseline and next probes

## Scope

This is observed testnet behavior, not yet a simulator specification or proof of mainnet parity. All seven requests in this session were read-only `POST /info` calls to `https://api.hyperliquid-testnet.xyz`. No signed actions, trades, or outbound transfers were submitted.

Account: `0xA76bA0c24C781E0961a103786fB35a75e00f92D3`.

Evidence root: [`captures/testnet/20260918T045515.026265Z-baseline`](https://github.com/riftresearch/hypercore-simulator-captures/tree/main/testnet/20260918T045515.026265Z-baseline/).

Each probe directory contains:

- `request.body`: exact submitted body, without reformatting.
- `response.body`: response payload saved by curl, without JSON reserialization.
- `response.headers`: received HTTP status line and headers.
- `transport.trace`: curl's timestamped hex trace of sent/received HTTP data, including request headers.
- `metadata.json`: URL, method, UTC start/end times, curl exit code/stderr, and curl transfer metadata.

Captures used HTTP/1.1, no retries, a 10-second connection timeout, and a 30-second overall timeout. These are HTTP application-layer observations, not encrypted packet captures. No private keys were sent or recorded.

## Observations

All seven probes returned HTTP `200 OK`, `Content-Type: application/json`, and curl exit code 0.

| Capture directory | Query | Observed result |
| --- | --- | --- |
| `spot-clearinghouse-state` | `spotClearinghouseState` | USDC token 0, total `"500.0"`, hold `"0.0"`, entryNtl `"0.0"`; no other balances returned. |
| `user-role` | `userRole` | `{"role":"user"}`. |
| `pre-transfer-check` | `preTransferCheck` | `{"isSanctioned":false,"userExists":true,"fee":"0.0","userHasSentTx":false}`. |
| `non-funding-ledger` | `userNonFundingLedgerUpdates`, startTime 0 | One inbound spot-to-spot USDC send, amount `"500.0"`, fee `"1.0"`. |
| `user-fees` | `userFees` | userSpotCrossRate `"0.0007"` (0.07%), userSpotAddRate `"0.0004"` (0.04%). Complete fee schedule and ancillary fields are in the raw response. |
| `user-fills` | `userFills` | `[]`. |
| `funding-sender-ledger` | `userNonFundingLedgerUpdates`, funding sender and narrow transfer-time range | The same transfer entry as the recipient's ledger, including the same positive amount, hash, fee, and nonce. |

Important shape details:

- `preTransferCheck.fee` is a decimal **string**, not a JSON number.
- An existing funded account can have `userHasSentTx:false`.
- The returned balance includes `entryNtl`, in addition to the initially requested balance fields.
- The transfer's `delta.type` is `"send"`. It includes `user`, `destination`, `sourceDex`, `destinationDex`, `token`, `amount`, `usdcValue`, `fee`, `nativeTokenFee`, `nonce`, and `feeToken`.
- This transfer's ledger token is `"USDC"`, not the action's `NAME:tokenId` representation.
- Both ledgers use the same positive `amount`; this observation does not support modeling the sender entry as a negative amount.
- Ledger addresses are lowercased, although these account queries used a checksummed address.

Exact transfer delta, observed identically on both sides:

```json
{
  "type": "send",
  "user": "0x33f65788aca48d733c2c2444ac9f79b18206aa92",
  "destination": "0xa76ba0c24c781e0961a103786fb35a75e00f92d3",
  "sourceDex": "spot",
  "destinationDex": "spot",
  "token": "USDC",
  "amount": "500.0",
  "usdcValue": "500.0",
  "fee": "1.0",
  "nativeTokenFee": "0.0",
  "nonce": 1789706493224,
  "feeToken": "USDC"
}
```

The enclosing entry has time `1789707218325` and hash `0xe343a82b0fc22037e4bd0429937997010100c010aac53f09870c537dcec5fa22`.

The reported fee is not a before/after balance measurement of the sender. Activation fee source, refusal text, and exact balance debits still require controlled experiments.

## Next probe sequence

1. **Reusable recorder and case manifest.** Preserve these artifacts for every request, including malformed bodies. For signed probes also retain the exact signed envelope, signer address, nonce, intended mutation, and before/after account queries. Keep private keys out of artifacts. Do not retry ambiguous submissions automatically; resolve them with read-only queries.
2. **Read-only contract matrix.** Capture `spotMeta`, `l2Book` aggregation (`nSigFigs`/`mantissa`), order lookup by oid/cloid, and fresh-account responses. Exercise all eight required queries with valid requests, missing fields, wrong types, invalid enum values, unknown assets, malformed JSON, and wrong content types. Record HTTP status, content type, and exact error bytes instead of assuming every malformed request returns 422.
3. **Controlled `sendAsset` probes.** Generate a second controlled testnet account. Snapshot both sides, send a small amount to the fresh recipient, then repeat to the now-existing recipient. Measure activation, fees, balances, roles, and both ledgers. Resubmit an identical signed envelope to capture reused-nonce behavior. Separately probe insufficient balances, invalid amounts/tokens/dexes, tampered signatures, and nonce-window boundaries. A signature made by another valid key may recover another signer rather than yield an invalid-signature error; observe the actual outcome.
4. **Controlled IOC probes.** Use metadata and current depth to choose bounded-size trades. Capture buy/sell fees in the received token, full/partial/no fills, minimum notional, size/price precision rejection or normalization, cloid lookup, and fills/status consistency. Avoid assuming the server rounds just because an SDK does. Live depth is not deterministic; record book snapshots and distinguish observations from simulator test controls.
5. **Implement and replay.** Build the simulator from the observed contract, with exact errors and deterministic account/order transitions. Replay the captured cases against it. Model funding modes, configurable depth, and injected faults as separate test controls, not Hyperliquid endpoints.

Signed experiments must use testnet signing. Never direct a signed probe at mainnet. Mainnet-specific differences need separate evidence before claiming parity.

## Not yet established

No malformed requests or signed actions have been exercised. Nonce reuse/consumption/window behavior, signature validation precedence, activation refusal text, IOC precision/matching/minimum/fees, large-number serialization, and ambiguous-submit recovery remain unmeasured. The funding transaction's signed envelope is not available from these ledger responses, so its nonce cannot be replay-tested from the ledger alone.
