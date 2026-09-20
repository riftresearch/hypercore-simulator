//! Simulator state and its three entry points: info queries, signed exchange
//! actions and test controls. One thread owns an `Engine`; see `actor`.

mod account;
mod book;
mod cancel;
mod dust;
mod order;
mod transfer;

pub use account::{Fill, aggregate_fills};
pub use cancel::CancelReply;
pub use order::OrderReply;

use crate::{
    decimal::{self, ONE, ZERO, div, mul, wire},
    error::{Error, Result},
    fees::{FeeResponse, FeeState},
    wire::{Action, Address, Cloid, Control, Envelope, Info, OrderLookup, Rejection, TxHash},
};
use account::{Account, LedgerEntry, OrderDetail, OrderRecord, OrderState, PairIndex, TokenIndex};
use book::{Book, L2Book};
use dust::{DustPolicy, DustReply};
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde::{Deserialize, Serialize};
use serde_json::value::RawValue;
use sha3::{Digest, Keccak256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::Arc,
};
pub use transfer::EvmSend;
use transfer::FundReply;

/// Captured `spotMeta` documents bundled as each network's default metadata;
/// `bundled/README.md` records their provenance.
pub const MAINNET_SPOT_META: &str = include_str!("../../bundled/spotMeta-mainnet.json");
pub const TESTNET_SPOT_META: &str = include_str!("../../bundled/spotMeta-testnet.json");

const FEES: &str = include_str!("../../bundled/userFees-testnet.json");

pub struct Token {
    pub index: TokenIndex,
    pub name: Arc<str>,
    pub id: String,
    pub sz_decimals: u32,
    pub wei_decimals: u32,
    pub canonical: bool,
}

pub struct Pair {
    pub index: PairIndex,
    pub name: Arc<str>,
    pub base: TokenIndex,
    pub quote: TokenIndex,
}

#[derive(Deserialize)]
struct MetaDoc {
    tokens: Vec<TokenMeta>,
    universe: Vec<PairMeta>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct TokenMeta {
    index: u64,
    name: String,
    token_id: String,
    sz_decimals: u32,
    wei_decimals: u32,
    #[serde(default)]
    is_canonical: bool,
}

#[derive(Deserialize)]
struct PairMeta {
    index: u64,
    name: String,
    tokens: [u64; 2],
}

pub struct Engine {
    /// The `spotMeta` document exactly as supplied, echoed verbatim.
    meta: Box<RawValue>,
    tokens: BTreeMap<TokenIndex, Token>,
    pairs: BTreeMap<PairIndex, Pair>,
    pair_names: HashMap<Arc<str>, PairIndex>,
    usdc: TokenIndex,
    /// Each base token's lowest-index USDC-quoted market, for valuation and dust.
    usdc_pairs: BTreeMap<TokenIndex, PairIndex>,
    quote_tokens: BTreeSet<TokenIndex>,
    aligned_quote_tokens: BTreeSet<TokenIndex>,
    accounts: BTreeMap<Address, Account>,
    books: BTreeMap<PairIndex, Book>,
    /// Open stop and take-profit orders by market, as (owner, oid).
    triggers: BTreeMap<PairIndex, Vec<(Address, u64)>>,
    mark_prices: BTreeMap<PairIndex, Decimal>,
    dust: DustPolicy,
    upgrade_post_only: bool,
    fees: FeeState,
    default_fees: FeeState,
    next_oid: u64,
    next_tid: u64,
    /// Spot withdrawals to external EVM chains, in submission order, for the
    /// `/_test/evm_sends` control that a bridge mock drains.
    evm_sends: Vec<EvmSend>,
}

impl Engine {
    pub fn new(meta_json: &str) -> Result<Self> {
        let meta: MetaDoc = serde_json::from_str(meta_json).map_err(|e| e.to_string())?;
        let mut tokens = BTreeMap::new();
        for item in meta.tokens {
            if item.sz_decimals > 8 || item.wei_decimals > 28 {
                return Err("unsupported metadata precision".into());
            }
            let index = TokenIndex(item.index);
            let token = Token {
                index,
                name: item.name.into(),
                id: item.token_id,
                sz_decimals: item.sz_decimals,
                wei_decimals: item.wei_decimals,
                canonical: item.is_canonical,
            };
            if tokens.insert(index, token).is_some() {
                return Err("duplicate token index".into());
            }
        }
        let mut pairs = BTreeMap::new();
        for item in meta.universe {
            let [base, quote] = item.tokens.map(TokenIndex);
            if base == quote || !tokens.contains_key(&base) || !tokens.contains_key(&quote) {
                return Err("invalid pair tokens".into());
            }
            let index = PairIndex(item.index);
            let pair = Pair {
                index,
                name: item.name.into(),
                base,
                quote,
            };
            if pairs.insert(index, pair).is_some() {
                return Err("duplicate pair index".into());
            }
        }
        let usdc = tokens
            .values()
            .find(|t| &*t.name == "USDC")
            .map(|t| t.index)
            .ok_or("metadata must include USDC")?;
        let pair_names = pairs.values().map(|p| (p.name.clone(), p.index)).collect();
        let mut usdc_pairs = BTreeMap::new();
        for pair in pairs.values().filter(|p| &*tokens[&p.quote].name == "USDC") {
            usdc_pairs.entry(pair.base).or_insert(pair.index);
        }
        let fees = FeeState::parse(FEES)?;
        let mut engine = Self {
            meta: serde_json::from_str(meta_json).map_err(|e| e.to_string())?,
            tokens,
            pairs,
            pair_names,
            usdc,
            usdc_pairs,
            quote_tokens: BTreeSet::new(),
            aligned_quote_tokens: BTreeSet::new(),
            accounts: BTreeMap::new(),
            books: BTreeMap::new(),
            triggers: BTreeMap::new(),
            mark_prices: BTreeMap::new(),
            dust: DustPolicy::default(),
            upgrade_post_only: false,
            default_fees: fees.clone(),
            fees,
            next_oid: 1,
            next_tid: 1,
            evm_sends: Vec::new(),
        };
        engine.quote_tokens = engine.default_quote_tokens();
        Ok(engine)
    }

    /// Every quote token in the universe plus every token named USDC.
    fn default_quote_tokens(&self) -> BTreeSet<TokenIndex> {
        let usdc = self
            .tokens
            .values()
            .filter(|t| &*t.name == "USDC")
            .map(|t| t.index);
        self.pairs.values().map(|p| p.quote).chain(usdc).collect()
    }

    fn reset(&mut self) {
        self.accounts.clear();
        self.books.clear();
        self.triggers.clear();
        self.mark_prices.clear();
        self.dust.reset();
        self.upgrade_post_only = false;
        self.next_oid = 1;
        self.next_tid = 1;
        self.evm_sends.clear();
        self.fees = self.default_fees.clone();
        self.quote_tokens = self.default_quote_tokens();
        self.aligned_quote_tokens.clear();
    }

    fn token(&self, index: TokenIndex) -> &Token {
        &self.tokens[&index]
    }

    /// Resolve `NAME:0xID`, a name (canonical only when strict), or when not
    /// strict a numeric index or a bare token id.
    fn token_by_name(&self, name: &str, strict: bool) -> Result<&Token> {
        let numeric = name.parse::<u64>().ok().filter(|n| n.to_string() == name);
        self.tokens
            .values()
            .find(|t| {
                name.split_once(':').is_some_and(|(symbol, id)| {
                    symbol == &*t.name && id.eq_ignore_ascii_case(&t.id)
                }) || (&*t.name == name && (!strict || t.canonical))
                    || (!strict && (numeric == Some(t.index.0) || t.id.eq_ignore_ascii_case(name)))
            })
            .ok_or_else(|| "Simulator: unknown spot token".into())
    }

    fn pair_by_name(&self, name: &str) -> Option<&Pair> {
        self.pair_names.get(name).map(|index| &self.pairs[index])
    }

    /// The mark price if one was set, else the current book mid.
    fn reference_price(&self, pair: PairIndex) -> Result<Option<Decimal>> {
        if let Some(price) = self.mark_prices.get(&pair) {
            return Ok(Some(*price));
        }
        self.books.get(&pair).map_or(Ok(None), Book::mid)
    }

    fn usdc_value(&self, token: &Token, quantity: Decimal) -> Result<Decimal> {
        if &*token.name == "USDC" {
            return Ok(quantity);
        }
        let Some(pair) = self.usdc_pairs.get(&token.index) else {
            return Ok(ZERO);
        };
        let price = self.reference_price(*pair)?.unwrap_or(ZERO);
        mul(price, quantity)
    }

    fn balances(&self, account: Option<&Account>) -> Balances<'_> {
        let rows = account.into_iter().flat_map(|account| {
            account.balances.iter().map(|(index, balance)| {
                let total = if account.serialize_f64 {
                    // This opt-in wire quirk never affects arithmetic or internal state.
                    balance
                        .total
                        .to_string()
                        .parse::<f64>()
                        .map_or_else(|_| wire(balance.total), |v| format!("{v:?}"))
                } else {
                    wire(balance.total)
                };
                BalanceRow {
                    coin: &self.tokens[index].name,
                    token: *index,
                    total,
                    hold: wire(balance.hold),
                    entry_ntl: wire(balance.entry),
                }
            })
        });
        Balances {
            balances: rows.collect(),
        }
    }

    pub fn info(&self, query: &Info, now_ms: u64) -> Result<InfoReply<'_>, Rejection> {
        let account = |user: &Address| self.accounts.get(user);
        Ok(match query {
            Info::SpotMeta => InfoReply::Meta(&self.meta),
            Info::L2Book {
                coin,
                n_sig_figs,
                mantissa,
            } => {
                let sig = *n_sig_figs;
                if sig.is_some_and(|n| !(2..=5).contains(&n))
                    || mantissa.is_some_and(|m| sig != Some(5) || ![2, 5].contains(&m))
                {
                    return Err(Rejection::Null);
                }
                match self.pair_by_name(coin) {
                    None => InfoReply::Book(None),
                    Some(pair) => {
                        let mantissa = mantissa.map_or(1, u64::from);
                        InfoReply::Book(Some(self.l2(
                            pair,
                            sig.map(u32::from),
                            mantissa,
                            now_ms,
                        )?))
                    }
                }
            }
            Info::ClearinghouseState { user } => InfoReply::Balances(self.balances(account(user))),
            Info::PreTransferCheck { user, source } => {
                let account = account(user);
                InfoReply::PreTransfer {
                    is_sanctioned: false,
                    user_exists: account.is_some(),
                    fee: if account.is_none() && source.is_some() {
                        ONE
                    } else {
                        ZERO
                    },
                    user_has_sent_tx: account.is_some_and(|a| a.sent),
                }
            }
            Info::UserRole { user } => InfoReply::Role {
                role: if account(user).is_some() {
                    "user"
                } else {
                    "missing"
                },
            },
            Info::UserFees { user } => {
                let account = account(user);
                InfoReply::Fees(self.fees.response(
                    account.map(|a| &a.daily_volume),
                    account.map(|a| &a.daily_add_volume),
                    now_ms,
                )?)
            }
            Info::OrderStatus { user, oid } => {
                let account = account(user);
                let oid = match oid {
                    OrderLookup::Oid(oid) => Some(*oid),
                    OrderLookup::Cloid(cloid) => account.and_then(|a| a.cloids.get(cloid).copied()),
                };
                match oid.and_then(|oid| account?.orders.get(&oid)) {
                    Some(record) => InfoReply::Order(record),
                    None => InfoReply::UnknownOid {
                        status: "unknownOid",
                    },
                }
            }
            Info::OpenOrders { user } => InfoReply::OpenOrders(
                account(user)
                    .into_iter()
                    .flat_map(Account::open_orders)
                    .map(|(_, record)| {
                        let detail = &record.order.order;
                        OpenOrderRow {
                            coin: &detail.coin,
                            limit_px: detail.limit_px,
                            oid: detail.oid,
                            side: detail.side,
                            sz: detail.sz,
                            timestamp: detail.timestamp,
                            cloid: detail.cloid,
                        }
                    })
                    .collect(),
            ),
            Info::FrontendOpenOrders { user } => InfoReply::FrontendOpenOrders(
                account(user)
                    .into_iter()
                    .flat_map(Account::open_orders)
                    .map(|(_, record)| &record.order.order)
                    .collect(),
            ),
            Info::HistoricalOrders { user } => InfoReply::HistoricalOrders(
                account(user)
                    .into_iter()
                    .flat_map(|a| a.orders.values().rev())
                    .take(2000)
                    .map(|record| &record.order)
                    .collect(),
            ),
            Info::UserFills {
                user,
                aggregate_by_time,
            } => match account(user) {
                None => InfoReply::Fills(Vec::new()),
                Some(account) if *aggregate_by_time == Some(true) => {
                    InfoReply::Aggregated(aggregate_fills(&account.fills)?)
                }
                Some(account) => InfoReply::Fills(account.fills.iter().rev().take(2000).collect()),
            },
            Info::UserFillsByTime {
                user,
                start_time,
                end_time,
                aggregate_by_time,
            } => {
                let end = end_time.unwrap_or(now_ms);
                let mut window: Vec<&Fill> = account(user)
                    .into_iter()
                    .flat_map(|account| account.fills.iter())
                    .filter(|fill| fill.time >= *start_time && fill.time <= end)
                    .collect();
                // Oldest first, as captured; the cap keeps the most recent 2000.
                window.sort_by_key(|fill| fill.time);
                if *aggregate_by_time == Some(true) {
                    let fills: Vec<Fill> = window.into_iter().cloned().collect();
                    let mut groups = aggregate_fills(&fills)?;
                    groups.reverse();
                    InfoReply::Aggregated(groups)
                } else {
                    let excess = window.len().saturating_sub(2000);
                    InfoReply::Fills(window.split_off(excess))
                }
            }
            Info::LedgerUpdates {
                user,
                start_time,
                end_time,
            } => {
                if end_time.is_some() && start_time.is_none() {
                    return Err(Rejection::Null);
                }
                if let Some(start) = *start_time
                    && i64::try_from(start)
                        .ok()
                        .and_then(chrono::DateTime::from_timestamp_millis)
                        .is_none()
                {
                    return Err(Rejection::Null);
                }
                let (start, end) = (start_time.unwrap_or(0), end_time.unwrap_or(now_ms));
                let entries = account(user).into_iter().flat_map(|account| {
                    account
                        .ledger
                        .iter()
                        .filter(move |entry| entry.time >= start && entry.time <= end)
                        .take(500)
                });
                InfoReply::Ledger(entries.collect())
            }
        })
    }

    pub fn exchange(
        &mut self,
        envelope: &Envelope,
        raw: &[u8],
        signer: Address,
        now_ms: u64,
    ) -> ExchangeReply {
        match self.exchange_inner(envelope, raw, signer, now_ms) {
            Ok(response) => ExchangeReply::Ok(response),
            Err(error) => ExchangeReply::Err(error),
        }
    }

    fn exchange_inner(
        &mut self,
        envelope: &Envelope,
        raw: &[u8],
        signer: Address,
        now_ms: u64,
    ) -> Result<ExchangeOk> {
        if envelope.vault_address.is_some() {
            return Err("Vault may not perform this action.".into());
        }
        if let Some(nonce) = envelope.action.user_signed_nonce() {
            if envelope.expires_after.is_some() {
                return Err("Action does not support expires_after".into());
            }
            if nonce != envelope.nonce {
                return Err("Nonce mismatch.".into());
            }
        }
        if !self.accounts.contains_key(&signer) {
            return Err(match envelope.action {
                Action::SendAsset(_) | Action::SendToEvmWithData(_) => {
                    format!("Must deposit before performing actions. User: {signer}")
                }
                _ => format!("User or API Wallet {signer} does not exist."),
            }
            .into());
        }
        self.admit_nonce(signer, envelope.nonce, now_ms)?;
        if envelope
            .expires_after
            .is_some_and(|expires| expires < now_ms)
        {
            return Err("Action already expired".into());
        }
        Ok(match &envelope.action {
            Action::Order(action) => ExchangeOk::Order {
                data: OrderData {
                    statuses: self.order_action(action, signer, now_ms, raw)?,
                },
            },
            Action::Cancel(action) => ExchangeOk::Cancel {
                data: CancelData {
                    statuses: self.cancel_action(action, signer, now_ms),
                },
            },
            Action::CancelByCloid(action) => ExchangeOk::Cancel {
                data: CancelData {
                    statuses: self.cancel_by_cloid_action(action, signer, now_ms),
                },
            },
            Action::Modify(action) => {
                self.modify_action(action, signer, now_ms, raw)?;
                ExchangeOk::Default
            }
            Action::BatchModify(action) => ExchangeOk::Order {
                data: OrderData {
                    statuses: self.batch_modify_action(action, signer, now_ms, raw),
                },
            },
            Action::ScheduleCancel(action) => {
                self.schedule_cancel_action(action, signer, now_ms)?;
                ExchangeOk::Default
            }
            Action::SendAsset(action) => {
                self.send_asset(action, signer, envelope.nonce, now_ms, raw)?;
                ExchangeOk::Default
            }
            Action::SendToEvmWithData(action) => {
                self.send_to_evm(action, signer, envelope.nonce, now_ms, raw)?;
                ExchangeOk::Default
            }
        })
    }

    /// Enforce the measured nonce window, replay protection and the sent flag.
    fn admit_nonce(&mut self, signer: Address, nonce: u64, now_ms: u64) -> Result<()> {
        let oldest = i128::from(now_ms) - 172_800_000 + 120_000;
        let newest = i128::from(now_ms) + 86_400_000 - 120_000;
        if i128::from(nonce) <= oldest {
            return Err(format!("Invalid nonce: nonce too low {nonce} < {oldest}").into());
        }
        if i128::from(nonce) >= newest {
            return Err(format!("Invalid nonce: nonce too high {nonce} > {newest}").into());
        }
        let account = self
            .accounts
            .get_mut(&signer)
            .ok_or("Simulator: missing signer")?;
        while account
            .nonces
            .first()
            .is_some_and(|seen| i128::from(*seen) <= oldest)
        {
            account.nonces.pop_first();
        }
        if account.nonces.contains(&nonce) {
            return Err(format!("Invalid nonce: duplicate nonce {nonce}").into());
        }
        if let Some(floor) = account
            .nonces
            .iter()
            .rev()
            .nth(99)
            .filter(|floor| nonce <= **floor)
        {
            return Err(format!("Invalid nonce: nonce too low {nonce} < {floor}").into());
        }
        account.nonces.insert(nonce);
        account.sent = true;
        Ok(())
    }

    pub fn control(
        &mut self,
        control: Control,
        raw: &[u8],
        now_ms: u64,
    ) -> Result<ControlReply<'_>> {
        Ok(match control {
            Control::Reset => {
                self.reset();
                ControlReply::Ok { ok: true }
            }
            Control::Upgrade(upgrade) => {
                if let Some(post_only) = upgrade.post_only {
                    self.upgrade_post_only = post_only;
                }
                ControlReply::Upgrade {
                    post_only: self.upgrade_post_only,
                }
            }
            Control::Account(query) => {
                ControlReply::Account(match self.accounts.get(&query.address) {
                    None => AccountReply::Missing {
                        address: query.address,
                        exists: false,
                    },
                    Some(account) => AccountReply::Present {
                        address: query.address,
                        exists: true,
                        user_has_sent_tx: account.sent,
                        serialize_f64: account.serialize_f64,
                        state: self.balances(Some(account)),
                        nonces: &account.nonces,
                        fills: &account.fills,
                        ledger: &account.ledger,
                        orders: &account.orders,
                        cloids: &account.cloids,
                    },
                })
            }
            Control::Fees(context) => {
                let indices =
                    |key: &str, values: Option<Vec<u64>>| -> Result<Option<BTreeSet<TokenIndex>>> {
                        values
                            .map(|values| {
                                values
                                    .into_iter()
                                    .map(TokenIndex)
                                    .map(|index| {
                                        self.tokens
                                            .contains_key(&index)
                                            .then_some(index)
                                            .ok_or_else(|| {
                                                format!(
                                                "Simulator: {key} must contain known token indices"
                                            )
                                            .into()
                                            })
                                    })
                                    .collect()
                            })
                            .transpose()
                    };
                let quote_tokens = indices("quoteTokenIndices", context.quote_token_indices)?;
                let aligned = indices(
                    "alignedQuoteTokenIndices",
                    context.aligned_quote_token_indices,
                )?;
                if context.fees.is_none() && quote_tokens.is_none() && aligned.is_none() {
                    return Err("Simulator: fees or quote context are required".into());
                }
                if let Some(fees) = context.fees {
                    self.fees = fees;
                }
                if let Some(indices) = quote_tokens {
                    self.quote_tokens = indices;
                }
                if let Some(indices) = aligned {
                    self.aligned_quote_tokens = indices;
                }
                ControlReply::Ok { ok: true }
            }
            Control::Dust(config) => ControlReply::Dust(self.configure_dust(&config)?),
            Control::EvmSends => ControlReply::EvmSends(&self.evm_sends),
            Control::Fund(funding) => ControlReply::Fund(self.fund(&funding, raw, now_ms)?),
            Control::Book(spec) => ControlReply::Book(self.set_book(&spec, now_ms)?),
            Control::Time(_) | Control::Fault(_) => {
                return Err("Simulator: unknown control endpoint".into());
            }
        })
    }
}

/// A deterministic transaction hash: the request bytes plus a per-effect discriminator.
pub fn tx_hash(raw: &[u8], discriminator: u64) -> TxHash {
    let mut hasher = Keccak256::new();
    hasher.update(raw);
    hasher.update(discriminator.to_be_bytes());
    TxHash(hasher.finalize().into())
}

fn quote_units(decimals: u32) -> Result<Decimal> {
    Ok(Decimal::from(
        10_u64
            .checked_pow(decimals)
            .ok_or("Simulator: quote precision out of range")?,
    ))
}

/// Preserve the exchange's quote-wei floating-point fee quantization.
fn quote_fee(cost: Decimal, units: Decimal, rate: f64) -> Result<Decimal> {
    let fee_units = (mul(cost, units)?
        .to_f64()
        .ok_or("Simulator: fee notional out of range")?
        * rate)
        .floor();
    div(
        Decimal::from_f64_retain(fee_units).ok_or("Simulator: fee out of range")?,
        units,
    )
}

impl From<Error> for Rejection {
    fn from(_: Error) -> Self {
        Self::Unprocessable
    }
}

// ---------------------------------------------------------------------------
// Replies. Each borrows engine state and is serialized before the next command.

#[derive(Serialize)]
pub struct Balances<'a> {
    balances: Vec<BalanceRow<'a>>,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct BalanceRow<'a> {
    coin: &'a str,
    token: TokenIndex,
    total: String,
    hold: String,
    entry_ntl: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OpenOrderRow<'a> {
    coin: &'a str,
    #[serde(with = "decimal::text")]
    limit_px: Decimal,
    oid: u64,
    side: account::Side,
    #[serde(with = "decimal::text")]
    sz: Decimal,
    timestamp: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    cloid: Option<Cloid>,
}

#[derive(Serialize)]
#[serde(untagged)]
pub enum InfoReply<'a> {
    Meta(&'a RawValue),
    Book(Option<L2Book<'a>>),
    Balances(Balances<'a>),
    #[serde(rename_all = "camelCase")]
    PreTransfer {
        is_sanctioned: bool,
        user_exists: bool,
        #[serde(with = "decimal::text")]
        fee: Decimal,
        user_has_sent_tx: bool,
    },
    Role {
        role: &'static str,
    },
    Fees(FeeResponse<'a>),
    Order(&'a OrderRecord),
    UnknownOid {
        status: &'static str,
    },
    OpenOrders(Vec<OpenOrderRow<'a>>),
    FrontendOpenOrders(Vec<&'a OrderDetail>),
    HistoricalOrders(Vec<&'a OrderState>),
    Fills(Vec<&'a Fill>),
    Aggregated(Vec<Fill>),
    Ledger(Vec<&'a LedgerEntry>),
}

#[derive(Serialize)]
#[serde(tag = "status", content = "response", rename_all = "lowercase")]
pub enum ExchangeReply {
    Ok(ExchangeOk),
    Err(Error),
}

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "lowercase")]
pub enum ExchangeOk {
    Order { data: OrderData },
    Cancel { data: CancelData },
    Default,
}

#[derive(Serialize)]
pub struct OrderData {
    pub statuses: Vec<OrderReply>,
}

#[derive(Serialize)]
pub struct CancelData {
    pub statuses: Vec<CancelReply>,
}

#[derive(Serialize)]
#[serde(untagged)]
pub enum ControlReply<'a> {
    Ok {
        ok: bool,
    },
    #[serde(rename_all = "camelCase")]
    Upgrade {
        post_only: bool,
    },
    Account(AccountReply<'a>),
    Dust(DustReply),
    Fund(FundReply<'a>),
    Book(L2Book<'a>),
    EvmSends(&'a [EvmSend]),
}

#[derive(Serialize)]
#[serde(untagged)]
pub enum AccountReply<'a> {
    Missing {
        address: Address,
        exists: bool,
    },
    Present {
        address: Address,
        exists: bool,
        #[serde(rename = "userHasSentTx")]
        user_has_sent_tx: bool,
        serialize_f64: bool,
        state: Balances<'a>,
        nonces: &'a BTreeSet<u64>,
        fills: &'a [Fill],
        ledger: &'a [LedgerEntry],
        orders: &'a BTreeMap<u64, OrderRecord>,
        cloids: &'a BTreeMap<Cloid, u64>,
    },
}

#[cfg(test)]
mod tests;
