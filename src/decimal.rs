//! Exact decimal arithmetic and the wire text form of decimals.
//!
//! `rust_decimal`'s checked operations round when a result exceeds 28 digits.
//! Accounting must never round silently, so every operation here either returns
//! the mathematically exact value or fails.

use crate::error::{Error, Result};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, Serializer};

pub const ZERO: Decimal = Decimal::ZERO;
pub const ONE: Decimal = Decimal::ONE;

/// Strict wire decimal: no surrounding whitespace, sign prefix `+`, or exponent.
pub fn parse(raw: &str) -> Result<Decimal> {
    if raw.is_empty() || raw.trim() != raw || raw.contains(['e', 'E', '+']) {
        return Err("Simulator: invalid decimal string".into());
    }
    Decimal::from_str_exact(raw).map_err(|_| "Simulator: decimal out of range".into())
}

/// The `sendAsset` amount grammar additionally accepts exponent notation.
pub fn parse_scientific(raw: &str) -> Result<Decimal> {
    fn inner(raw: &str) -> Option<Decimal> {
        if raw.is_empty() || raw.trim() != raw {
            return None;
        }
        if let Ok(value) = Decimal::from_str_exact(raw) {
            return Some(value);
        }
        let (mantissa, exponent) = raw.split_once(['e', 'E'])?;
        let mut scale = exponent.parse::<i64>().ok()?.checked_neg()?;
        let negative = mantissa.starts_with('-');
        let mantissa = mantissa.strip_prefix(['+', '-']).unwrap_or(mantissa);
        let mut coefficient = 0_i128;
        let mut zeros = 0_u32;
        let mut fractional = false;
        let mut digits = false;
        for byte in mantissa.bytes() {
            match byte {
                b'.' if !fractional => fractional = true,
                b'0'..=b'9' => {
                    digits = true;
                    if fractional {
                        scale = scale.checked_add(1)?;
                    }
                    if byte == b'0' {
                        if coefficient != 0 {
                            zeros = zeros.checked_add(1)?;
                        }
                    } else {
                        coefficient = coefficient
                            .checked_mul(10_i128.checked_pow(zeros.checked_add(1)?)?)?
                            .checked_add(i128::from(byte - b'0'))?;
                        zeros = 0;
                    }
                }
                _ => return None,
            }
        }
        if !digits {
            return None;
        }
        if coefficient == 0 {
            return Some(ZERO);
        }
        scale = scale.checked_sub(i64::from(zeros))?;
        if scale < 0 {
            let shift = u32::try_from(scale.checked_neg()?).ok()?;
            coefficient = coefficient.checked_mul(10_i128.checked_pow(shift)?)?;
            scale = 0;
        }
        let coefficient = if negative { -coefficient } else { coefficient };
        Decimal::try_from_i128_with_scale(coefficient, u32::try_from(scale).ok()?).ok()
    }
    inner(raw).ok_or_else(|| "Invalid decimal number".into())
}

fn exact(mut coefficient: i128, mut scale: u32) -> Result<Decimal> {
    while scale > 0 && coefficient % 10 == 0 {
        coefficient /= 10;
        scale -= 1;
    }
    if scale > 28 {
        return Err("Simulator: decimal precision out of range".into());
    }
    Decimal::try_from_i128_with_scale(coefficient, scale)
        .map_err(|_| "Simulator: decimal overflow".into())
}

fn overflow() -> Error {
    "Simulator: decimal overflow".into()
}

pub fn add(a: Decimal, b: Decimal) -> Result<Decimal> {
    let (a, b) = (a.normalize(), b.normalize());
    let scale = a.scale().max(b.scale());
    let left = a
        .mantissa()
        .checked_mul(10_i128.pow(scale - a.scale()))
        .ok_or_else(overflow)?;
    let right = b
        .mantissa()
        .checked_mul(10_i128.pow(scale - b.scale()))
        .ok_or_else(overflow)?;
    exact(left.checked_add(right).ok_or_else(overflow)?, scale)
}

pub fn sub(a: Decimal, b: Decimal) -> Result<Decimal> {
    add(a, -b)
}

pub fn mul(a: Decimal, b: Decimal) -> Result<Decimal> {
    if a.is_zero() || b.is_zero() {
        return Ok(ZERO);
    }
    let (a, b) = (a.normalize(), b.normalize());
    let (mut left, mut right) = (a.mantissa(), b.mantissa());
    let mut scale = a.scale() + b.scale();
    // Cancel exact powers of ten before multiplying, never round them away.
    for (left_factor, right_factor) in [(2, 5), (5, 2)] {
        while scale > 0 && left % left_factor == 0 && right % right_factor == 0 {
            left /= left_factor;
            right /= right_factor;
            scale -= 1;
        }
    }
    exact(left.checked_mul(right).ok_or_else(overflow)?, scale)
}

/// Division rounds at 28 digits; callers truncate to the token's precision.
pub fn div(a: Decimal, b: Decimal) -> Result<Decimal> {
    a.checked_div(b)
        .ok_or_else(|| "Simulator: decimal division out of range".into())
}

/// Hyperliquid's textual form: normalized, always with a fractional part.
pub fn wire(value: Decimal) -> String {
    let text = value.normalize().to_string();
    if text.contains('.') {
        text
    } else {
        format!("{text}.0")
    }
}

/// A decimal in its wire text form, for fields using `#[serde(with = "decimal::text")]`.
pub mod text {
    use super::*;

    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&wire(*value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        crate::wire::from_str(deserializer, parse)
    }
}

/// An optional decimal field in wire text form.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Text(pub Decimal);

impl<'de> Deserialize<'de> for Text {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        text::deserialize(deserializer).map(Self)
    }
}

impl Serialize for Text {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        text::serialize(&self.0, serializer)
    }
}
