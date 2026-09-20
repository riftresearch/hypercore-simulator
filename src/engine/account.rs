//! Per-account state: balances, nonce history, fills, ledger and order records.

use crate::{
    decimal::{self, ZERO, add, div, mul},
    error::Result,
    wire::{Address, Cloid, Tif, TxHash},
};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct TokenIndex(pub u64);

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PairIndex(pub u64);

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Balance {
    pub total: Decimal,
    /// USDC cost basis of the holding, reported as `entryNtl`.
    pub entry: Decimal,
    /// Amount locked by resting orders; `total - hold` is spendable.
    pub hold: Decimal,
}

impl Balance {
    pub fn available(&self) -> Decimal {
        self.total - self.hold
    }
}

/// A user's armed dead-man switch.
#[derive(Clone, Copy, Debug, Default)]
pub struct ScheduledCancel {
    pub deadline_ms: Option<u64>,
    /// UTC day and count of triggers fired that day; at most ten per day.
    pub fired: (i64, u32),
}

#[derive(Default)]
pub struct Account {
    pub balances: BTreeMap<TokenIndex, Balance>,
    pub sent: bool,
    pub serialize_f64: bool,
    pub nonces: BTreeSet<u64>,
    pub fills: Vec<Fill>,
    pub ledger: Vec<LedgerEntry>,
    pub orders: BTreeMap<u64, OrderRecord>,
    pub cloids: BTreeMap<Cloid, u64>,
    /// Taker (`userCross`) and maker (`userAdd`) volume by UTC day.
    pub daily_volume: BTreeMap<i64, Decimal>,
    pub daily_add_volume: BTreeMap<i64, Decimal>,
    /// Lifetime USDC notional traded, gating `scheduleCancel`.
    pub lifetime_volume: Decimal,
    pub scheduled_cancel: ScheduledCancel,
}

impl Account {
    pub fn available(&self, token: TokenIndex) -> Decimal {
        self.balances.get(&token).map_or(ZERO, Balance::available)
    }

    /// Open orders, resting or awaiting a trigger, by oid.
    pub fn open_orders(&self) -> impl Iterator<Item = (&u64, &OrderRecord)> {
        self.orders
            .iter()
            .filter(|(_, record)| record.order.status == OrderStatus::Open)
    }
}

/// Balances copied out of accounts for editing. Nothing is written back until
/// every fallible step has succeeded, so a rejected action leaves every account
/// exactly as it was, without cloning whole balance maps.
#[derive(Debug)]
pub struct Draft {
    slots: Vec<(TokenIndex, Option<Balance>)>,
}

impl Draft {
    pub fn new(account: Option<&Account>, tokens: impl IntoIterator<Item = TokenIndex>) -> Self {
        let mut slots: Vec<(TokenIndex, Option<Balance>)> = Vec::with_capacity(2);
        for token in tokens {
            if !slots.iter().any(|(loaded, _)| *loaded == token) {
                let balance = account.and_then(|a| a.balances.get(&token)).copied();
                slots.push((token, balance));
            }
        }
        Self { slots }
    }

    pub fn get(&self, token: TokenIndex) -> Option<Balance> {
        self.slots
            .iter()
            .find(|(loaded, _)| *loaded == token)
            .and_then(|(_, balance)| *balance)
    }

    pub fn total(&self, token: TokenIndex) -> Decimal {
        self.get(token).map_or(ZERO, |b| b.total)
    }

    pub fn available(&self, token: TokenIndex) -> Decimal {
        self.get(token).map_or(ZERO, |b| b.available())
    }

    pub fn entry(&self, token: TokenIndex) -> Decimal {
        self.get(token).map_or(ZERO, |b| b.entry)
    }

    /// The balance row, created at zero if the account did not hold the token.
    pub fn balance(&mut self, token: TokenIndex) -> &mut Balance {
        let slot = self
            .slots
            .iter_mut()
            .find(|(loaded, _)| *loaded == token)
            .map(|(_, balance)| balance)
            .expect("every edited token is loaded into the draft");
        slot.get_or_insert_default()
    }

    /// Apply a signed delta to the total; a zero delta does not create a row.
    pub fn credit(&mut self, token: TokenIndex, value: Decimal) -> Result<()> {
        if value == ZERO {
            return Ok(());
        }
        let balance = self.balance(token);
        let next = add(balance.total, value)?;
        if next < ZERO {
            return Err("Insufficient spot balance".into());
        }
        balance.total = next;
        Ok(())
    }

    /// Apply a signed delta to the locked amount.
    pub fn hold(&mut self, token: TokenIndex, value: Decimal) -> Result<()> {
        if value == ZERO {
            return Ok(());
        }
        let balance = self.balance(token);
        let next = add(balance.hold, value)?;
        if next < ZERO || next > balance.total {
            return Err("Simulator: hold exceeds balance".into());
        }
        balance.hold = next;
        Ok(())
    }

    pub fn commit(self, account: &mut Account) {
        for (token, balance) in self.slots {
            if let Some(balance) = balance {
                account.balances.insert(token, balance);
            }
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Side {
    #[serde(rename = "B")]
    Buy,
    #[serde(rename = "A")]
    Sell,
}

impl Side {
    pub fn from_buy(buy: bool) -> Self {
        if buy { Self::Buy } else { Self::Sell }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Direction {
    Buy,
    Sell,
    #[serde(rename = "Spot Dust Conversion")]
    DustConversion,
}

/// One fill as reported by `userFills`; also parses captured upstream fills.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Fill {
    pub coin: Arc<str>,
    #[serde(with = "decimal::text")]
    pub px: Decimal,
    #[serde(with = "decimal::text")]
    pub sz: Decimal,
    pub side: Side,
    pub time: u64,
    #[serde(with = "decimal::text")]
    pub start_position: Decimal,
    pub dir: Direction,
    #[serde(with = "decimal::text")]
    pub closed_pnl: Decimal,
    pub hash: TxHash,
    pub oid: u64,
    /// True for the taker side of a match.
    pub crossed: bool,
    #[serde(with = "decimal::text")]
    pub fee: Decimal,
    pub tid: u64,
    pub fee_token: Arc<str>,
    pub twap_id: (),
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cloid: Option<Cloid>,
}

/// Combine same-order, same-time fills the way `aggregateByTime` reports them.
pub fn aggregate_fills(fills: &[Fill]) -> Result<Vec<Fill>> {
    let mut groups: Vec<(Fill, Decimal)> = Vec::new();
    for fill in fills {
        // The aggregated feed excludes system dust conversions; the raw feed retains them.
        if fill.dir == Direction::DustConversion {
            continue;
        }
        let notional = mul(fill.px, fill.sz)?;
        match groups.last_mut() {
            Some((previous, total)) if previous.oid == fill.oid && previous.time == fill.time => {
                *total = add(*total, notional)?;
                previous.sz = add(previous.sz, fill.sz)?;
                previous.fee = add(previous.fee, fill.fee)?;
                previous.closed_pnl = add(previous.closed_pnl, fill.closed_pnl)?;
            }
            _ => groups.push((fill.clone(), notional)),
        }
    }
    groups
        .into_iter()
        .rev()
        .take(2000)
        .map(|(mut fill, notional)| {
            fill.px = div(notional, fill.sz)?.round_dp(10);
            Ok(fill)
        })
        .collect()
}

#[derive(Clone, Debug, Serialize)]
pub struct LedgerEntry {
    pub time: u64,
    pub hash: TxHash,
    pub delta: Delta,
}

#[derive(Clone, Debug, Serialize)]
#[serde(tag = "type", rename_all = "camelCase")]
pub enum Delta {
    #[serde(rename_all = "camelCase")]
    Send {
        user: Address,
        destination: Address,
        source_dex: &'static str,
        destination_dex: &'static str,
        token: Arc<str>,
        #[serde(with = "decimal::text")]
        amount: Decimal,
        #[serde(with = "decimal::text")]
        usdc_value: Decimal,
        #[serde(with = "decimal::text")]
        fee: Decimal,
        #[serde(with = "decimal::text")]
        native_token_fee: Decimal,
        nonce: u64,
        fee_token: Arc<str>,
    },
    Deposit {
        #[serde(with = "decimal::text")]
        usdc: Decimal,
    },
}

/// Order lifecycle states, named as `orderStatus` reports them.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub enum OrderStatus {
    Open,
    Filled,
    Canceled,
    Triggered,
    SelfTradeCanceled,
    ScheduledCancel,
    MinTradeNtlRejected,
    InsufficientSpotBalanceRejected,
    IocCancelRejected,
    BadAloPxRejected,
    BadTriggerPxRejected,
    MarketOrderNoLiquidityRejected,
}

/// The `orderStatus` document for a known order.
#[derive(Clone, Debug, Serialize)]
pub struct OrderRecord {
    pub status: &'static str,
    pub order: OrderState,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OrderState {
    pub order: OrderDetail,
    pub status: OrderStatus,
    pub status_timestamp: u64,
}

/// The kind of order, as `orderType` names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum OrderKind {
    Limit(Tif),
    /// A stop or take-profit order waiting on the reference price.
    Trigger {
        is_market: bool,
        trigger_px: Decimal,
        take_profit: bool,
    },
}

impl OrderKind {
    pub fn label(self) -> &'static str {
        match self {
            Self::Limit(_) => "Limit",
            Self::Trigger {
                is_market,
                take_profit,
                ..
            } => match (take_profit, is_market) {
                (true, true) => "Take Profit Market",
                (true, false) => "Take Profit Limit",
                (false, true) => "Stop Market",
                (false, false) => "Stop Limit",
            },
        }
    }

    pub fn tif(self) -> Option<Tif> {
        match self {
            Self::Limit(tif) => Some(tif),
            Self::Trigger { .. } => None,
        }
    }
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OrderDetail {
    pub coin: Arc<str>,
    pub side: Side,
    #[serde(with = "decimal::text")]
    pub limit_px: Decimal,
    /// Remaining size.
    #[serde(with = "decimal::text")]
    pub sz: Decimal,
    pub oid: u64,
    pub timestamp: u64,
    pub trigger_condition: String,
    pub is_trigger: bool,
    #[serde(with = "decimal::text")]
    pub trigger_px: Decimal,
    pub children: [(); 0],
    pub is_position_tpsl: bool,
    pub reduce_only: bool,
    pub order_type: &'static str,
    #[serde(with = "decimal::text")]
    pub orig_sz: Decimal,
    pub tif: Option<Tif>,
    pub cloid: Option<Cloid>,
    /// Engine-only: which market and kind this order is; not serialized.
    #[serde(skip)]
    pub pair: PairIndex,
    #[serde(skip)]
    pub kind: OrderKind,
}

impl OrderRecord {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        coin: Arc<str>,
        pair: PairIndex,
        side: Side,
        kind: OrderKind,
        limit_px: Decimal,
        remaining: Decimal,
        orig_sz: Decimal,
        oid: u64,
        cloid: Option<Cloid>,
        status: OrderStatus,
        now_ms: u64,
    ) -> Self {
        let (is_trigger, trigger_px, trigger_condition) = match kind {
            OrderKind::Limit(_) => (false, ZERO, "N/A".to_owned()),
            OrderKind::Trigger {
                trigger_px,
                take_profit,
                ..
            } => {
                // A sell take-profit or buy stop fires when the price rises.
                let above = take_profit == (side == Side::Sell);
                let word = if above { "above" } else { "below" };
                (
                    true,
                    trigger_px,
                    format!("Price {word} {}", decimal::wire(trigger_px)),
                )
            }
        };
        Self {
            status: "order",
            order: OrderState {
                order: OrderDetail {
                    coin,
                    side,
                    limit_px,
                    sz: remaining,
                    oid,
                    timestamp: now_ms,
                    trigger_condition,
                    is_trigger,
                    trigger_px,
                    children: [],
                    is_position_tpsl: false,
                    reduce_only: false,
                    order_type: kind.label(),
                    orig_sz,
                    tif: kind.tif(),
                    cloid,
                    pair,
                    kind,
                },
                status,
                status_timestamp: now_ms,
            },
        }
    }

    pub fn set_status(&mut self, status: OrderStatus, now_ms: u64) {
        self.order.status = status;
        self.order.status_timestamp = now_ms;
    }
}
