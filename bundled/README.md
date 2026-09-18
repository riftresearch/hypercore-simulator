# Bundled captures

These are byte-for-byte copies of files in [hypercore-simulator-captures](https://github.com/riftresearch/hypercore-simulator-captures), compiled into the crate with `include_str!` so the library builds and runs without the evidence checkout.

| File | Source in the captures repository | What it is | SHA-256 |
| --- | --- | --- | --- |
| `spotMeta-mainnet.json` | `mainnet/spotMeta/response.body` | Public mainnet spotMeta, fetched 2026-09-18T20:23Z | `db22a67d410d083b95f2d4032a7115d68d6c5282dcd91d2248e7965992bbe725` |
| `spotMeta-testnet.json` | `testnet/readonly-contract/spotMeta.json` | Testnet spotMeta recorded by the read-only contract matrix | `126388d8cdf6a61dbd5c6f1fe66bf4c73bf0810a252002bfc461c2a331a14d8c` |
| `userFees-testnet.json` | `testnet/20260918T045515.026265Z-baseline/user-fees/response.body` | Testnet userFees baseline: the default fee schedule and exchange volume | `756046d432a4170e303da11de92e1a2fd6f211dadfc771ed0dfe122e6c30204e` |

The unit-test fixtures under `tests/fixtures/` are copies of `testnet/orders-multilevel-04/*/response.body`, `testnet/fill-limit-readonly/*/response.body` and `testnet/fill-limit-history/older-maker-fills/response.body`.
