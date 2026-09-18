//! Order books: resting orders with price-time priority plus anonymous
//! synthetic depth injected by tests, and the `l2Book` view over them.

use super::{Engine, Pair, account::PairIndex};
use crate::{
    decimal::{self, Text, ZERO, add, div, mul, sub},
    error::Result,
    wire::{Address, BookSpec, LevelSpec},
};
use rust_decimal::Decimal;
use serde::Serialize;
use std::collections::BTreeMap;

/// One resting entry. Synthetic depth from `/_test/book` has no owner and may
/// stand for several orders (`n`); a user's order is exactly one.
#[derive(Clone, Copy, Debug)]
pub struct Resting {
    pub oid: u64,
    pub owner: Option<Address>,
    pub px: Decimal,
    pub sz: Decimal,
    pub n: u64,
}

impl Resting {
    pub fn synthetic(px: Decimal, sz: Decimal, n: u64) -> Self {
        Self {
            oid: 0,
            owner: None,
            px,
            sz,
            n,
        }
    }

    /// Whether a taker order at `px` on the given side crosses this entry.
    pub fn crossed_by(&self, buy: bool, px: Decimal) -> bool {
        if buy { self.px <= px } else { self.px >= px }
    }
}

#[derive(Clone, Debug, Default)]
pub struct Book {
    /// Descending by price, then arrival.
    pub bids: Vec<Resting>,
    /// Ascending by price, then arrival.
    pub asks: Vec<Resting>,
}

impl Book {
    pub fn mid(&self) -> Result<Option<Decimal>> {
        self.bids
            .first()
            .zip(self.asks.first())
            .map(|(bid, ask)| add(div(bid.px, Decimal::TWO)?, div(ask.px, Decimal::TWO)?))
            .transpose()
    }

    /// The side a taker of the given direction consumes.
    pub fn opposite(&self, buy: bool) -> &[Resting] {
        if buy { &self.asks } else { &self.bids }
    }

    pub fn opposite_mut(&mut self, buy: bool) -> &mut Vec<Resting> {
        if buy { &mut self.asks } else { &mut self.bids }
    }

    /// Queue a resting order behind everything at the same or a better price.
    pub fn rest(&mut self, buy: bool, entry: Resting) {
        let side = if buy { &mut self.bids } else { &mut self.asks };
        insert_by_priority(side, buy, entry);
    }

    pub fn remove(&mut self, buy: bool, oid: u64) -> Option<Resting> {
        let side = if buy { &mut self.bids } else { &mut self.asks };
        let position = side.iter().position(|entry| entry.oid == oid)?;
        Some(side.remove(position))
    }

    /// Replace synthetic depth, keeping users' resting orders in place.
    pub fn replace_synthetic(&mut self, bids: Vec<Resting>, asks: Vec<Resting>) {
        for (side, fresh, buy) in [(&mut self.bids, bids, true), (&mut self.asks, asks, false)] {
            let owned: Vec<Resting> = side
                .drain(..)
                .filter(|entry| entry.owner.is_some())
                .collect();
            *side = fresh;
            for entry in owned {
                insert_by_priority(side, buy, entry);
            }
        }
    }
}

fn insert_by_priority(side: &mut Vec<Resting>, buy: bool, entry: Resting) {
    let position = side
        .iter()
        .position(|other| {
            if buy {
                other.px < entry.px
            } else {
                other.px > entry.px
            }
        })
        .unwrap_or(side.len());
    side.insert(position, entry);
}

#[derive(Serialize)]
pub struct L2Book<'a> {
    coin: &'a str,
    time: u64,
    levels: [Vec<LevelRow>; 2],
    /// Raw top-of-book spread, measured only on aggregated views. Its value
    /// with an empty side is unmeasured, so it is omitted then.
    #[serde(skip_serializing_if = "Option::is_none")]
    spread: Option<Text>,
}

#[derive(Serialize)]
pub struct LevelRow {
    #[serde(with = "decimal::text")]
    px: Decimal,
    #[serde(with = "decimal::text")]
    sz: Decimal,
    n: u64,
}

fn bucket(px: Decimal, bid: bool, sig: u32, mantissa: u64) -> Result<Decimal> {
    let text = px.normalize().to_string();
    let (whole, fraction) = text.split_once('.').unwrap_or((&text, ""));
    let exponent = if whole != "0" {
        whole.len() as i32 - 1
    } else {
        -(fraction.bytes().take_while(|b| *b == b'0').count() as i32) - 1
    };
    let power = exponent - sig as i32 + 1;
    let unit = if power < 0 {
        Decimal::new(mantissa as i64, (-power).min(28) as u32)
    } else {
        let factor = 10_u64
            .checked_pow(power as u32)
            .ok_or("Simulator: aggregation price out of range")?;
        mul(Decimal::from(factor), Decimal::from(mantissa))?
    };
    let ratio = div(px, unit)?;
    mul(if bid { ratio.floor() } else { ratio.ceil() }, unit)
}

/// Group a side into at most 20 price levels, optionally at `sig` significant figures.
pub fn aggregate(
    levels: &[Resting],
    bids: bool,
    sig: Option<u32>,
    mantissa: u64,
) -> Result<Vec<LevelRow>> {
    let mut grouped: BTreeMap<Decimal, (Decimal, u64)> = BTreeMap::new();
    for level in levels {
        let px = match sig {
            Some(sig) => bucket(level.px, bids, sig, mantissa)?,
            None => level.px,
        };
        let row = grouped.entry(px).or_insert((ZERO, 0));
        row.0 = add(row.0, level.sz)?;
        row.1 = row
            .1
            .checked_add(level.n)
            .ok_or("Simulator: book count overflow")?;
    }
    let mut rows: Vec<_> = grouped
        .into_iter()
        .map(|(px, (sz, n))| LevelRow { px, sz, n })
        .collect();
    if bids {
        rows.reverse();
    }
    rows.truncate(20);
    Ok(rows)
}

impl Engine {
    pub(super) fn l2<'a>(
        &'a self,
        pair: &'a Pair,
        sig: Option<u32>,
        mantissa: u64,
        now_ms: u64,
    ) -> Result<L2Book<'a>> {
        let empty = Book::default();
        let book = self.books.get(&pair.index).unwrap_or(&empty);
        let spread = match (sig, book.bids.first(), book.asks.first()) {
            (Some(_), Some(bid), Some(ask)) => Some(Text(sub(ask.px, bid.px)?)),
            _ => None,
        };
        Ok(L2Book {
            coin: &pair.name,
            time: now_ms,
            levels: [
                aggregate(&book.bids, true, sig, mantissa)?,
                aggregate(&book.asks, false, sig, mantissa)?,
            ],
            spread,
        })
    }

    pub(super) fn set_book(&mut self, spec: &BookSpec, now_ms: u64) -> Result<L2Book<'_>> {
        let pair = self
            .pair_by_name(&spec.coin)
            .ok_or("Simulator: unknown spot pair")?;
        let (index, base) = (pair.index, self.token(pair.base));
        let mark = spec.mark_px.map(|mark| positive(mark.0)).transpose()?;
        let mark_only = spec.mid.is_none()
            && spec.depth.is_none()
            && spec.bids.is_none()
            && spec.asks.is_none();
        if let Some(mark) = mark
            && mark_only
        {
            self.mark_prices.insert(index, mark);
            self.fire_triggers(index, now_ms)?;
            return self.l2_of(index, now_ms);
        }
        let price_scale = 8 - base.sz_decimals;
        let (mut bids, mut asks) = if let Some(mid) = spec.mid {
            if spec.bids.is_some() || spec.asks.is_some() {
                return Err(
                    "Simulator: mid/depth and explicit levels are mutually exclusive".into(),
                );
            }
            let mid = positive(mid.0)?;
            let depth = positive(spec.depth.ok_or("Simulator: depth must be a string")?.0)?;
            if depth.normalize().scale() > base.sz_decimals {
                return Err("Simulator: depth exceeds szDecimals".into());
            }
            let tick = Decimal::new(1, price_scale);
            let step = mul(mid, Decimal::new(1, 3))?.max(tick);
            let (mut bids, mut asks) = (Vec::new(), Vec::new());
            for i in 1..=20 {
                let offset = mul(step, Decimal::from(i))?;
                let bid = sub(mid, offset)?.trunc_with_scale(price_scale);
                let ask = add(mid, offset)?.trunc_with_scale(price_scale);
                if bid > ZERO {
                    bids.push(Resting::synthetic(bid, depth, 1));
                }
                asks.push(Resting::synthetic(ask, depth, 1));
            }
            (bids, asks)
        } else {
            (
                parse_levels(spec.bids.as_deref(), "bids", base.sz_decimals)?,
                parse_levels(spec.asks.as_deref(), "asks", base.sz_decimals)?,
            )
        };
        bids.sort_by_key(|level| std::cmp::Reverse(level.px));
        asks.sort_by_key(|level| level.px);
        let mut book = self.books.get(&index).cloned().unwrap_or_default();
        book.replace_synthetic(bids, asks);
        if book
            .bids
            .first()
            .zip(book.asks.first())
            .is_some_and(|(bid, ask)| bid.px >= ask.px)
        {
            return Err("Simulator: book must not be crossed".into());
        }
        // Validate aggregation before replacing the existing book.
        aggregate(&book.bids, true, None, 1)?;
        aggregate(&book.asks, false, None, 1)?;
        self.books.insert(index, book);
        if let Some(mark) = mark {
            self.mark_prices.insert(index, mark);
        }
        self.fire_triggers(index, now_ms)?;
        self.l2_of(index, now_ms)
    }

    fn l2_of(&self, index: PairIndex, now_ms: u64) -> Result<L2Book<'_>> {
        self.l2(&self.pairs[&index], None, 1, now_ms)
    }
}

fn positive(value: Decimal) -> Result<Decimal> {
    if value <= ZERO {
        return Err("Simulator: amount must be positive".into());
    }
    Ok(value)
}

fn parse_levels(levels: Option<&[LevelSpec]>, key: &str, sz_decimals: u32) -> Result<Vec<Resting>> {
    levels
        .ok_or_else(|| format!("Simulator: {key} must be an array"))?
        .iter()
        .map(|level| {
            let px = positive(level.px)?;
            let sz = positive(level.sz)?;
            if sz.normalize().scale() > sz_decimals {
                return Err("Simulator: book size exceeds szDecimals".into());
            }
            if level.n == 0 {
                return Err("Simulator: n must be positive integer".into());
            }
            Ok(Resting::synthetic(px, sz, level.n))
        })
        .collect()
}
