//! Spot orders: limit orders matched with price-time priority against resting
//! and synthetic depth, remainders that rest with locked funds, and stop or
//! take-profit triggers that fire on the reference price.
//!
//! Immediate-or-cancel behavior, fees, cost basis and rejection texts are
//! measured on testnet. Resting, post-only, maker, self-trade and trigger
//! semantics follow the exchange's documented rules and the recovered matching
//! loop; they have not been probed live.

use super::{
    Engine,
    account::{
        Direction, Draft, Fill, OrderKind, OrderRecord, OrderStatus, PairIndex, Side, TokenIndex,
    },
    book::Resting,
    quote_fee, quote_units, tx_hash,
};
use crate::{
    decimal::{self, ONE, ZERO, add, div, mul, sub, wire},
    error::{Error, Result},
    wire::{Address, Cloid, Grouping, OrderAction, OrderRequest, OrderType, Tif, Tpsl, TxHash},
};
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde::Serialize;
use std::{collections::BTreeMap, sync::Arc};

/// One entry of an order action's `statuses`.
#[derive(Serialize)]
#[serde(rename_all = "lowercase")]
pub enum OrderReply {
    Error(Error),
    Filled(Filled),
    Resting(Rested),
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Filled {
    #[serde(with = "decimal::text")]
    total_sz: Decimal,
    #[serde(with = "decimal::text")]
    avg_px: Decimal,
    oid: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    cloid: Option<Cloid>,
}

#[derive(Serialize)]
pub struct Rested {
    oid: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    cloid: Option<Cloid>,
}

/// An order that passed static validation and awaits execution.
#[derive(Clone, Copy)]
pub(super) struct Prepared {
    pub asset: u32,
    pub pair: PairIndex,
    pub buy: bool,
    pub sz: Decimal,
    pub px: Decimal,
    pub cloid: Option<Cloid>,
    pub kind: OrderKind,
}

/// Everything about a market that a fill needs, copied out so the borrow of
/// engine tables ends before accounts and books are mutated.
pub(super) struct Market {
    name: Arc<str>,
    base: TokenIndex,
    quote: TokenIndex,
    quote_name: Arc<str>,
    base_sz_decimals: u32,
    base_wei_decimals: u32,
    quote_wei_decimals: u32,
    usdc: TokenIndex,
    quote_usdc: Decimal,
    pub(super) quote_units: Decimal,
    fee_weight: Decimal,
    volume_weight: Decimal,
}

impl Market {
    fn pays_and_receives(&self, buy: bool) -> (TokenIndex, TokenIndex) {
        if buy {
            (self.quote, self.base)
        } else {
            (self.base, self.quote)
        }
    }

    /// Funds a resting order of `sz` at `px` locks.
    fn locked(&self, buy: bool, sz: Decimal, px: Decimal) -> Result<(TokenIndex, Decimal)> {
        Ok(if buy {
            (self.quote, mul(sz, px)?)
        } else {
            (self.base, sz)
        })
    }
}

/// The balance and fee effects of one side of a match.
struct Settled {
    cost: Decimal,
    cost_usdc: Decimal,
    fee: Decimal,
    fee_token: TokenIndex,
    start: Decimal,
    closed_pnl: Decimal,
    /// Contribution to the account's UTC-day volume.
    counted: Decimal,
}

/// A maker's side of a match, computed before anything is committed.
pub(super) struct MakerFill {
    maker: Address,
    oid: u64,
    fill: Fill,
    counted: Decimal,
    cost_usdc: Decimal,
}

/// Which order of the taker's the match loop is executing.
struct Taker {
    owner: Address,
    oid: u64,
    cloid: Option<Cloid>,
    hash: TxHash,
    tif: Tif,
    /// A triggered stop or take-profit market order.
    market_order: bool,
    /// The record already exists (a trigger firing), so it is updated, not created.
    existing: bool,
}

impl Engine {
    pub(super) fn order_action(
        &mut self,
        action: &OrderAction,
        signer: Address,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<Vec<OrderReply>> {
        if action
            .grouping
            .is_some_and(|grouping| grouping != Grouping::Na)
            || action.builder.is_some()
        {
            return Err(
                "Simulator: only ungrouped orders without builder fees are supported".into(),
            );
        }
        if action.orders.is_empty() {
            return Ok(vec![OrderReply::Error("Orders are empty.".into())]);
        }
        // An unknown asset anywhere rejects the envelope; any other static
        // failure rejects the batch with a single status.
        let mut resolved = Vec::with_capacity(action.orders.len());
        for order in &action.orders {
            let pair = u64::from(order.a)
                .checked_sub(10000)
                .map(PairIndex)
                .and_then(|index| self.pairs.get(&index))
                .ok_or("Invalid spot")?;
            resolved.push((order, pair.index));
        }
        let prepared: Result<Vec<Prepared>> = resolved
            .into_iter()
            .map(|(order, pair)| self.prepare(order, pair))
            .collect();
        Ok(match prepared {
            Err(error) => vec![OrderReply::Error(error)],
            Ok(orders) => orders
                .into_iter()
                .map(|order| {
                    self.place(order, signer, now_ms, raw)
                        .unwrap_or_else(OrderReply::Error)
                })
                .collect(),
        })
    }

    pub(super) fn prepare(&self, order: &OrderRequest, pair: PairIndex) -> Result<Prepared> {
        let pair = &self.pairs[&pair];
        let token = self.token(pair.base);
        if order.r {
            return Err("Reduce-only is invalid for spot trading.".into());
        }
        let kind = match order.t {
            OrderType::Limit(limit) => OrderKind::Limit(limit.tif),
            OrderType::Trigger(trigger) => OrderKind::Trigger {
                is_market: trigger.is_market,
                trigger_px: trigger.trigger_px,
                take_profit: trigger.tpsl == Tpsl::Tp,
            },
        };
        let sz = order.s;
        if sz == ZERO {
            return Err("Order has zero size.".into());
        }
        if sz.normalize().scale() > token.sz_decimals {
            return Err("Order has invalid size.".into());
        }
        let px = order.p;
        if px <= ZERO {
            return Err("Order has invalid price.".into());
        }
        if !on_tick(px, token.sz_decimals) {
            return Err(format!("Price must be divisible by tick size. asset={}", order.a).into());
        }
        if let OrderKind::Trigger { trigger_px, .. } = kind
            && (trigger_px <= ZERO || !on_tick(trigger_px, token.sz_decimals))
        {
            return Err(format!("Invalid TP/SL price. asset={}", order.a).into());
        }
        if matches!(kind, OrderKind::Limit(_))
            && let Some(reference) = self.reference_price(pair.index)?
        {
            let fifth = Decimal::new(2, 1);
            if px <= mul(reference, fifth)? || reference < mul(px, fifth)? {
                return Err(
                    "Order price cannot be more than 80% away from the reference price".into(),
                );
            }
        }
        Ok(Prepared {
            asset: order.a,
            pair: pair.index,
            buy: order.b,
            sz,
            px,
            cloid: order.c,
            kind,
        })
    }

    /// Execute a freshly prepared order for `signer`, assigning it an oid.
    pub(super) fn place(
        &mut self,
        order: Prepared,
        signer: Address,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<OrderReply> {
        let post_only = matches!(order.kind, OrderKind::Limit(Tif::Alo));
        if self.upgrade_post_only && !post_only {
            return Err("Only post-only orders allowed immediately after network upgrade".into());
        }
        if let Some(cloid) = order.cloid
            && self
                .accounts
                .get(&signer)
                .and_then(|account| account.cloids.get(&cloid))
                .and_then(|oid| self.accounts[&signer].orders.get(oid))
                .is_some_and(|record| record.order.status == OrderStatus::Open)
        {
            return Err(format!(
                "Simulator: cloid {cloid} already belongs to an open order. asset={}",
                order.asset
            )
            .into());
        }
        let oid = self.next_oid;
        let next_oid = oid.checked_add(1).ok_or("Simulator: order id exhausted")?;
        let hash = tx_hash(raw, oid);
        let reply = match order.kind {
            OrderKind::Limit(tif) => self.execute_limit(
                &order,
                Taker {
                    owner: signer,
                    oid,
                    cloid: order.cloid,
                    hash,
                    tif,
                    market_order: false,
                    existing: false,
                },
                now_ms,
            )?,
            OrderKind::Trigger { .. } => self.place_trigger(&order, signer, oid, now_ms)?,
        };
        self.next_oid = next_oid;
        self.fire_triggers(order.pair, now_ms)?;
        Ok(reply)
    }

    pub(super) fn market(&self, pair: PairIndex) -> Result<Market> {
        let pair = &self.pairs[&pair];
        let base = self.token(pair.base);
        let quote = self.token(pair.quote);
        let stable =
            self.quote_tokens.contains(&pair.base) && self.quote_tokens.contains(&pair.quote);
        let stable_weight = if stable { Decimal::new(2, 1) } else { ONE };
        let aligned = self.aligned_quote_tokens.contains(&pair.quote);
        let fee_weight = mul(
            stable_weight,
            if aligned { Decimal::new(8, 1) } else { ONE },
        )?;
        let volume_weight = if pair.quote == self.usdc || aligned {
            mul(
                stable_weight,
                if aligned { Decimal::new(12, 1) } else { ONE },
            )?
        } else {
            ZERO
        };
        Ok(Market {
            name: pair.name.clone(),
            base: pair.base,
            quote: pair.quote,
            quote_name: quote.name.clone(),
            base_sz_decimals: base.sz_decimals,
            base_wei_decimals: base.wei_decimals,
            quote_wei_decimals: quote.wei_decimals,
            usdc: self.usdc,
            quote_usdc: self.usdc_value(quote, ONE)?,
            quote_units: quote_units(quote.wei_decimals)?,
            fee_weight,
            volume_weight,
        })
    }

    fn taker_rate(&self, market: &Market, owner: Address, now_ms: u64) -> Result<f64> {
        let account = self.accounts.get(&owner);
        let rate = self.fees.spot_taker_rate(
            account.map(|a| &a.daily_volume),
            account.map(|a| &a.daily_add_volume),
            now_ms,
        )?;
        mul(rate, market.fee_weight)?
            .to_f64()
            .ok_or_else(|| "Simulator: fee rate out of range".into())
    }

    fn maker_rate(&self, market: &Market, owner: Address, now_ms: u64) -> Result<f64> {
        let account = self.accounts.get(&owner);
        let rate = self.fees.spot_maker_rate(
            account.map(|a| &a.daily_volume),
            account.map(|a| &a.daily_add_volume),
            now_ms,
        )?;
        mul(rate, market.fee_weight)?
            .to_f64()
            .ok_or_else(|| "Simulator: fee rate out of range".into())
    }

    /// Apply one side of a match of `qty` at `px` to a draft. Fees are quantized
    /// in quote wei before a buy's fee is converted to base. `held` releases the
    /// paid amount from a resting order's lock.
    #[allow(clippy::too_many_arguments)]
    fn settle(
        &self,
        draft: &mut Draft,
        market: &Market,
        buy: bool,
        qty: Decimal,
        px: Decimal,
        rate: f64,
        held: bool,
    ) -> Result<Settled> {
        let cost = mul(qty, px)?;
        if market.quote_usdc <= ZERO {
            return Err("Simulator: missing positive USDC valuation for quote token".into());
        }
        let cost_usdc = if market.quote == market.usdc {
            cost
        } else {
            mul(cost, market.quote_usdc)?.trunc_with_scale(8)
        };
        let counted = if market.volume_weight != ZERO {
            mul(
                mul(cost, market.volume_weight)?.trunc_with_scale(2),
                Decimal::TWO,
            )?
        } else {
            ZERO
        };
        let (paying, receiving) = market.pays_and_receives(buy);
        let received = if buy { qty } else { cost };
        let quote_fee = quote_fee(cost, market.quote_units, rate)?;
        let fee = if buy { div(quote_fee, px)? } else { quote_fee }.trunc_with_scale(if buy {
            market.base_wei_decimals
        } else {
            market.quote_wei_decimals
        });
        let start = draft.total(market.base);
        let prior_entry = draft.entry(market.base);
        let cost_basis_usdc = if !buy && start > ZERO {
            if qty == start {
                prior_entry
            } else {
                div(mul(prior_entry, qty)?, start)?.trunc_with_scale(8)
            }
        } else {
            ZERO
        };
        let cost_basis = if buy || market.quote == market.usdc {
            cost_basis_usdc
        } else {
            div(cost_basis_usdc, market.quote_usdc)?.trunc_with_scale(market.quote_wei_decimals)
        };
        let quote_basis = (market.quote != market.usdc).then(|| {
            draft
                .get(market.quote)
                .map_or((ZERO, ZERO), |b| (b.total, b.entry))
        });
        let paid = if buy { cost } else { qty };
        if held {
            draft.hold(paying, -paid)?;
        }
        draft.credit(paying, -paid)?;
        draft.credit(receiving, sub(received, fee)?)?;
        let base_balance = draft.balance(market.base);
        base_balance.entry = if buy {
            add(prior_entry, cost_usdc)?
        } else if base_balance.total == ZERO {
            ZERO
        } else {
            div(mul(prior_entry, base_balance.total)?, start)?.trunc_with_scale(8)
        };
        if let Some((quote_start, quote_entry)) = quote_basis {
            let quote_balance = draft.balance(market.quote);
            quote_balance.entry = if buy {
                div(mul(quote_entry, quote_balance.total)?, quote_start)?.trunc_with_scale(8)
            } else {
                add(quote_entry, cost_usdc)?
            };
        }
        Ok(Settled {
            cost,
            cost_usdc,
            fee,
            fee_token: receiving,
            start,
            closed_pnl: if buy { ZERO } else { sub(cost, cost_basis)? },
            counted,
        })
    }

    /// The maker's side of a match against a user's resting order.
    #[allow(clippy::too_many_arguments)]
    pub(super) fn settle_maker(
        &self,
        drafts: &mut BTreeMap<Address, Draft>,
        market: &Market,
        entry: &Resting,
        taker_buys: bool,
        qty: Decimal,
        now_ms: u64,
        tid: u64,
        hash: TxHash,
    ) -> Result<MakerFill> {
        let maker = entry
            .owner
            .ok_or("Simulator: synthetic depth has no maker")?;
        let maker_buys = !taker_buys;
        let rate = self.maker_rate(market, maker, now_ms)?;
        let draft = drafts
            .entry(maker)
            .or_insert_with(|| Draft::new(self.accounts.get(&maker), [market.base, market.quote]));
        let settled = self.settle(draft, market, maker_buys, qty, entry.px, rate, true)?;
        let cloid = self
            .accounts
            .get(&maker)
            .and_then(|a| a.orders.get(&entry.oid))
            .and_then(|record| record.order.order.cloid);
        Ok(MakerFill {
            maker,
            oid: entry.oid,
            fill: Fill {
                coin: market.name.clone(),
                px: entry.px,
                sz: qty,
                side: Side::from_buy(maker_buys),
                time: now_ms,
                start_position: settled.start,
                dir: if maker_buys {
                    Direction::Buy
                } else {
                    Direction::Sell
                },
                closed_pnl: settled.closed_pnl,
                hash,
                oid: entry.oid,
                crossed: false,
                fee: settled.fee,
                tid,
                fee_token: self.token(settled.fee_token).name.clone(),
                twap_id: (),
                cloid,
            },
            counted: settled.counted,
            cost_usdc: settled.cost_usdc,
        })
    }

    /// Commit maker fills: balances, fills, order records and volume.
    pub(super) fn commit_makers(
        &mut self,
        drafts: BTreeMap<Address, Draft>,
        fills: Vec<MakerFill>,
        now_ms: u64,
    ) {
        for (maker, draft) in drafts {
            if let Some(account) = self.accounts.get_mut(&maker) {
                draft.commit(account);
            }
        }
        let day = (now_ms / 86_400_000) as i64;
        for maker_fill in fills {
            let Some(account) = self.accounts.get_mut(&maker_fill.maker) else {
                continue;
            };
            if let Some(record) = account.orders.get_mut(&maker_fill.oid) {
                record.order.order.sz = record
                    .order
                    .order
                    .sz
                    .checked_sub(maker_fill.fill.sz)
                    .unwrap_or(ZERO);
                if record.order.order.sz == ZERO {
                    record.set_status(OrderStatus::Filled, now_ms);
                }
            }
            if maker_fill.counted != ZERO {
                let total = account.daily_add_volume.entry(day).or_default();
                *total = add(*total, maker_fill.counted).unwrap_or(*total);
            }
            account.lifetime_volume = add(account.lifetime_volume, maker_fill.cost_usdc)
                .unwrap_or(account.lifetime_volume);
            account.fills.push(maker_fill.fill);
        }
    }

    /// Match a limit order, then rest, cancel or reject its remainder.
    fn execute_limit(&mut self, order: &Prepared, taker: Taker, now_ms: u64) -> Result<OrderReply> {
        let Prepared {
            asset,
            pair: pair_index,
            buy,
            sz,
            px,
            ..
        } = *order;
        let market = self.market(pair_index)?;
        let rate = self.taker_rate(&market, taker.owner, now_ms)?;
        let account = self
            .accounts
            .get(&taker.owner)
            .ok_or("Simulator: missing signer")?;
        let (paying, _) = market.pays_and_receives(buy);
        let mut draft = Draft::new(Some(account), [market.base, market.quote]);
        let resting: Vec<Resting> = self
            .books
            .get(&pair_index)
            .map(|book| book.opposite(buy).to_vec())
            .unwrap_or_default();
        let marketable = resting.first().is_some_and(|best| best.crossed_by(buy, px));
        let rests = matches!(taker.tif, Tif::Gtc | Tif::Alo);
        let mut early: Option<(OrderStatus, String)> = None;
        if taker.tif == Tif::Alo && marketable {
            let bid = self
                .books
                .get(&pair_index)
                .and_then(|b| b.bids.first())
                .map(|b| b.px);
            let ask = self
                .books
                .get(&pair_index)
                .and_then(|b| b.asks.first())
                .map(|a| a.px);
            early = Some((
                OrderStatus::BadAloPxRejected,
                format!(
                    "Post only order would have immediately matched, bbo was {} @ {}. asset={asset}",
                    bid.map_or("None".to_owned(), wire),
                    ask.map_or("None".to_owned(), wire)
                ),
            ));
        } else if rests {
            if mul(px, sz)? < Decimal::TEN {
                early = Some((
                    OrderStatus::MinTradeNtlRejected,
                    format!(
                        "Order must have minimum value of 10 {}. asset={asset}",
                        market.quote_name
                    ),
                ));
            } else {
                let (token, needed) = market.locked(buy, sz, px)?;
                if draft.available(token) < needed {
                    early = Some((
                        OrderStatus::InsufficientSpotBalanceRejected,
                        format!("Insufficient spot balance asset={asset}"),
                    ));
                }
            }
        }
        if let Some((status, message)) = early {
            self.record_order(order, &taker, Side::from_buy(buy), sz, status, now_ms);
            return Ok(OrderReply::Error(message.into()));
        }
        let mut tid = self.next_tid;
        let day = (now_ms / 86_400_000) as i64;
        let mut daily_volume = account.daily_volume.get(&day).copied().unwrap_or(ZERO);
        let mut lifetime = account.lifetime_volume;
        let mut balance_limited = false;
        let mut below_minimum = false;
        let mut remaining = sz;
        let mut notional = ZERO;
        let mut last_price = None;
        let mut fills = Vec::new();
        let mut maker_drafts = BTreeMap::new();
        let mut maker_fills = Vec::new();
        // Consumed entries by position: leftover size (zero removes the entry).
        let mut consumed: BTreeMap<usize, Decimal> = BTreeMap::new();
        let mut self_canceled: Vec<usize> = Vec::new();
        for (position, entry) in resting.iter().enumerate() {
            if remaining == ZERO || !entry.crossed_by(buy, px) {
                break;
            }
            if entry.owner == Some(taker.owner) {
                // Self-trade prevention cancels the resting order and releases its lock.
                let (token, locked) = market.locked(!buy, entry.sz, entry.px)?;
                draft.hold(token, -locked)?;
                self_canceled.push(position);
                continue;
            }
            let mut quantity = remaining.min(entry.sz);
            if quantity == ZERO {
                continue;
            }
            let available = draft.available(paying);
            if available == ZERO {
                balance_limited = true;
                break;
            }
            if fills.is_empty() && mul(entry.px, sz)? < Decimal::TEN {
                below_minimum = true;
                break;
            }
            let affordable = if buy {
                if mul(quantity, entry.px)? <= available {
                    quantity
                } else {
                    div(available, entry.px)?.trunc_with_scale(market.base_sz_decimals)
                }
            } else {
                available.trunc_with_scale(market.base_sz_decimals)
            };
            if affordable < quantity {
                quantity = affordable;
                balance_limited = true;
            }
            if quantity == ZERO {
                break;
            }
            let settled = self.settle(&mut draft, &market, buy, quantity, entry.px, rate, false)?;
            if entry.owner.is_some() {
                maker_fills.push(self.settle_maker(
                    &mut maker_drafts,
                    &market,
                    entry,
                    buy,
                    quantity,
                    now_ms,
                    tid,
                    taker.hash,
                )?);
            }
            fills.push(Fill {
                coin: market.name.clone(),
                px: entry.px,
                sz: quantity,
                side: Side::from_buy(buy),
                time: now_ms,
                start_position: settled.start,
                dir: if buy { Direction::Buy } else { Direction::Sell },
                closed_pnl: settled.closed_pnl,
                hash: taker.hash,
                oid: taker.oid,
                crossed: true,
                fee: settled.fee,
                tid,
                fee_token: self.token(settled.fee_token).name.clone(),
                twap_id: (),
                cloid: taker.cloid,
            });
            tid = tid.checked_add(1).ok_or("Simulator: trade id exhausted")?;
            daily_volume = add(daily_volume, settled.counted)?;
            lifetime = add(lifetime, settled.cost_usdc)?;
            remaining = sub(remaining, quantity)?;
            consumed.insert(position, sub(entry.sz, quantity)?);
            notional = add(notional, settled.cost)?;
            last_price = Some(entry.px);
        }
        let executed = sub(sz, remaining)?;
        let insufficient = executed == ZERO && balance_limited;
        let side = Side::from_buy(buy);
        let (status, reply) = if below_minimum {
            (
                OrderStatus::MinTradeNtlRejected,
                OrderReply::Error(
                    format!(
                        "Order must have minimum value of 10 {}. asset={asset}",
                        market.quote_name
                    )
                    .into(),
                ),
            )
        } else if insufficient {
            (
                OrderStatus::InsufficientSpotBalanceRejected,
                OrderReply::Error(format!("Insufficient spot balance asset={asset}").into()),
            )
        } else if executed == ZERO && !rests {
            if taker.market_order {
                (
                    OrderStatus::MarketOrderNoLiquidityRejected,
                    OrderReply::Error(
                        format!("No liquidity available for market order. asset={asset}").into(),
                    ),
                )
            } else {
                (
                    OrderStatus::IocCancelRejected,
                    OrderReply::Error(
                        format!(
                            "Order could not immediately match against any resting orders. asset={asset}"
                        )
                        .into(),
                    ),
                )
            }
        } else if remaining > ZERO && rests {
            let (token, locked) = market.locked(buy, remaining, px)?;
            draft.hold(token, locked)?;
            (
                OrderStatus::Open,
                OrderReply::Resting(Rested {
                    oid: taker.oid,
                    cloid: taker.cloid,
                }),
            )
        } else {
            (
                OrderStatus::Filled,
                OrderReply::Filled(Filled {
                    total_sz: executed,
                    avg_px: div(notional, executed)?.trunc_with_scale(8 - market.base_sz_decimals),
                    oid: taker.oid,
                    cloid: taker.cloid,
                }),
            )
        };
        // Every fallible calculation precedes any mutation.
        let owner = taker.owner;
        let account = self
            .accounts
            .get_mut(&owner)
            .ok_or("Simulator: missing signer")?;
        draft.commit(account);
        account.fills.extend(fills);
        if executed > ZERO && market.volume_weight != ZERO {
            account.daily_volume.insert(day, daily_volume);
        }
        account.lifetime_volume = lifetime;
        self.record_order(order, &taker, side, remaining, status, now_ms);
        for position in &self_canceled {
            let oid = resting[*position].oid;
            if let Some(record) = self
                .accounts
                .get_mut(&owner)
                .and_then(|a| a.orders.get_mut(&oid))
            {
                record.set_status(OrderStatus::SelfTradeCanceled, now_ms);
            }
        }
        self.commit_makers(maker_drafts, maker_fills, now_ms);
        let book = self.books.entry(pair_index).or_default();
        let opposite = book.opposite_mut(buy);
        let mut rebuilt = Vec::with_capacity(opposite.len() + 1);
        for (position, entry) in resting.iter().enumerate() {
            if self_canceled.contains(&position) {
                continue;
            }
            match consumed.get(&position) {
                Some(leftover) if *leftover == ZERO => {}
                Some(leftover) => rebuilt.push(Resting {
                    sz: *leftover,
                    ..*entry
                }),
                None => rebuilt.push(*entry),
            }
        }
        // Entries that arrived while this order matched cannot exist: the state
        // thread is the only writer, so the snapshot is the whole side.
        *opposite = rebuilt;
        if status == OrderStatus::Open {
            book.rest(
                buy,
                Resting {
                    oid: taker.oid,
                    owner: Some(owner),
                    px,
                    sz: remaining,
                    n: 1,
                },
            );
        }
        self.next_tid = tid;
        if let Some(price) = last_price {
            self.mark_prices.insert(pair_index, price);
        }
        Ok(reply)
    }

    /// Create or update the order's `orderStatus` record and cloid index.
    fn record_order(
        &mut self,
        order: &Prepared,
        taker: &Taker,
        side: Side,
        remaining: Decimal,
        status: OrderStatus,
        now_ms: u64,
    ) {
        let name = self.pairs[&order.pair].name.clone();
        let Some(account) = self.accounts.get_mut(&taker.owner) else {
            return;
        };
        if taker.existing {
            if let Some(record) = account.orders.get_mut(&taker.oid) {
                record.order.order.sz = remaining;
                record.set_status(status, now_ms);
            }
            return;
        }
        let record = OrderRecord::new(
            name,
            order.pair,
            side,
            order.kind,
            order.px,
            remaining,
            order.sz,
            taker.oid,
            taker.cloid,
            status,
            now_ms,
        );
        account.orders.insert(taker.oid, record);
        if let Some(cloid) = taker.cloid {
            account.cloids.insert(cloid, taker.oid);
        }
    }

    /// Rest a stop or take-profit order until the reference price reaches it.
    fn place_trigger(
        &mut self,
        order: &Prepared,
        owner: Address,
        oid: u64,
        now_ms: u64,
    ) -> Result<OrderReply> {
        let OrderKind::Trigger {
            trigger_px,
            take_profit,
            ..
        } = order.kind
        else {
            return Err("Simulator: not a trigger order".into());
        };
        let side = Side::from_buy(order.buy);
        let taker = Taker {
            owner,
            oid,
            cloid: order.cloid,
            hash: TxHash([0; 32]),
            tif: Tif::Gtc,
            market_order: false,
            existing: false,
        };
        if let Some(reference) = self.reference_price(order.pair)?
            && trigger_met(side, take_profit, trigger_px, reference)
        {
            self.record_order(
                order,
                &taker,
                side,
                order.sz,
                OrderStatus::BadTriggerPxRejected,
                now_ms,
            );
            return Ok(OrderReply::Error(
                format!("Invalid TP/SL price. asset={}", order.asset).into(),
            ));
        }
        self.record_order(order, &taker, side, order.sz, OrderStatus::Open, now_ms);
        self.triggers
            .entry(order.pair)
            .or_default()
            .push((owner, oid));
        Ok(OrderReply::Resting(Rested {
            oid,
            cloid: order.cloid,
        }))
    }

    /// Fire every trigger on `pair` whose condition the reference price now meets.
    pub(super) fn fire_triggers(&mut self, pair: PairIndex, now_ms: u64) -> Result<()> {
        loop {
            let Some(reference) = self.reference_price(pair)? else {
                return Ok(());
            };
            let Some(pending) = self.triggers.get(&pair) else {
                return Ok(());
            };
            let Some((owner, oid)) = pending.iter().copied().find(|(owner, oid)| {
                self.accounts
                    .get(owner)
                    .and_then(|a| a.orders.get(oid))
                    .is_some_and(|record| {
                        let detail = &record.order.order;
                        matches!(detail.kind, OrderKind::Trigger { trigger_px, take_profit, .. }
                            if trigger_met(detail.side, take_profit, trigger_px, reference))
                    })
            }) else {
                return Ok(());
            };
            if let Some(pending) = self.triggers.get_mut(&pair) {
                pending.retain(|entry| *entry != (owner, oid));
            }
            self.fire(owner, oid, now_ms)?;
        }
    }

    /// Convert a triggered order into a market or limit order under its own oid.
    fn fire(&mut self, owner: Address, oid: u64, now_ms: u64) -> Result<()> {
        let Some(record) = self
            .accounts
            .get_mut(&owner)
            .and_then(|account| account.orders.get_mut(&oid))
        else {
            return Ok(());
        };
        let detail = &record.order.order;
        let OrderKind::Trigger { is_market, .. } = detail.kind else {
            return Ok(());
        };
        let asset = u32::try_from(detail.pair.0 + 10000).unwrap_or(u32::MAX);
        let order = Prepared {
            asset,
            pair: detail.pair,
            buy: detail.side == Side::Buy,
            sz: detail.sz,
            px: detail.limit_px,
            cloid: detail.cloid,
            kind: detail.kind,
        };
        record.set_status(OrderStatus::Triggered, now_ms);
        let mut hash = [0; 32];
        hash[24..].copy_from_slice(&oid.to_be_bytes());
        let taker = Taker {
            owner,
            oid,
            cloid: order.cloid,
            hash: TxHash(hash),
            tif: if is_market { Tif::Ioc } else { Tif::Gtc },
            market_order: is_market,
            existing: true,
        };
        // A rejection is recorded on the order; nothing else observes it.
        let _ = self.execute_limit(&order, taker, now_ms)?;
        Ok(())
    }
}

/// Whether a stop or take-profit condition holds at `reference`. A sell
/// take-profit or buy stop fires when the price rises to the trigger; the
/// other two fire when it falls to it.
pub(super) fn trigger_met(
    side: Side,
    take_profit: bool,
    trigger_px: Decimal,
    reference: Decimal,
) -> bool {
    let above = take_profit == (side == Side::Sell);
    if above {
        reference >= trigger_px
    } else {
        reference <= trigger_px
    }
}

/// Prices carry at most `8 - szDecimals` decimals and five significant figures.
fn on_tick(px: Decimal, sz_decimals: u32) -> bool {
    let normal = px.normalize();
    let significant = normal
        .to_string()
        .bytes()
        .filter(u8::is_ascii_digit)
        .skip_while(|b| *b == b'0')
        .count();
    normal.scale() <= 8 - sz_decimals && (normal.scale() == 0 || significant <= 5)
}
