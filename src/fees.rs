//! External fee schedule and exchange volume, never a donor account's activity.
//!
//! The captured `userFees` document is validated through typed views. Fields the
//! simulator neither computes nor replaces are echoed verbatim.

use crate::{
    decimal::{self, ONE, ZERO, add, mul, wire},
    error::{Error, Result},
    wire::from_str,
};
use chrono::{DateTime, Days, NaiveDate, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize, de::Error as _};
use serde_json::{Map, Value};
use std::collections::BTreeMap;

const DAY_MS: u64 = 86_400_000;

/// An account's per-UTC-day volume, absent for unknown accounts.
pub type Daily<'a> = Option<&'a BTreeMap<i64, Decimal>>;
const REPLACED: [&str; 5] = [
    "dailyUserVlm",
    "userCrossRate",
    "userAddRate",
    "userSpotCrossRate",
    "userSpotAddRate",
];

#[derive(Clone, Debug)]
struct Rates {
    /// Wire text of cross, add, spotCross and spotAdd after the staking discount.
    user: [String; 4],
    spot_taker: Decimal,
    spot_maker: Decimal,
}

#[derive(Clone, Debug)]
struct VipTier {
    cutoff: Decimal,
    rates: Rates,
}

#[derive(Clone, Debug)]
pub struct FeeState {
    echo: Map<String, Value>,
    exchange: BTreeMap<NaiveDate, String>,
    base: Rates,
    system_spot_taker: Decimal,
    vip: Vec<VipTier>,
}

fn nonnegative<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    from_str(deserializer, |raw| {
        let value = decimal::parse(raw)?;
        if value < ZERO {
            return Err("Simulator: negative fee volume or cutoff".into());
        }
        Ok(value)
    })
}

fn discount<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    from_str(deserializer, |raw| {
        let value = decimal::parse(raw)?;
        if !(ZERO..=ONE).contains(&value) {
            return Err("Simulator: fee discount must be between zero and one".into());
        }
        Ok(value)
    })
}

/// Validate a nonnegative volume but keep its exact text for echoing.
fn nonnegative_text<'de, D: Deserializer<'de>>(deserializer: D) -> Result<String, D::Error> {
    from_str(deserializer, |raw| {
        if decimal::parse(raw)? < ZERO {
            return Err("Simulator: negative fee volume or cutoff".into());
        }
        Ok(raw.to_owned())
    })
}

fn date<'de, D: Deserializer<'de>>(deserializer: D) -> Result<NaiveDate, D::Error> {
    from_str(deserializer, |raw| {
        let date = NaiveDate::parse_from_str(raw, "%Y-%m-%d")
            .map_err(|_| "Simulator: invalid fee volume date")?;
        if date.format("%Y-%m-%d").to_string() != raw {
            return Err("Simulator: noncanonical fee volume date".into());
        }
        Ok(date)
    })
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)] // read only to validate the document
struct Source {
    daily_user_vlm: Vec<DayRow>,
    fee_schedule: Schedule,
    active_staking_discount: StakingRow,
    #[serde(deserialize_with = "discount")]
    active_referral_discount: Decimal,
    // Donor rates are validated, then replaced by the recipient's computed rates.
    #[serde(with = "decimal::text")]
    user_cross_rate: Decimal,
    #[serde(with = "decimal::text")]
    user_add_rate: Decimal,
    #[serde(with = "decimal::text")]
    user_spot_cross_rate: Decimal,
    #[serde(with = "decimal::text")]
    user_spot_add_rate: Decimal,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)] // read only to validate the document
struct DayRow {
    #[serde(deserialize_with = "date")]
    date: NaiveDate,
    // Donor activity is validated, but never transferred to the recipient.
    #[serde(deserialize_with = "nonnegative")]
    user_cross: Decimal,
    #[serde(deserialize_with = "nonnegative")]
    user_add: Decimal,
    #[serde(deserialize_with = "nonnegative_text")]
    exchange: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)] // read only to validate the document
struct Schedule {
    #[serde(flatten)]
    rates: RateRow,
    tiers: Tiers,
    #[serde(deserialize_with = "discount")]
    referral_discount: Decimal,
    staking_discount_tiers: Vec<StakingRow>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RateRow {
    #[serde(deserialize_with = "nonnegative")]
    cross: Decimal,
    #[serde(deserialize_with = "nonnegative")]
    add: Decimal,
    #[serde(deserialize_with = "nonnegative")]
    spot_cross: Decimal,
    #[serde(deserialize_with = "nonnegative")]
    spot_add: Decimal,
}

#[derive(Deserialize)]
#[allow(dead_code)] // read only to validate the document
struct Tiers {
    vip: Vec<VipRow>,
    mm: Vec<MakerRow>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct VipRow {
    #[serde(deserialize_with = "nonnegative")]
    ntl_cutoff: Decimal,
    #[serde(flatten)]
    rates: RateRow,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)] // read only to validate the document
struct MakerRow {
    #[serde(deserialize_with = "discount")]
    maker_fraction_cutoff: Decimal,
    #[serde(with = "decimal::text")]
    add: Decimal,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
#[allow(dead_code)] // read only to validate the document
struct StakingRow {
    #[serde(deserialize_with = "nonnegative")]
    bps_of_max_supply: Decimal,
    #[serde(deserialize_with = "discount")]
    discount: Decimal,
}

impl Rates {
    fn new(row: &RateRow, staking: Decimal, referral: Decimal) -> Result<Self> {
        let mut values = [row.cross, row.add, row.spot_cross, row.spot_add];
        for value in &mut values {
            *value = mul(*value, staking)?;
        }
        Ok(Self {
            spot_taker: mul(values[2], referral)?,
            spot_maker: values[3],
            user: values.map(wire),
        })
    }
}

impl<'de> Deserialize<'de> for FeeState {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let document = Value::deserialize(deserializer)?;
        let source = Source::deserialize(&document).map_err(D::Error::custom)?;
        let Value::Object(mut echo) = document else {
            return Err(D::Error::custom("Simulator: fees must be an object"));
        };
        for key in REPLACED {
            echo.remove(key);
        }
        Self::build(source, echo).map_err(D::Error::custom)
    }
}

impl FeeState {
    pub fn parse(json: &str) -> Result<Self> {
        serde_json::from_str(json).map_err(|error| Error::from(error.to_string()))
    }

    fn build(source: Source, echo: Map<String, Value>) -> Result<Self> {
        let staking = add(ONE, -source.active_staking_discount.discount)?;
        let referral = add(ONE, -source.active_referral_discount)?;
        let schedule = &source.fee_schedule;
        let mut vip = Vec::with_capacity(schedule.tiers.vip.len());
        for row in &schedule.tiers.vip {
            if vip
                .last()
                .is_some_and(|last: &VipTier| row.ntl_cutoff <= last.cutoff)
            {
                return Err("Simulator: VIP fee cutoffs must be strictly increasing".into());
            }
            vip.push(VipTier {
                cutoff: row.ntl_cutoff,
                rates: Rates::new(&row.rates, staking, referral)?,
            });
        }
        let mut exchange = BTreeMap::new();
        for row in source.daily_user_vlm {
            if exchange.insert(row.date, row.exchange).is_some() {
                return Err("Simulator: duplicate fee volume date".into());
            }
        }
        Ok(Self {
            echo,
            exchange,
            base: Rates::new(&schedule.rates, staking, referral)?,
            system_spot_taker: schedule.rates.spot_cross,
            vip,
        })
    }

    fn day(now_ms: u64) -> Result<(i64, NaiveDate)> {
        let millis = i64::try_from(now_ms).map_err(|_| "Simulator: fee time out of range")?;
        let date = DateTime::<Utc>::from_timestamp_millis(millis)
            .ok_or("Simulator: fee time out of range")?
            .date_naive();
        Ok(((now_ms / DAY_MS) as i64, date))
    }

    /// The tier for the fourteen completed UTC days of taker plus maker volume.
    fn rates(&self, cross: Daily<'_>, add_volume: Daily<'_>, day: i64) -> Result<&Rates> {
        let mut volume = ZERO;
        // Today is reported immediately but only enters tiers at next UTC midnight.
        for (_, value) in [cross, add_volume]
            .into_iter()
            .flatten()
            .flat_map(|daily| daily.range((day - 14)..day))
        {
            if *value < ZERO {
                return Err("Simulator: negative fee volume or cutoff".into());
            }
            volume = add(volume, *value)?;
        }
        Ok(self
            .vip
            .iter()
            .rev()
            .find(|tier| volume > tier.cutoff)
            .map_or(&self.base, |tier| &tier.rates))
    }

    pub fn response(
        &self,
        cross: Daily<'_>,
        add_volume: Daily<'_>,
        now_ms: u64,
    ) -> Result<FeeResponse<'_>> {
        let (day, today) = Self::day(now_ms)?;
        let rates = self.rates(cross, add_volume, day)?;
        let mut rows = Vec::with_capacity(15);
        for offset in (0..=14).rev() {
            let date = today
                .checked_sub_days(Days::new(offset))
                .ok_or("Simulator: fee date out of range")?;
            let volume = |daily: Daily<'_>| -> Result<Decimal> {
                let value = daily
                    .and_then(|daily| daily.get(&(day - offset as i64)))
                    .copied()
                    .unwrap_or(ZERO);
                if value < ZERO {
                    return Err("Simulator: negative fee volume or cutoff".into());
                }
                Ok(value)
            };
            rows.push(DayReport {
                date: date.format("%Y-%m-%d").to_string(),
                user_cross: wire(volume(cross)?),
                user_add: wire(volume(add_volume)?),
                exchange: self.exchange.get(&date).map_or("0.0", String::as_str),
            });
        }
        let [cross, add, spot_cross, spot_add] = &rates.user;
        Ok(FeeResponse {
            echo: &self.echo,
            daily_user_vlm: rows,
            user_cross_rate: cross,
            user_add_rate: add,
            user_spot_cross_rate: spot_cross,
            user_spot_add_rate: spot_add,
        })
    }

    pub fn system_spot_taker_rate(&self) -> Decimal {
        self.system_spot_taker
    }

    pub fn spot_taker_rate(
        &self,
        cross: Daily<'_>,
        add_volume: Daily<'_>,
        now_ms: u64,
    ) -> Result<Decimal> {
        let (day, _) = Self::day(now_ms)?;
        Ok(self.rates(cross, add_volume, day)?.spot_taker)
    }

    /// The maker rate after the staking discount; referral discounts apply to takers only.
    pub fn spot_maker_rate(
        &self,
        cross: Daily<'_>,
        add_volume: Daily<'_>,
        now_ms: u64,
    ) -> Result<Decimal> {
        let (day, _) = Self::day(now_ms)?;
        Ok(self.rates(cross, add_volume, day)?.spot_maker)
    }
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FeeResponse<'a> {
    #[serde(flatten)]
    echo: &'a Map<String, Value>,
    daily_user_vlm: Vec<DayReport<'a>>,
    user_cross_rate: &'a str,
    user_add_rate: &'a str,
    user_spot_cross_rate: &'a str,
    user_spot_add_rate: &'a str,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DayReport<'a> {
    date: String,
    user_cross: String,
    user_add: String,
    exchange: &'a str,
}
