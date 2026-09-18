//! Cancels, modifies and the scheduled dead-man cancel.
//!
//! None of these paths were probed on testnet; they follow the documented API
//! and the recovered handlers (cancel by `(user, oid)`, modify as cancel plus
//! reinsert under a new oid, schedule-cancel gated on lifetime volume).

use super::{
    Engine,
    account::{OrderKind, OrderStatus, Side},
    order::OrderReply,
};
use crate::{
    decimal::{ZERO, mul, sub},
    error::{Error, Result},
    wire::{
        Address, BatchModifyAction, CancelAction, CancelByCloidAction, ModifyAction, OidOrCloid,
        OrderRequest, ScheduleCancelAction,
    },
};
use rust_decimal::Decimal;
use serde::Serialize;
use std::fmt::Display;

/// One entry of a cancel action's `statuses`.
#[derive(Serialize)]
#[serde(untagged)]
pub enum CancelReply {
    Success(&'static str),
    Error { error: Error },
}

fn missing(order: impl Display) -> Error {
    format!("Order {order}: Order was never placed, already canceled, or filled.").into()
}

impl Engine {
    /// Remove an open order of `owner`, releasing its lock. When `asset` is
    /// given it must name the order's market, as the API's cancel requires.
    pub(super) fn cancel_open(
        &mut self,
        owner: Address,
        oid: u64,
        asset: Option<u32>,
        status: OrderStatus,
        now_ms: u64,
    ) -> Result<()> {
        let account = self
            .accounts
            .get_mut(&owner)
            .ok_or("Simulator: missing signer")?;
        let record = account
            .orders
            .get_mut(&oid)
            .filter(|record| record.order.status == OrderStatus::Open)
            .ok_or_else(|| missing(oid))?;
        let detail = &record.order.order;
        if asset.is_some_and(|asset| u64::from(asset) != detail.pair.0 + 10000) {
            return Err(missing(oid));
        }
        let (pair, buy, kind, sz, px) = (
            detail.pair,
            detail.side == Side::Buy,
            detail.kind,
            detail.sz,
            detail.limit_px,
        );
        record.set_status(status, now_ms);
        match kind {
            OrderKind::Limit(_) => {
                if let Some(book) = self.books.get_mut(&pair) {
                    book.remove(buy, oid);
                }
                let market = &self.pairs[&pair];
                let (token, locked) = if buy {
                    (market.quote, mul(sz, px)?)
                } else {
                    (market.base, sz)
                };
                if let Some(balance) = self
                    .accounts
                    .get_mut(&owner)
                    .and_then(|account| account.balances.get_mut(&token))
                {
                    balance.hold = sub(balance.hold, locked)?.max(ZERO);
                }
            }
            OrderKind::Trigger { .. } => {
                if let Some(pending) = self.triggers.get_mut(&pair) {
                    pending.retain(|entry| *entry != (owner, oid));
                }
            }
        }
        Ok(())
    }

    pub(super) fn cancel_action(
        &mut self,
        action: &CancelAction,
        signer: Address,
        now_ms: u64,
    ) -> Vec<CancelReply> {
        action
            .cancels
            .iter()
            .map(|cancel| {
                match self.cancel_open(
                    signer,
                    cancel.o,
                    Some(cancel.a),
                    OrderStatus::Canceled,
                    now_ms,
                ) {
                    Ok(()) => CancelReply::Success("success"),
                    Err(error) => CancelReply::Error { error },
                }
            })
            .collect()
    }

    pub(super) fn cancel_by_cloid_action(
        &mut self,
        action: &CancelByCloidAction,
        signer: Address,
        now_ms: u64,
    ) -> Vec<CancelReply> {
        action
            .cancels
            .iter()
            .map(|cancel| {
                let oid = self
                    .accounts
                    .get(&signer)
                    .and_then(|account| account.cloids.get(&cancel.cloid).copied());
                let result = match oid {
                    Some(oid) => self.cancel_open(
                        signer,
                        oid,
                        Some(cancel.asset),
                        OrderStatus::Canceled,
                        now_ms,
                    ),
                    None => Err(missing(cancel.cloid)),
                };
                match result {
                    Ok(()) => CancelReply::Success("success"),
                    Err(error) => CancelReply::Error { error },
                }
            })
            .collect()
    }

    /// Replace one open order: the old one is canceled, the new one placed
    /// through the normal path under a fresh oid.
    fn modify_one(
        &mut self,
        target: &OidOrCloid,
        order: &OrderRequest,
        signer: Address,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<OrderReply> {
        let account = self
            .accounts
            .get(&signer)
            .ok_or("Simulator: missing signer")?;
        let oid = match target {
            OidOrCloid::Oid(oid) => Some(*oid),
            OidOrCloid::Cloid(cloid) => account.cloids.get(cloid).copied(),
        }
        .filter(|oid| {
            account
                .orders
                .get(oid)
                .is_some_and(|record| record.order.status == OrderStatus::Open)
        })
        .ok_or("Cannot modify canceled or filled order.")?;
        let pair = u64::from(order.a)
            .checked_sub(10000)
            .map(super::account::PairIndex)
            .and_then(|index| self.pairs.get(&index))
            .ok_or("Invalid spot")?
            .index;
        let existing = &account.orders[&oid].order.order;
        if existing.pair != pair {
            return Err("Cannot modify canceled or filled order.".into());
        }
        if (existing.side == Side::Buy) != order.b {
            return Err("Simulator: modify cannot change an order's side".into());
        }
        let prepared = self.prepare(order, pair)?;
        self.cancel_open(signer, oid, None, OrderStatus::Canceled, now_ms)?;
        self.place(prepared, signer, now_ms, raw)
    }

    pub(super) fn modify_action(
        &mut self,
        action: &ModifyAction,
        signer: Address,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<()> {
        match self.modify_one(&action.oid, &action.order, signer, now_ms, raw)? {
            OrderReply::Error(error) => Err(error),
            _ => Ok(()),
        }
    }

    pub(super) fn batch_modify_action(
        &mut self,
        action: &BatchModifyAction,
        signer: Address,
        now_ms: u64,
        raw: &[u8],
    ) -> Vec<OrderReply> {
        action
            .modifies
            .iter()
            .map(|modify| {
                self.modify_one(&modify.oid, &modify.order, signer, now_ms, raw)
                    .unwrap_or_else(OrderReply::Error)
            })
            .collect()
    }

    pub(super) fn schedule_cancel_action(
        &mut self,
        action: &ScheduleCancelAction,
        signer: Address,
        now_ms: u64,
    ) -> Result<()> {
        let account = self
            .accounts
            .get_mut(&signer)
            .ok_or("Simulator: missing signer")?;
        let Some(time) = action.time else {
            account.scheduled_cancel.deadline_ms = None;
            return Ok(());
        };
        if account.lifetime_volume < Decimal::from(1_000_000) {
            return Err("Cannot set scheduled cancel time until enough volume traded".into());
        }
        if time < now_ms.saturating_add(5000) {
            return Err(
                "Scheduled cancel time too early, must be at least 5 seconds from now.".into(),
            );
        }
        let today = (now_ms / 86_400_000) as i64;
        let (day, fired) = account.scheduled_cancel.fired;
        if day == today && fired >= 10 {
            return Err("Too many scheduled cancel triggers today".into());
        }
        account.scheduled_cancel.deadline_ms = Some(time);
        Ok(())
    }

    /// Fire every dead-man switch whose deadline has passed.
    pub(super) fn run_scheduled_cancels(&mut self, now_ms: u64) -> Result<()> {
        let today = (now_ms / 86_400_000) as i64;
        let due: Vec<Address> = self
            .accounts
            .iter()
            .filter(|(_, account)| {
                account
                    .scheduled_cancel
                    .deadline_ms
                    .is_some_and(|deadline| deadline <= now_ms)
            })
            .map(|(address, _)| *address)
            .collect();
        for owner in due {
            let open: Vec<u64> = self.accounts[&owner]
                .open_orders()
                .map(|(oid, _)| *oid)
                .collect();
            for oid in open {
                self.cancel_open(owner, oid, None, OrderStatus::ScheduledCancel, now_ms)?;
            }
            let account = self.accounts.get_mut(&owner).expect("account exists");
            let (day, fired) = account.scheduled_cancel.fired;
            account.scheduled_cancel.fired = if day == today {
                (day, fired + 1)
            } else {
                (today, 1)
            };
            account.scheduled_cancel.deadline_ms = None;
        }
        Ok(())
    }
}
