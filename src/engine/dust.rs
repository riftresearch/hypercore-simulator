//! Periodic conversion of sub-lot holdings into quote token, as the exchange
//! performs at each dust boundary. The pooled dust sells into the book like a
//! taker, so users' resting bids receive maker fills.

use super::{
    Engine,
    account::{Direction, Fill, PairIndex, Side},
    quote_fee,
};
use crate::{
    decimal::{ONE, ZERO, add, div, mul, sub, wire},
    error::Result,
    wire::{Address, DustConfig, TxHash},
};
use rust_decimal::{Decimal, prelude::ToPrimitive};
use serde::Serialize;
use std::collections::BTreeMap;

#[derive(Default)]
pub struct DustPolicy {
    interval_ms: Option<u64>,
    /// Absent: network default. `Some(None)`: explicitly uncapped.
    max_notional: Option<Option<Decimal>>,
    /// Last observed clock, from which the next boundary is derived.
    clock: Option<u64>,
    mainnet: bool,
}

impl DustPolicy {
    fn interval(&self) -> u64 {
        self.interval_ms
            .unwrap_or(if self.mainnet { 86_400_000 } else { 60_000 })
    }

    fn cap(&self) -> Option<Decimal> {
        self.max_notional.unwrap_or(self.mainnet.then_some(ONE))
    }

    pub fn reset(&mut self) {
        self.interval_ms = None;
        self.max_notional = None;
        self.clock = None;
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct DustReply {
    ok: bool,
    interval_ms: u64,
    max_notional: Option<String>,
    max_notional_source: &'static str,
}

impl Engine {
    pub(super) fn configure_dust(&mut self, config: &DustConfig) -> Result<DustReply> {
        if config.interval_ms == Some(0) {
            return Err("Simulator: intervalMs must be positive".into());
        }
        let cap = config.max_notional.map(|cap| cap.map(|text| text.0));
        if cap.flatten().is_some_and(|value| value < ZERO) {
            return Err("Simulator: maxNotional must be nonnegative".into());
        }
        if let Some(interval) = config.interval_ms {
            if interval != self.dust.interval() {
                self.dust.clock = None;
            }
            self.dust.interval_ms = Some(interval);
        }
        if let Some(cap) = cap {
            self.dust.max_notional = Some(cap);
        }
        Ok(DustReply {
            ok: true,
            interval_ms: self.dust.interval(),
            max_notional: self.dust.cap().map(wire),
            max_notional_source: if self.dust.max_notional.is_some() {
                "override"
            } else if self.dust.mainnet {
                "hip1"
            } else {
                "testnet-unmeasured"
            },
        })
    }

    /// Observe the clock: fire due scheduled cancels and sweep dust once if a
    /// boundary was crossed.
    pub fn advance(&mut self, now_ms: u64, mainnet: bool) -> Result<()> {
        self.run_scheduled_cancels(now_ms)?;
        if self.dust.mainnet != mainnet {
            self.dust.mainnet = mainnet;
            self.dust.clock = None;
        }
        let interval = self.dust.interval();
        if let Some(previous) = self.dust.clock
            && now_ms > previous
            && now_ms / interval > previous / interval
        {
            // A jump without intervening activity needs only one sweep. Use the
            // first crossed UTC boundary for fills, but retain the latest clock.
            let boundary = (previous / interval + 1) * interval;
            self.sweep_dust(boundary)?;
        }
        // First observation and rewinds establish a fresh anchor without undoing state.
        self.dust.clock = Some(now_ms);
        Ok(())
    }

    fn sweep_dust(&mut self, now_ms: u64) -> Result<()> {
        let mut candidates: BTreeMap<PairIndex, Vec<(Address, Decimal)>> = BTreeMap::new();
        for (address, account) in &self.accounts {
            for (base, balance) in &account.balances {
                // Locked dust belongs to a resting order and is left alone.
                if balance.total <= ZERO || balance.hold > ZERO {
                    continue;
                }
                let Some(pair) = self.usdc_pairs.get(base) else {
                    continue;
                };
                let lot = Decimal::new(1, self.tokens[base].sz_decimals);
                if balance.total < lot {
                    candidates
                        .entry(*pair)
                        .or_default()
                        .push((*address, balance.total));
                }
            }
        }
        for (pair, balances) in candidates {
            self.sweep_pair_dust(pair, balances, now_ms)?;
        }
        Ok(())
    }

    fn sweep_pair_dust(
        &mut self,
        pair_index: PairIndex,
        candidates: Vec<(Address, Decimal)>,
        now_ms: u64,
    ) -> Result<()> {
        let pair = &self.pairs[&pair_index];
        let token = &self.tokens[&pair.base];
        let Some(book) = self.books.get(&pair_index) else {
            return Ok(());
        };
        let Some(mid) = book.mid()? else {
            return Ok(());
        };
        let cap = self.dust.cap();
        let mut recipients = Vec::with_capacity(candidates.len());
        let mut dust = ZERO;
        // Candidate order comes from the address-keyed map, never funding order.
        for (address, quantity) in candidates {
            if let Some(cap) = cap
                && mul(quantity, mid)? > cap
            {
                continue;
            }
            dust = add(dust, quantity)?;
            recipients.push((address, quantity));
        }
        let aggregate_cap = Decimal::from(if token.canonical && &*token.name == "PURR" {
            10_000
        } else {
            3_000
        });
        if recipients.is_empty() || mul(dust, mid)? > aggregate_cap {
            return Ok(());
        }
        let sell = dust.trunc_with_scale(token.sz_decimals);
        if sell == ZERO {
            for (address, _) in recipients {
                if let Some(account) = self.accounts.get_mut(&address) {
                    account.balances.remove(&pair.base);
                }
            }
            return Ok(());
        }
        let market = self.market(pair_index)?;
        let quote = &self.tokens[&pair.quote];
        let stable =
            self.quote_tokens.contains(&pair.base) && self.quote_tokens.contains(&pair.quote);
        let rate = mul(
            self.fees.system_spot_taker_rate(),
            if stable { Decimal::new(2, 1) } else { ONE },
        )?
        .to_f64()
        .ok_or("Simulator: fee rate out of range")?;
        let mut remaining = sell;
        let mut gross = ZERO;
        let mut fees = ZERO;
        let mut consumed = 0;
        let mut partial = None;
        let mut last_price = None;
        let mut tid = self.next_tid;
        let mut maker_drafts = BTreeMap::new();
        let mut maker_fills = Vec::new();
        for level in &book.bids {
            if remaining == ZERO {
                break;
            }
            let quantity = remaining.min(level.sz);
            let cost = mul(quantity, level.px)?;
            gross = add(gross, cost)?;
            fees = add(fees, quote_fee(cost, market.quote_units, rate)?)?;
            if level.owner.is_some() {
                maker_fills.push(self.settle_maker(
                    &mut maker_drafts,
                    &market,
                    level,
                    false,
                    quantity,
                    now_ms,
                    tid,
                    TxHash([0; 32]),
                )?);
                tid = tid.checked_add(1).ok_or("Simulator: trade id exhausted")?;
            }
            remaining = sub(remaining, quantity)?;
            let leftover = sub(level.sz, quantity)?;
            last_price = Some(level.px);
            if leftover == ZERO {
                consumed += 1;
            } else {
                partial = Some(leftover);
            }
        }
        let executed = sub(sell, remaining)?;
        if executed == ZERO {
            return Ok(());
        }
        let average = div(gross, executed)?;
        let net = sub(gross, fees)?.trunc_with_scale(quote.wei_decimals);
        if net < ZERO {
            return Err("Simulator: dust fee exceeds proceeds".into());
        }
        let oid = self.next_oid;
        let next_oid = oid.checked_add(1).ok_or("Simulator: order id exhausted")?;
        let mut payouts = Vec::with_capacity(recipients.len());
        let mut residual = net;
        let mut largest = ZERO;
        let mut residual_recipient = 0;
        for (index, (address, quantity)) in recipients.into_iter().enumerate() {
            let share = if quantity == dust {
                net
            } else {
                let numerator = mul(net, quantity)?;
                let mut share = div(numerator, dust)?.trunc_with_scale(quote.wei_decimals);
                // Decimal division rounds at its precision limit; do not let that
                // round a value just below a quote-wei boundary upward.
                if mul(share, dust)? > numerator {
                    share = sub(share, Decimal::new(1, quote.wei_decimals))?;
                }
                share
            };
            if share < ZERO || share > net {
                return Err("Simulator: dust allocation out of range".into());
            }
            residual = sub(residual, share)?;
            // Equal balances keep the first lexicographic address.
            if quantity > largest {
                largest = quantity;
                residual_recipient = index;
            }
            let fill = Fill {
                coin: pair.name.clone(),
                px: average,
                sz: quantity,
                side: Side::Sell,
                time: now_ms,
                start_position: quantity,
                dir: Direction::DustConversion,
                closed_pnl: ZERO,
                hash: TxHash([0; 32]),
                oid,
                crossed: true,
                fee: ZERO,
                tid: 0,
                fee_token: quote.name.clone(),
                twap_id: (),
                cloid: None,
            };
            payouts.push((address, quantity, share, fill));
        }
        if residual < ZERO {
            return Err("Simulator: dust allocation exceeds proceeds".into());
        }
        payouts[residual_recipient].2 = add(payouts[residual_recipient].2, residual)?;
        // Every fallible calculation precedes any balance, fill, or book mutation.
        // Makers settle first; a recipient who was also a maker then keeps the
        // base it just bought while its dust quantity is removed.
        let (base, quote) = (pair.base, pair.quote);
        self.commit_makers(maker_drafts, maker_fills, now_ms);
        for (address, quantity, share, fill) in payouts {
            let Some(account) = self.accounts.get_mut(&address) else {
                continue;
            };
            if let Some(balance) = account.balances.get_mut(&base) {
                balance.total = sub(balance.total, quantity)?;
                if balance.total == ZERO {
                    account.balances.remove(&base);
                }
            }
            if share != ZERO {
                let balance = account.balances.entry(quote).or_default();
                balance.total = add(balance.total, share)?;
            }
            account.fills.push(fill);
        }
        if let Some(book) = self.books.get_mut(&pair_index) {
            book.bids.drain(..consumed);
            if let Some(leftover) = partial
                && let Some(first) = book.bids.first_mut()
            {
                first.sz = leftover;
            }
        }
        self.next_oid = next_oid;
        self.next_tid = tid;
        if let Some(price) = last_price {
            self.mark_prices.insert(pair_index, price);
        }
        self.fire_triggers(pair_index, now_ms)?;
        Ok(())
    }
}
