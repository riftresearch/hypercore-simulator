//! Hyperliquid signing modeled on the official Python SDK's `utils/signing.py`.
//! L1 actions (`order`, `cancel`, `cancelByCloid`, `modify`, `batchModify`,
//! `scheduleCancel`) use the phantom Agent; `sendAsset` is user-signed EIP-712.
//! Errors describe local validation, not observed upstream error text/precedence.

use crate::{
    error::Result,
    wire::{
        Action, Address, CancelByCloidTarget, CancelTarget, Envelope, Grouping, ModifyEntry,
        OidOrCloid, OrderRequest, SendAsset, Word,
    },
};
use k256::ecdsa::{RecoveryId, Signature, VerifyingKey};
use serde::Serialize;
use sha3::{Digest, Keccak256};

const DOMAIN_TYPE: &str =
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)";
const AGENT_TYPE: &str = "Agent(string source,bytes32 connectionId)";
const SEND_ASSET_TYPE: &str = "HyperliquidTransaction:SendAsset(string hyperliquidChain,string destination,string sourceDex,string destinationDex,string token,string amount,string fromSubAccount,uint64 nonce)";

/// Recover the actual signer; recovery is not account authorization.
///
/// Changing an L1 action or its environment usually recovers a different valid
/// key rather than producing a cryptographic error. The caller must check the
/// returned address against its account state. HTTP object key order is not
/// signed: the server reconstructs the SDK's typed wire-field order.
pub fn recover_signer(envelope: &Envelope, is_mainnet: bool) -> Result<Address> {
    let digest = match &envelope.action {
        // User-signed payloads do not authenticate wrapper vault/expiry/nonce.
        Action::SendAsset(action) => send_asset_digest(action, is_mainnet)?,
        action => l1_digest(&pack(action)?, envelope, is_mainnet),
    };
    let signature = envelope.signature;
    if !matches!(signature.v, 0 | 1 | 27 | 28) {
        return Err("signature.v must be 0, 1, 27 or 28".into());
    }
    // from_scalars rejects zero and values >= the secp256k1 group order.
    let mut parsed = Signature::from_scalars(signature.r.0, signature.s.0)
        .map_err(|_| "signature r/s must be nonzero secp256k1 scalars")?;
    let mut recovery_id = RecoveryId::new(matches!(signature.v, 1 | 28), false);
    // k256 only verifies low-s signatures. Normalize a mathematically valid
    // high-s signature with its parity bit, preserving the recovered signer.
    if let Some(normalized) = parsed.normalize_s() {
        parsed = normalized;
        recovery_id = RecoveryId::new(!recovery_id.is_y_odd(), false);
    }
    let key = VerifyingKey::recover_from_prehash(&digest, &parsed, recovery_id)
        .map_err(|_| "signature public-key recovery failed")?;
    let encoded = key.to_encoded_point(false);
    let hash = keccak(&encoded.as_bytes()[1..]);
    let mut address = [0; 20];
    address.copy_from_slice(&hash[12..]);
    Ok(Address(address))
}

// The SDK's action dictionaries, in its key order. `builder` is never signed here.

#[derive(Serialize)]
struct SignedOrder<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    orders: &'a [OrderRequest],
    grouping: Grouping,
}

#[derive(Serialize)]
struct SignedCancel<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    cancels: &'a [CancelTarget],
}

#[derive(Serialize)]
struct SignedCancelByCloid<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    cancels: &'a [CancelByCloidTarget],
}

#[derive(Serialize)]
struct SignedModify<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    oid: OidOrCloid,
    order: &'a OrderRequest,
}

#[derive(Serialize)]
struct SignedBatchModify<'a> {
    #[serde(rename = "type")]
    kind: &'static str,
    modifies: &'a [ModifyEntry],
}

#[derive(Serialize)]
struct SignedScheduleCancel {
    #[serde(rename = "type")]
    kind: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    time: Option<u64>,
}

/// MessagePack the action exactly as the SDK does before hashing.
fn pack(action: &Action) -> Result<Vec<u8>> {
    let packed = match action {
        Action::Order(action) => rmp_serde::to_vec_named(&SignedOrder {
            kind: "order",
            orders: &action.orders,
            grouping: action.grouping.unwrap_or(Grouping::Na),
        }),
        Action::Cancel(action) => rmp_serde::to_vec_named(&SignedCancel {
            kind: "cancel",
            cancels: &action.cancels,
        }),
        Action::CancelByCloid(action) => rmp_serde::to_vec_named(&SignedCancelByCloid {
            kind: "cancelByCloid",
            cancels: &action.cancels,
        }),
        Action::Modify(action) => rmp_serde::to_vec_named(&SignedModify {
            kind: "modify",
            oid: action.oid,
            order: &action.order,
        }),
        Action::BatchModify(action) => rmp_serde::to_vec_named(&SignedBatchModify {
            kind: "batchModify",
            modifies: &action.modifies,
        }),
        Action::ScheduleCancel(action) => rmp_serde::to_vec_named(&SignedScheduleCancel {
            kind: "scheduleCancel",
            time: action.time,
        }),
        Action::SendAsset(_) => return Err("sendAsset is not an L1 action".into()),
    };
    packed.map_err(|error| format!("action MessagePack encoding failed: {error}").into())
}

fn l1_digest(packed: &[u8], envelope: &Envelope, is_mainnet: bool) -> [u8; 32] {
    let mut data = packed.to_vec();
    data.extend_from_slice(&envelope.nonce.to_be_bytes());
    match envelope.vault_address {
        None => data.push(0),
        Some(vault) => {
            data.push(1);
            data.extend_from_slice(&vault.0);
        }
    }
    if let Some(expiry) = envelope.expires_after {
        data.push(0);
        data.extend_from_slice(&expiry.to_be_bytes());
    }
    let agent_hash = hash_words(&[
        keccak(AGENT_TYPE.as_bytes()),
        keccak(if is_mainnet { b"a" } else { b"b" }),
        keccak(&data),
    ]);
    typed_digest(domain_hash("Exchange", uint_word(1337)), agent_hash)
}

fn send_asset_digest(action: &SendAsset, is_mainnet: bool) -> Result<[u8; 32]> {
    let expected = if is_mainnet { "Mainnet" } else { "Testnet" };
    if action.hyperliquid_chain.name() != expected {
        return Err(format!("hyperliquidChain must be {expected}").into());
    }
    // Addresses below have EIP-712 type string, not address: hash their
    // original text after shape validation.
    Address::parse(&action.destination).map_err(|_| "destination must be an address")?;
    if !action.from_sub_account.is_empty() {
        Address::parse(&action.from_sub_account)
            .map_err(|_| "fromSubAccount must be an address")?;
    }
    let action_hash = hash_words(&[
        keccak(SEND_ASSET_TYPE.as_bytes()),
        keccak(action.hyperliquid_chain.name().as_bytes()),
        keccak(action.destination.as_bytes()),
        keccak(action.source_dex.as_bytes()),
        keccak(action.destination_dex.as_bytes()),
        keccak(action.token.as_bytes()),
        keccak(action.amount.as_bytes()),
        keccak(action.from_sub_account.as_bytes()),
        uint_word(action.nonce),
    ]);
    // signatureChainId identifies the wallet's signing chain, not the
    // Hyperliquid environment. The SDK accepts arbitrary uint256 chain IDs.
    let Word(chain_id) = action.signature_chain_id;
    Ok(typed_digest(
        domain_hash("HyperliquidSignTransaction", chain_id),
        action_hash,
    ))
}

fn domain_hash(name: &str, chain_id: [u8; 32]) -> [u8; 32] {
    hash_words(&[
        keccak(DOMAIN_TYPE.as_bytes()),
        keccak(name.as_bytes()),
        keccak(b"1"),
        chain_id,
        [0; 32], // zero verifyingContract address, left-padded to one ABI word
    ])
}

fn typed_digest(domain: [u8; 32], message: [u8; 32]) -> [u8; 32] {
    let mut hash = Keccak256::new();
    hash.update([0x19, 0x01]);
    hash.update(domain);
    hash.update(message);
    hash.finalize().into()
}

pub fn keccak(bytes: &[u8]) -> [u8; 32] {
    Keccak256::digest(bytes).into()
}

fn hash_words(words: &[[u8; 32]]) -> [u8; 32] {
    let mut hash = Keccak256::new();
    for word in words {
        hash.update(word);
    }
    hash.finalize().into()
}

fn uint_word(value: u64) -> [u8; 32] {
    let mut word = [0; 32];
    word[24..].copy_from_slice(&value.to_be_bytes());
    word
}
