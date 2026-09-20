//! Spot sends between accounts, spot withdrawals to external EVM chains, plus
//! the `/_test/fund` control that seeds accounts.

use super::account::{Delta, Draft, LedgerEntry, TokenIndex};
use super::{Balances, Engine, tx_hash};
use crate::{
    decimal::{self, ONE, ZERO, add, div, mul, parse_scientific, sub},
    error::Result,
    wire::{Address, Funding, FundingMode, SendAsset, SendToEvmWithData, TxHash},
};
use rust_decimal::Decimal;
use serde::Serialize;
use std::{iter, sync::Arc};

/// HyperCore's USDC system address, the ledger destination of a withdrawal.
pub const USDC_SYSTEM_ADDRESS: Address = Address([
    0x20, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
]);
/// HyperEVM gas for the 200k-gas withdrawal call, as measured on mainnet on
/// 2026-09-20: charged in HYPE when held, otherwise in USDC.
pub const EVM_SEND_GAS_HYPE: Decimal = Decimal::from_parts(2101, 0, 0, false, 8);
pub const EVM_SEND_GAS_USDC: Decimal = Decimal::from_parts(1822, 0, 0, false, 6);

/// One spot withdrawal to an external EVM chain, as `/_test/evm_sends` reports it.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct EvmSend {
    pub from: Address,
    pub destination: Address,
    pub destination_chain_id: u32,
    #[serde(with = "decimal::text")]
    pub amount: Decimal,
    pub nonce: u64,
    pub time: u64,
}

pub(super) struct Transfer {
    pub sender: Address,
    pub destination: Address,
    pub token: TokenIndex,
    pub quantity: Decimal,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct FundReply<'a> {
    ok: bool,
    address: Address,
    #[serde(with = "decimal::text")]
    activation_fee: Decimal,
    state: Balances<'a>,
}

impl Engine {
    pub(super) fn send_asset(
        &mut self,
        action: &SendAsset,
        signer: Address,
        nonce: u64,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<()> {
        if action.source_dex != "spot" || action.destination_dex != "spot" {
            return Err("Invalid perp DEX".into());
        }
        if !action.from_sub_account.is_empty() {
            return Err("Simulator: subaccounts are unsupported".into());
        }
        let destination = Address::parse(&action.destination)?;
        let token = self
            .token_by_name(&action.token, true)
            .map_err(|_| format!("Unknown token {}", action.token))?;
        let quantity = parse_scientific(&action.amount)?;
        if quantity == ZERO {
            return Err("Send amount cannot be zero".into());
        }
        if quantity < ZERO || quantity.normalize().scale() > token.wei_decimals {
            return Err("Invalid number of decimals".into());
        }
        let transfer = Transfer {
            sender: signer,
            destination,
            token: token.index,
            quantity,
        };
        self.transfer(transfer, nonce, now_ms, tx_hash(raw, nonce))
    }

    pub(super) fn send_to_evm(
        &mut self,
        action: &SendToEvmWithData,
        signer: Address,
        nonce: u64,
        now_ms: u64,
        raw: &[u8],
    ) -> Result<()> {
        if action.source_dex != "spot" {
            return Err("Simulator: only spot withdrawals to EVM are supported".into());
        }
        if action.address_encoding != "hex" {
            return Err("Simulator: only hex address encoding is supported".into());
        }
        if action.data != "0x" {
            return Err("Simulator: custom hook data is unsupported".into());
        }
        let destination = Address::parse(&action.destination_recipient)?;
        let token = self
            .token_by_name(&action.token, true)
            .map_err(|_| format!("Unknown token {}", action.token))?;
        if token.index != self.usdc {
            return Err("Simulator: only USDC withdraws to EVM".into());
        }
        let quantity = parse_scientific(&action.amount)?;
        if quantity == ZERO {
            return Err("Send amount cannot be zero".into());
        }
        if quantity < ZERO || quantity.normalize().scale() > token.wei_decimals {
            return Err("Invalid number of decimals".into());
        }
        let usdc = self.usdc;
        let source = self
            .accounts
            .get(&signer)
            .ok_or("Simulator: sender does not exist")?;
        if source.available(usdc) < quantity {
            return Err("Insufficient balance for token transfer".into());
        }
        let hype = self
            .tokens
            .values()
            .find(|token| &*token.name == "HYPE")
            .map(|token| token.index);
        let (fee_token, fee, native_token_fee) = match hype {
            Some(hype) if source.available(hype) >= EVM_SEND_GAS_HYPE => {
                (hype, ZERO, EVM_SEND_GAS_HYPE)
            }
            _ if source.available(usdc) - quantity >= EVM_SEND_GAS_USDC => {
                (usdc, EVM_SEND_GAS_USDC, ZERO)
            }
            _ => return Err("Insufficient USDC or HYPE balance for token transfer gas.".into()),
        };
        let mut debit = Draft::new(Some(source), [usdc, fee_token]);
        debit.credit(usdc, -quantity)?;
        debit.credit(
            fee_token,
            -(if fee_token == usdc {
                fee
            } else {
                native_token_fee
            }),
        )?;
        if fee_token != usdc {
            let prior = source
                .balances
                .get(&fee_token)
                .ok_or("Simulator: missing gas balance")?;
            let remaining = debit.balance(fee_token);
            remaining.entry =
                div(mul(prior.entry, remaining.total)?, prior.total)?.trunc_with_scale(8);
        }
        let entry = LedgerEntry {
            time: now_ms,
            hash: tx_hash(raw, nonce),
            delta: Delta::Send {
                user: signer,
                destination: USDC_SYSTEM_ADDRESS,
                source_dex: "spot",
                destination_dex: "spot",
                token: token.name.clone(),
                amount: quantity,
                usdc_value: quantity,
                fee,
                native_token_fee,
                nonce,
                fee_token: Arc::from(if fee == ZERO { "" } else { "USDC" }),
            },
        };
        let account = self
            .accounts
            .get_mut(&signer)
            .ok_or("Simulator: sender does not exist")?;
        debit.commit(account);
        account.ledger.push(entry);
        self.evm_sends.push(EvmSend {
            from: signer,
            destination,
            destination_chain_id: action.destination_chain_id,
            amount: quantity,
            nonce,
            time: now_ms,
        });
        Ok(())
    }

    /// Move `quantity` and charge the activation gas that a fresh recipient incurs.
    pub(super) fn transfer(
        &mut self,
        transfer: Transfer,
        nonce: u64,
        now_ms: u64,
        hash: TxHash,
    ) -> Result<()> {
        let Transfer {
            sender,
            destination,
            token: token_index,
            quantity,
        } = transfer;
        if sender == destination {
            return Err("Invalid send".into());
        }
        let usdc = self.usdc;
        let token = &self.tokens[&token_index];
        let fee = if self.accounts.contains_key(&destination) {
            ZERO
        } else {
            ONE
        };
        let source = self
            .accounts
            .get(&sender)
            .ok_or("Simulator: sender does not exist")?;
        // Funds locked by resting orders cannot be sent.
        if source.available(token_index) < quantity {
            return Err("Insufficient balance for token transfer".into());
        }
        let fee_token = if fee == ZERO {
            usdc
        } else if token_index == usdc || self.quote_tokens.contains(&token_index) {
            if source.available(token_index) - quantity < fee {
                return Err(format!(
                    "Insufficient {} balance for token transfer gas.",
                    token.name
                )
                .into());
            }
            token_index
        } else {
            let quotes = self
                .quote_tokens
                .iter()
                .copied()
                .filter(|index| *index != usdc);
            iter::once(usdc)
                .chain(quotes)
                .find(|index| source.available(*index) >= fee)
                .ok_or("Insufficient quote token (e.g., 1 USDC or USDT) balance for token transfer gas.")?
        };
        let fee_name: Arc<str> = if fee == ZERO {
            Arc::from("")
        } else {
            self.tokens[&fee_token].name.clone()
        };
        let valuation = self.usdc_value(token, quantity)?;
        let mut debit = Draft::new(Some(source), [token_index, fee_token]);
        debit.credit(token_index, -quantity)?;
        debit.credit(fee_token, -fee)?;
        // Debiting a non-USDC holding scales its cost basis down proportionally.
        let gas_debited = fee != ZERO && fee_token != token_index;
        let debited = iter::once(token_index).chain(gas_debited.then_some(fee_token));
        for index in debited.filter(|index| *index != usdc) {
            let prior = source
                .balances
                .get(&index)
                .ok_or("Simulator: missing transfer balance")?;
            let remaining = debit.balance(index);
            remaining.entry =
                div(mul(prior.entry, remaining.total)?, prior.total)?.trunc_with_scale(8);
        }
        let mut credit = Draft::new(self.accounts.get(&destination), [token_index]);
        credit.credit(token_index, quantity)?;
        if token_index != usdc {
            let received = credit.balance(token_index);
            received.entry = add(received.entry, valuation)?.trunc_with_scale(8);
        }
        let entry = LedgerEntry {
            time: now_ms,
            hash,
            delta: Delta::Send {
                user: sender,
                destination,
                source_dex: "spot",
                destination_dex: "spot",
                token: token.name.clone(),
                amount: quantity,
                usdc_value: valuation.trunc_with_scale(6),
                fee,
                native_token_fee: ZERO,
                nonce,
                fee_token: fee_name,
            },
        };
        let account = self
            .accounts
            .get_mut(&sender)
            .ok_or("Simulator: sender does not exist")?;
        debit.commit(account);
        account.ledger.push(entry.clone());
        let receiver = self.accounts.entry(destination).or_default();
        credit.commit(receiver);
        receiver.ledger.push(entry);
        Ok(())
    }

    pub(super) fn fund(
        &mut self,
        funding: &Funding,
        raw: &[u8],
        now_ms: u64,
    ) -> Result<FundReply<'_>> {
        let user = funding.address;
        let token = self.token_by_name(&funding.token, false)?;
        let quantity = funding.amount;
        if quantity.normalize().scale() > token.wei_decimals {
            return Err("Simulator: amount exceeds token weiDecimals".into());
        }
        let external = funding.mode == FundingMode::Transfer && funding.sender.is_none();
        if quantity < ZERO || (quantity == ZERO && !external) {
            return Err("Simulator: amount must be positive".into());
        }
        let basis = funding.entry_ntl.map(|text| text.0);
        if basis.is_some_and(|value| value < ZERO) || (basis.is_some() && !external) {
            return Err(
                "Simulator: entryNtl requires external transfer funding and a nonnegative value"
                    .into(),
            );
        }
        if funding.user_has_sent_tx.is_some() && !external {
            return Err("Simulator: userHasSentTx requires external transfer funding".into());
        }
        let fresh = !self.accounts.contains_key(&user);
        let fee = if fresh { ONE } else { ZERO };
        let usdc = self.usdc;
        let token_index = token.index;
        match funding.mode {
            FundingMode::Transfer => {
                if let Some(sender) = funding.sender {
                    let transfer = Transfer {
                        sender,
                        destination: user,
                        token: token_index,
                        quantity,
                    };
                    self.transfer(transfer, now_ms, now_ms, tx_hash(raw, now_ms))?;
                } else {
                    let mut draft = Draft::new(self.accounts.get(&user), [token_index]);
                    let balance = draft.balance(token_index);
                    if let Some(basis) = basis {
                        balance.entry = add(balance.entry, basis)?;
                    }
                    draft.credit(token_index, quantity)?;
                    let entry = LedgerEntry {
                        time: now_ms,
                        hash: tx_hash(raw, now_ms),
                        delta: Delta::Send {
                            user: Address::ZERO,
                            destination: user,
                            source_dex: "spot",
                            destination_dex: "spot",
                            token: token.name.clone(),
                            amount: quantity,
                            usdc_value: self.usdc_value(token, quantity)?.trunc_with_scale(6),
                            fee,
                            native_token_fee: ZERO,
                            nonce: now_ms,
                            fee_token: Arc::from(if fee == ZERO { "" } else { "USDC" }),
                        },
                    };
                    let account = self.accounts.entry(user).or_default();
                    draft.commit(account);
                    account.ledger.push(entry);
                    if let Some(sent) = funding.user_has_sent_tx {
                        account.sent = sent;
                    }
                }
            }
            FundingMode::Deposit => {
                if funding.sender.is_some() {
                    return Err("Simulator: sender is only valid for transfer funding".into());
                }
                if token_index != usdc {
                    return Err("Simulator: deposit funding requires USDC".into());
                }
                let net = sub(quantity, fee)?;
                if net < ZERO {
                    return Err("Simulator: deposit must cover activation fee".into());
                }
                let mut draft = Draft::new(self.accounts.get(&user), [token_index]);
                draft.balance(token_index);
                draft.credit(token_index, net)?;
                let account = self.accounts.entry(user).or_default();
                draft.commit(account);
                account.ledger.push(LedgerEntry {
                    time: now_ms,
                    hash: tx_hash(raw, now_ms),
                    delta: Delta::Deposit { usdc: net },
                });
            }
        }
        if let Some(serialize) = funding.serialize_f64
            && let Some(account) = self.accounts.get_mut(&user)
        {
            account.serialize_f64 = serialize;
        }
        Ok(FundReply {
            ok: true,
            address: user,
            activation_fee: fee,
            state: self.balances(self.accounts.get(&user)),
        })
    }
}
