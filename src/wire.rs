//! Typed request surface. Shape validation happens once, during deserialization.
//!
//! Serde derive reproduces the measured parser: unknown fields are ignored unless
//! a struct denies them, a duplicated known field is a deserialization error, and
//! a duplicated ignored field is not.

use crate::{
    decimal::{self, Text},
    error::{Error, Result},
    fees::FeeState,
};
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{self, IgnoredAny, MapAccess, SeqAccess, Visitor, value::MapAccessDeserializer},
};
use serde_json::{Map, Value};
use std::{fmt, marker::PhantomData};

/// Deserialize a string without copying it, then parse it.
pub fn from_str<'de, D, T>(
    deserializer: D,
    parse: impl FnOnce(&str) -> Result<T>,
) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
{
    struct Parse<F>(F);
    impl<'de, F: FnOnce(&str) -> Result<T>, T> Visitor<'de> for Parse<F> {
        type Value = T;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a string")
        }

        fn visit_str<E: de::Error>(self, raw: &str) -> Result<T, E> {
            (self.0)(raw).map_err(E::custom)
        }
    }
    deserializer.deserialize_str(Parse(parse))
}

/// Deserialize `T` inside `Some`: an absent field defaults to `None`, while a
/// present field must be a `T`, so null is not accepted where `T` rejects it.
pub fn some<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<Option<T>, D::Error> {
    T::deserialize(deserializer).map(Some)
}

/// Deserialize a struct from a JSON object only. Serde derive would also accept
/// a positional array, which the measured parser rejects.
pub fn object<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<T, D::Error> {
    struct Only<T>(PhantomData<T>);
    impl<'de, T: Deserialize<'de>> Visitor<'de> for Only<T> {
        type Value = T;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("an object")
        }

        fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<T, A::Error> {
            T::deserialize(MapAccessDeserializer::new(map))
        }
    }
    deserializer.deserialize_map(Only(PhantomData))
}

/// A sequence whose every element must be a JSON object; see `object`.
pub fn objects<'de, D: Deserializer<'de>, T: Deserialize<'de>>(
    deserializer: D,
) -> Result<Vec<T>, D::Error> {
    struct Each<T>(PhantomData<T>);
    impl<'de, T: Deserialize<'de>> Visitor<'de> for Each<T> {
        type Value = Vec<T>;

        fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
            formatter.write_str("a sequence of objects")
        }

        fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Vec<T>, A::Error> {
            let mut items = Vec::with_capacity(sequence.size_hint().unwrap_or(0));
            while let Some(Object(item)) = sequence.next_element::<Object<T>>()? {
                items.push(item);
            }
            Ok(items)
        }
    }
    deserializer.deserialize_seq(Each(PhantomData))
}

/// A root value that must be a JSON object.
pub struct Object<T>(pub T);

impl<'de, T: Deserialize<'de>> Deserialize<'de> for Object<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        object(deserializer).map(Self)
    }
}

/// Fixed-width hex identifiers. Only a lowercase `0x` prefix is recognized,
/// as upstream rejects `0X`; `$required` says whether the prefix is mandatory.
macro_rules! hex_newtype {
    ($(#[$doc:meta])* $name:ident, $bytes:literal, $required:literal, $message:literal) => {
        $(#[$doc])*
        #[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
        pub struct $name(pub [u8; $bytes]);

        impl $name {
            pub fn parse(raw: &str) -> Result<Self> {
                let digits = match raw.strip_prefix("0x") {
                    Some(digits) => digits,
                    None if $required => return Err($message.into()),
                    None => raw,
                };
                let mut bytes = [0; $bytes];
                if digits.len() != $bytes * 2 || hex::decode_to_slice(digits, &mut bytes).is_err() {
                    return Err($message.into());
                }
                Ok(Self(bytes))
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                write!(f, "0x{}", hex::encode(self.0))
            }
        }

        impl fmt::Debug for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt::Display::fmt(self, f)
            }
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
                serializer.collect_str(self)
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
                from_str(deserializer, Self::parse)
            }
        }
    };
}

hex_newtype!(
    /// A 20-byte account address; parsing normalizes case.
    Address, 20, false, "Simulator: invalid address"
);
hex_newtype!(
    /// A 16-byte client order id; digits may be uppercase.
    Cloid, 16, true, "Simulator: invalid cloid"
);
hex_newtype!(
    /// A 32-byte transaction hash generated by the simulator.
    TxHash, 32, true, "Simulator: invalid hash"
);

impl Address {
    pub const ZERO: Self = Self([0; 20]);
}

/// A 256-bit ABI word written as 1 to 64 hex digits with an optional `0x`/`0X`
/// prefix, as the SDK's `to_hex(int)` emits short, possibly odd-length scalars.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Word(pub [u8; 32]);

impl Word {
    pub fn parse(raw: &str) -> Result<Self> {
        let digits = raw
            .strip_prefix("0x")
            .or_else(|| raw.strip_prefix("0X"))
            .unwrap_or(raw);
        if digits.is_empty() || digits.len() > 64 {
            return Err("hex word must contain 1 to 64 hexadecimal digits".into());
        }
        let mut word = [0; 32];
        for (index, byte) in digits.bytes().rev().enumerate() {
            let nibble = match byte {
                b'0'..=b'9' => byte - b'0',
                b'a'..=b'f' => byte - b'a' + 10,
                b'A'..=b'F' => byte - b'A' + 10,
                _ => return Err("hex word must be hexadecimal".into()),
            };
            word[31 - index / 2] |= nibble << (4 * (index % 2));
        }
        Ok(Self(word))
    }
}

impl<'de> Deserialize<'de> for Word {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        from_str(deserializer, Self::parse)
    }
}

/// Order prices and sizes: unsigned digits with at most eight decimals, small
/// enough to count in 1e-8 units. Serialization is the signed canonical form.
pub mod order_number {
    use super::*;

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        from_str(deserializer, |raw| {
            let digits = !raw.is_empty() && raw.bytes().all(|b| b.is_ascii_digit() || b == b'.');
            let number = digits
                .then(|| Decimal::from_str_exact(raw).ok())
                .flatten()
                .filter(|n| n.normalize().scale() <= 8)
                .filter(|n| {
                    n.checked_mul(Decimal::from(100_000_000u64))
                        .and_then(|n| n.to_u64())
                        .is_some()
                });
            number.ok_or_else(|| "invalid wire number".into())
        })
    }

    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.collect_str(&value.normalize())
    }
}

// ---------------------------------------------------------------------------
// /info

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type")]
pub enum Info {
    #[serde(rename = "spotMeta")]
    SpotMeta,
    #[serde(rename = "l2Book", rename_all = "camelCase")]
    L2Book {
        coin: String,
        #[serde(default)]
        n_sig_figs: Option<u8>,
        #[serde(default)]
        mantissa: Option<u8>,
    },
    #[serde(rename = "spotClearinghouseState")]
    ClearinghouseState { user: Address },
    /// The object form never reads `source`; only the positional form carries it.
    #[serde(rename = "preTransferCheck")]
    PreTransferCheck {
        user: Address,
        #[serde(skip)]
        source: Option<Address>,
    },
    #[serde(rename = "userRole")]
    UserRole { user: Address },
    #[serde(rename = "userFees")]
    UserFees { user: Address },
    #[serde(rename = "orderStatus")]
    OrderStatus { user: Address, oid: OrderLookup },
    #[serde(rename = "openOrders")]
    OpenOrders { user: Address },
    #[serde(rename = "frontendOpenOrders")]
    FrontendOpenOrders { user: Address },
    #[serde(rename = "historicalOrders")]
    HistoricalOrders { user: Address },
    #[serde(rename = "userFills", rename_all = "camelCase")]
    UserFills {
        user: Address,
        /// Optional, but null is not a boolean.
        #[serde(default, deserialize_with = "some")]
        aggregate_by_time: Option<bool>,
    },
    /// Measured in `nq-prior-history`, `fill-limit-history` and
    /// `independent-market-history`: inclusive bounds, oldest first.
    #[serde(rename = "userFillsByTime", rename_all = "camelCase")]
    UserFillsByTime {
        user: Address,
        start_time: u64,
        #[serde(default)]
        end_time: Option<u64>,
        #[serde(default, deserialize_with = "some")]
        aggregate_by_time: Option<bool>,
    },
    #[serde(rename = "userNonFundingLedgerUpdates", rename_all = "camelCase")]
    LedgerUpdates {
        user: Address,
        #[serde(default)]
        start_time: Option<u64>,
        #[serde(default)]
        end_time: Option<u64>,
    },
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(untagged)]
pub enum OrderLookup {
    Oid(u64),
    Cloid(Cloid),
}

/// An info query in object form, or the positional `[type, arg, ...]` form.
#[derive(Clone, Debug)]
pub struct InfoRequest(pub Info);

impl<'de> Deserialize<'de> for InfoRequest {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct Either;
        impl<'de> Visitor<'de> for Either {
            type Value = Info;

            fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter.write_str("an info query object or sequence")
            }

            fn visit_map<A: MapAccess<'de>>(self, map: A) -> Result<Info, A::Error> {
                Info::deserialize(MapAccessDeserializer::new(map))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut sequence: A) -> Result<Info, A::Error> {
                let mut items = Vec::new();
                while let Some(item) = sequence.next_element::<Value>()? {
                    items.push(item);
                }
                positional(items).map_err(de::Error::custom)
            }
        }
        deserializer.deserialize_any(Either).map(Self)
    }
}

fn positional(items: Vec<Value>) -> Result<Info> {
    let kind = items
        .first()
        .and_then(Value::as_str)
        .ok_or("missing info type")?;
    let address =
        |value: &Value| Address::deserialize(value).map_err(|e| Error::from(e.to_string()));
    if kind == "preTransferCheck" {
        let [_, user, source] = items.as_slice() else {
            return Err("invalid info sequence length".into());
        };
        let source = (!source.is_null()).then(|| address(source)).transpose()?;
        return Ok(Info::PreTransferCheck {
            user: address(user)?,
            source,
        });
    }
    let (fields, minimum): (&[&str], usize) = match kind {
        "spotMeta" => (&[], 0),
        "userRole"
        | "spotClearinghouseState"
        | "userFees"
        | "openOrders"
        | "frontendOpenOrders"
        | "historicalOrders" => (&["user"], 1),
        "orderStatus" => (&["user", "oid"], 2),
        "userFills" => (&["user", "aggregateByTime"], 1),
        "userFillsByTime" => (&["user", "startTime", "endTime", "aggregateByTime"], 2),
        "l2Book" => (&["coin", "nSigFigs", "mantissa"], 1),
        "userNonFundingLedgerUpdates" => (&["user", "startTime", "endTime"], 3),
        _ => return Err("unsupported info sequence".into()),
    };
    let arguments = &items[1..];
    if arguments.len() < minimum || arguments.len() > fields.len() {
        return Err("invalid info sequence length".into());
    }
    let mut object = Map::new();
    object.insert("type".into(), kind.into());
    for (field, value) in fields.iter().zip(arguments) {
        object.insert((*field).into(), value.clone());
    }
    Info::deserialize(Value::Object(object)).map_err(|e| e.to_string().into())
}

/// HTTP outcomes of an info query that are not a JSON document.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rejection {
    /// 422 with the deserialization message.
    Unprocessable,
    /// 500 with a JSON `null` body.
    Null,
}

// ---------------------------------------------------------------------------
// /exchange

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Envelope {
    #[serde(deserialize_with = "object")]
    pub action: Action,
    pub nonce: u64,
    #[serde(deserialize_with = "object")]
    pub signature: Signature,
    #[serde(default)]
    pub vault_address: Option<Address>,
    #[serde(default)]
    pub expires_after: Option<u64>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Signature {
    pub r: Word,
    pub s: Word,
    pub v: u8,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "type")]
pub enum Action {
    #[serde(rename = "order")]
    Order(OrderAction),
    #[serde(rename = "cancel")]
    Cancel(CancelAction),
    #[serde(rename = "cancelByCloid")]
    CancelByCloid(CancelByCloidAction),
    #[serde(rename = "modify")]
    Modify(ModifyAction),
    #[serde(rename = "batchModify")]
    BatchModify(BatchModifyAction),
    #[serde(rename = "scheduleCancel")]
    ScheduleCancel(ScheduleCancelAction),
    #[serde(rename = "sendAsset")]
    SendAsset(SendAsset),
    #[serde(rename = "sendToEvmWithData")]
    SendToEvmWithData(SendToEvmWithData),
}

impl Action {
    /// The nonce a user-signed action carries inside its signed payload.
    pub fn user_signed_nonce(&self) -> Option<u64> {
        match self {
            Self::SendAsset(action) => Some(action.nonce),
            Self::SendToEvmWithData(action) => Some(action.nonce),
            _ => None,
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelAction {
    #[serde(deserialize_with = "objects")]
    pub cancels: Vec<CancelTarget>,
}

/// `{a: asset, o: oid}`, in signed field order.
#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub struct CancelTarget {
    pub a: u32,
    pub o: u64,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelByCloidAction {
    #[serde(deserialize_with = "objects")]
    pub cancels: Vec<CancelByCloidTarget>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub struct CancelByCloidTarget {
    pub asset: u32,
    pub cloid: Cloid,
}

/// The order to replace, by oid or by its client id.
#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(untagged)]
pub enum OidOrCloid {
    Oid(u64),
    Cloid(Cloid),
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModifyAction {
    pub oid: OidOrCloid,
    #[serde(deserialize_with = "object")]
    pub order: OrderRequest,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BatchModifyAction {
    #[serde(deserialize_with = "objects")]
    pub modifies: Vec<ModifyEntry>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct ModifyEntry {
    pub oid: OidOrCloid,
    #[serde(deserialize_with = "object")]
    pub order: OrderRequest,
}

/// Arm (`time`) or clear (absent) the dead-man switch.
#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScheduleCancelAction {
    #[serde(default)]
    pub time: Option<u64>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderAction {
    #[serde(deserialize_with = "objects")]
    pub orders: Vec<OrderRequest>,
    #[serde(default)]
    pub grouping: Option<Grouping>,
    /// Any non-null builder is rejected downstream, so its shape is not modeled.
    #[serde(default)]
    pub builder: Option<IgnoredAny>,
}

/// One order in the SDK's wire field order, which is also its signed order.
#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct OrderRequest {
    pub a: u32,
    pub b: bool,
    #[serde(with = "order_number")]
    pub p: Decimal,
    #[serde(with = "order_number")]
    pub s: Decimal,
    #[serde(default)]
    pub r: bool,
    pub t: OrderType,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub c: Option<Cloid>,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum OrderType {
    Limit(#[serde(deserialize_with = "object")] Limit),
    Trigger(#[serde(deserialize_with = "object")] Trigger),
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
pub struct Limit {
    pub tif: Tif,
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Trigger {
    pub is_market: bool,
    #[serde(with = "order_number")]
    pub trigger_px: Decimal,
    pub tpsl: Tpsl,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
pub enum Tif {
    Alo,
    Ioc,
    Gtc,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Tpsl {
    Tp,
    Sl,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum Grouping {
    Na,
    NormalTpsl,
    PositionTpsl,
}

/// User-signed transfer. String fields keep their exact text: EIP-712 hashes
/// them verbatim, and the engine interprets them afterwards.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SendAsset {
    pub hyperliquid_chain: Chain,
    pub signature_chain_id: Word,
    pub destination: String,
    pub source_dex: String,
    pub destination_dex: String,
    pub token: String,
    pub amount: String,
    pub from_sub_account: String,
    pub nonce: u64,
}

/// User-signed spot withdrawal to an external EVM chain through HyperEVM.
/// String fields keep their exact text for EIP-712 hashing, as `SendAsset` does.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct SendToEvmWithData {
    pub hyperliquid_chain: Chain,
    pub signature_chain_id: Word,
    pub token: String,
    pub amount: String,
    pub source_dex: String,
    pub destination_recipient: String,
    pub address_encoding: String,
    pub destination_chain_id: u32,
    pub gas_limit: u64,
    pub data: String,
    pub nonce: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
pub enum Chain {
    Mainnet,
    Testnet,
}

impl Chain {
    pub fn name(self) -> &'static str {
        match self {
            Self::Mainnet => "Mainnet",
            Self::Testnet => "Testnet",
        }
    }
}

// ---------------------------------------------------------------------------
// /_test/* controls

#[derive(Clone, Debug)]
pub enum Control {
    Time(Time),
    Fault(FaultSpec),
    Reset,
    Upgrade(Upgrade),
    Account(AccountQuery),
    Fees(FeeContext),
    Dust(DustConfig),
    Fund(Funding),
    Book(BookSpec),
    EvmSends,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct Time {
    /// Null releases a fixed clock.
    pub now_ms: Option<u64>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct FaultSpec {
    pub kind: FaultKind,
    #[serde(default = "default_delay")]
    pub delay_ms: u64,
    #[serde(default)]
    pub message: Option<String>,
}

fn default_delay() -> u64 {
    1000
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FaultKind {
    Refuse,
    Delay,
    RateLimit,
    DropAfterCommit,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Upgrade {
    #[serde(default)]
    pub post_only: Option<bool>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct AccountQuery {
    pub address: Address,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct FeeContext {
    #[serde(default)]
    pub fees: Option<FeeState>,
    #[serde(default)]
    pub quote_token_indices: Option<Vec<u64>>,
    #[serde(default)]
    pub aligned_quote_token_indices: Option<Vec<u64>>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct DustConfig {
    #[serde(default)]
    pub interval_ms: Option<u64>,
    /// Absent keeps the policy, null clears any override, a value overrides.
    #[serde(default, deserialize_with = "some")]
    pub max_notional: Option<Option<Text>>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Funding {
    pub address: Address,
    pub token: String,
    #[serde(with = "decimal::text")]
    pub amount: Decimal,
    pub mode: FundingMode,
    #[serde(default)]
    pub sender: Option<Address>,
    #[serde(default)]
    pub entry_ntl: Option<Text>,
    #[serde(default)]
    pub user_has_sent_tx: Option<bool>,
    #[serde(default, rename = "serialize_f64")]
    pub serialize_f64: Option<bool>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum FundingMode {
    Transfer,
    Deposit,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BookSpec {
    pub coin: String,
    #[serde(default)]
    pub mark_px: Option<Text>,
    #[serde(default)]
    pub mid: Option<Text>,
    #[serde(default)]
    pub depth: Option<Text>,
    #[serde(default)]
    pub bids: Option<Vec<LevelSpec>>,
    #[serde(default)]
    pub asks: Option<Vec<LevelSpec>>,
}

#[derive(Clone, Copy, Debug, Deserialize)]
pub struct LevelSpec {
    #[serde(with = "decimal::text")]
    pub px: Decimal,
    #[serde(with = "decimal::text")]
    pub sz: Decimal,
    #[serde(default = "one")]
    pub n: u64,
}

fn one() -> u64 {
    1
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn typed_envelope_matches_the_measured_shape_rules() {
        let base = serde_json::json!({
            "action": {"type": "order", "grouping": "na", "orders": [{
                "a": 10000, "b": true, "p": "1.50", "s": "2", "r": false,
                "t": {"limit": {"tif": "Ioc"}}, "c": format!("0x{}", "1".repeat(32))
            }]},
            "nonce": 1, "signature": {"r": "0x1", "s": "0x2", "v": 27}
        });
        let accepts = |mutate: fn(&mut Value)| {
            let mut value = base.clone();
            mutate(&mut value);
            serde_json::from_value::<Envelope>(value).is_ok()
        };
        assert!(accepts(|_| {}));
        assert!(accepts(|v| {
            v["action"]["orders"][0]
                .as_object_mut()
                .unwrap()
                .remove("r");
        }));
        assert!(accepts(|v| {
            v["action"].as_object_mut().unwrap().remove("grouping");
        }));
        assert!(accepts(|v| v["action"]["orders"][0]["c"] = Value::Null));
        assert!(accepts(|v| v["signature"]["r"] = "0".into()));
        assert!(accepts(|v| v["action"]["orders"][0]["extra"] = 1.into()));
        assert!(!accepts(|v| v["extra"] = 1.into()));
        assert!(!accepts(|v| v["action"]["extra"] = 1.into()));
        assert!(!accepts(|v| v["action"]["orders"][0]["p"] = "-1".into()));
        assert!(!accepts(|v| v["action"]["orders"][0]["p"] = "1e1".into()));
        assert!(!accepts(
            |v| v["action"]["orders"][0]["s"] = "1.000000001".into()
        ));
        assert!(!accepts(
            |v| v["action"]["orders"][0]["a"] = (1u64 << 32).into()
        ));
        assert!(!accepts(|v| v["action"]["orders"][0]["c"] = "0x12".into()));
        assert!(!accepts(
            |v| v["action"]["orders"][0]["t"]["limit"]["tif"] = "IOC".into()
        ));
        assert!(!accepts(|v| {
            v["action"]["orders"][0]["t"]["trigger"] =
                serde_json::json!({"isMarket": true, "triggerPx": "5", "tpsl": "tp"});
        }));
        assert!(!accepts(|v| v["signature"]["v"] = 256.into()));
        assert!(!accepts(|v| v["signature"]["r"] = "".into()));
        assert!(!accepts(|v| v["vaultAddress"] = Value::Array(vec![])));
        // Objects are never accepted positionally.
        assert!(!accepts(
            |v| v["signature"] = serde_json::json!(["0x1", "0x2", 27])
        ));
        assert!(!accepts(
            |v| v["action"]["orders"][0] = serde_json::json!([10000, true, "1", "1"])
        ));
        assert!(!accepts(
            |v| v["action"]["orders"][0]["t"] = serde_json::json!({"limit": ["Ioc"]})
        ));
        let positional =
            serde_json::json!([base["action"], 0, {"r": "0x0", "s": "0x0", "v": 27}, null, null]);
        assert!(serde_json::from_value::<Envelope>(positional.clone()).is_ok());
        assert!(serde_json::from_value::<Object<Envelope>>(positional).is_err());
    }

    #[test]
    fn optional_booleans_reject_null() {
        let user = "0x1111111111111111111111111111111111111111";
        let query = |aggregate: Value| {
            serde_json::from_value::<InfoRequest>(serde_json::json!({
                "type": "userFills", "user": user, "aggregateByTime": aggregate
            }))
            .is_ok()
        };
        assert!(query(Value::Bool(true)));
        assert!(!query(Value::Null));
        assert!(!query(1.into()));
    }

    #[test]
    fn duplicate_known_fields_are_rejected_and_ignored_duplicates_are_not() {
        let known = r#"{"type":"userRole","user":"0x1111111111111111111111111111111111111111","user":"0x2222222222222222222222222222222222222222"}"#;
        assert!(serde_json::from_str::<InfoRequest>(known).is_err());
        let ignored = r#"{"type":"userRole","user":"0x1111111111111111111111111111111111111111","x":{"a":1,"a":2},"x":3}"#;
        assert!(serde_json::from_str::<InfoRequest>(ignored).is_ok());
        let doubled_type = r#"{"type":"spotMeta","type":"spotMeta"}"#;
        assert!(serde_json::from_str::<InfoRequest>(doubled_type).is_err());
    }

    #[test]
    fn positional_info_queries_map_to_fields() {
        let query: InfoRequest = serde_json::from_str(
            r#"["preTransferCheck","0x1111111111111111111111111111111111111111",null]"#,
        )
        .unwrap();
        assert!(matches!(
            query.0,
            Info::PreTransferCheck { source: None, .. }
        ));
        let query: InfoRequest = serde_json::from_str(r#"["l2Book","PURR/USDC",5,2]"#).unwrap();
        assert!(matches!(
            query.0,
            Info::L2Book {
                n_sig_figs: Some(5),
                mantissa: Some(2),
                ..
            }
        ));
        assert!(serde_json::from_str::<InfoRequest>(r#"["orderStatus","0x11"]"#).is_err());
        assert!(serde_json::from_str::<InfoRequest>(r#"[42]"#).is_err());
    }

    #[test]
    fn address_parsing_follows_upstream_case_rules() {
        let lower = "0x111122223333444455556666777788889999aaaa";
        assert_eq!(Address::parse(lower).unwrap().to_string(), lower);
        assert_eq!(
            Address::parse(&lower.to_uppercase()[2..])
                .unwrap()
                .to_string(),
            lower
        );
        assert_eq!(
            Address::parse(&("0x".to_owned() + &lower[2..].to_uppercase()))
                .unwrap()
                .to_string(),
            lower
        );
        assert!(Address::parse(&("0X".to_owned() + &lower[2..])).is_err());
        assert!(Address::parse(&lower[..41]).is_err());
    }

    #[test]
    fn signed_order_numbers_use_the_sdk_canonical_form() {
        for (raw, canonical) in [
            ("1.50", "1.5"),
            ("0.10", "0.1"),
            ("00012.3400", "12.34"),
            ("100.0", "100"),
            ("10", "10"),
            ("0", "0"),
            ("000", "0"),
            ("0.000", "0"),
            (".5", "0.5"),
            ("5.", "5"),
        ] {
            let order: OrderRequest = serde_json::from_value(serde_json::json!({
                "a": 1, "b": true, "p": raw, "s": raw, "t": {"limit": {"tif": "Gtc"}}
            }))
            .unwrap_or_else(|e| panic!("{raw}: {e}"));
            let packed = rmp_serde::to_vec_named(&order).unwrap();
            let round: serde_json::Value = rmp_serde::from_slice(&packed).unwrap();
            assert_eq!(round["p"], canonical, "{raw}");
        }
    }
}
