"""Bounded signed spot account, amount and nonce discrimination campaigns."""

import argparse
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

from hyperliquid.utils.signing import sign_send_asset_action

from . import Recorder
from .matrix import info
from .recorder import TESTNET, create, encode
from .scenarios import free_balance, load_meta, reserve_nonce, snapshot, submit, wallet


class Campaign:
    def __init__(self, args):
        self.args = args
        self.recorder = Recorder(args.root, TESTNET)
        self.a = wallet("HYPERLIQUID_PRIVATE_KEY")
        self.b = wallet("HYPERLIQUID_B_PRIVATE_KEY")
        self.c = wallet("HYPERLIQUID_C_PRIVATE_KEY")
        self.meta = load_meta(self.recorder)
        self.usdc = next(t for t in self.meta["tokens"] if t["name"] == "USDC")
        self.reserved = Decimal(0)
        self.results = []

    def observe(self, prefix, signer):
        return snapshot(self.recorder, prefix, signer.address)

    def send(
        self,
        case,
        signer,
        destination,
        amount="0.01",
        token=None,
        *,
        action_fields=None,
        envelope_fields=None,
        offset=0,
        reservation="0.01",
    ):
        reserve = Decimal(reservation)
        if reserve < 0 or self.reserved + reserve > self.args.max_spend:
            raise RuntimeError("Cumulative worst-case debit exceeds --max-spend")
        self.reserved += reserve
        nonce = reserve_nonce(
            self.args.nonce_file, signer.address, TESTNET, case, offset
        )
        action = {
            "type": "sendAsset",
            "destination": destination,
            "sourceDex": "spot",
            "destinationDex": "spot",
            "token": token or self.usdc["name"] + ":" + self.usdc["tokenId"],
            "amount": amount,
            "fromSubAccount": "",
            "nonce": nonce,
        }
        action.update(action_fields or {})
        signature = sign_send_asset_action(signer, action, False)
        envelope = {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }
        envelope.update(envelope_fields or {})
        result = submit(
            self.recorder,
            case,
            envelope,
            {
                "signer": signer.address.lower(),
                "recipient": destination,
                "intended_mutation": "bounded spot send if accepted",
                "reserved_maximum_debit": str(self.reserved),
            },
            True,
        )
        if result.status == 429:
            raise RuntimeError("Rate limited; halt without retry")
        self.results.append(
            {
                "case": case,
                "nonce": nonce,
                "status": result.status,
                "response": result.json,
            }
        )
        return envelope, result

    def replay(self, case, envelope):
        result = submit(
            self.recorder,
            case,
            deepcopy(envelope),
            {
                "intended_mutation": "explicit signed replay of zero-value or unknown-token rejection",
            },
            True,
        )
        if result.status == 429:
            raise RuntimeError("Rate limited; halt without retry")
        self.results.append(
            {
                "case": case,
                "nonce": envelope["nonce"],
                "status": result.status,
                "response": result.json,
            }
        )
        return result

    def admission(self):
        before = self.observe("admission-before-c", self.c)
        if before["role"] != {"role": "missing"}:
            raise RuntimeError(
                "Admission requires a fresh controlled C; never silently reuse"
            )
        source = self.observe("admission-before-a", self.a)
        if free_balance(source["balances"], self.usdc["index"]) < 3:
            raise RuntimeError("Need 3 USDC for bounded activation")
        _, funded = self.send(
            "activate-c", self.a, self.c.address.lower(), "2", reservation="3"
        )
        if funded.json != {"status": "ok", "response": {"type": "default"}}:
            raise RuntimeError("Activation unsuccessful; no subsequent writes")
        self.observe("admission-funded-c", self.c)
        rejected, _ = self.send(
            "c-first-rejected-token",
            self.c,
            self.a.address.lower(),
            token="INVALID:0x" + "0" * 32,
        )
        self.observe("admission-rejected-c", self.c)
        self.replay("c-first-rejected-replay", rejected)
        self.send("c-first-success-self", self.c, self.c.address.lower())
        self.observe("admission-success-c", self.c)
        self.observe("admission-after-a", self.a)

    def transfer(self):
        self.observe("transfer-before-a", self.a)
        self.observe("transfer-before-b", self.b)
        cases = (
            ("wei-unit", "0.00000001"),
            ("sub-wei-unit", "0.000000001"),
            ("scientific", "1e-8"),
            ("positive-sign", "+0.01"),
            ("leading-dot", ".01"),
            ("trailing-dot", "0."),
            ("leading-zero", "00.01"),
            ("trailing-zero", "0.010000000000"),
            ("negative-zero", "-0.00000000"),
            ("leading-space", " 0.01"),
        )
        for name, amount in cases:
            envelope, _ = self.send(
                "amount-" + name, self.a, self.b.address.lower(), amount
            )
            if name in ("sub-wei-unit", "negative-zero"):
                self.replay("amount-" + name + "-replay", envelope)
            info(
                self.recorder,
                "amount-" + name + "-balance",
                {"type": "spotClearinghouseState", "user": self.b.address},
            )
        token = self.usdc["name"] + ":" + self.usdc["tokenId"]
        for name, value in (
            ("lower-name", token.lower()),
            ("wrong-name-right-id", "WRONG:" + self.usdc["tokenId"]),
            (
                "uppercase-id",
                self.usdc["name"] + ":0x" + self.usdc["tokenId"][2:].upper(),
            ),
            ("bare-id", self.usdc["tokenId"]),
            ("bare-name", self.usdc["name"]),
        ):
            self.send("token-" + name, self.a, self.b.address.lower(), token=value)
        for name, destination in (
            ("checksum", self.b.address),
            ("without-prefix", self.b.address[2:]),
            ("self", self.a.address.lower()),
        ):
            self.send("destination-" + name, self.a, destination)
        self.send(
            "send-expiry-zero",
            self.a,
            self.b.address.lower(),
            envelope_fields={"expiresAfter": 0},
        )
        self.send(
            "send-vault-self",
            self.a,
            self.b.address.lower(),
            envelope_fields={"vaultAddress": self.a.address.lower()},
        )
        # User-signed action nonce is authenticated separately from the wrapper.
        outer = reserve_nonce(
            self.args.nonce_file, self.a.address, TESTNET, "send-outer-nonce"
        )
        self.send(
            "send-nonce-mismatch",
            self.a,
            self.b.address.lower(),
            envelope_fields={"nonce": outer},
        )
        self.observe("transfer-after-a", self.a)
        self.observe("transfer-after-b", self.b)

    def dust(self):
        purr = next(t for t in self.meta["tokens"] if t["name"] == "PURR")
        self.observe("dust-before-a", self.a)
        self.observe("dust-before-b", self.b)
        # A's earlier omitted0.9972 PURR is ground truth. This action distinguishes
        # hidden-but-spendable balance from genuine balance removal, not a recheck.
        self.send(
            "dust-spend-omitted-purr",
            self.a,
            self.b.address.lower(),
            "0.00001",
            token=purr["name"] + ":" + purr["tokenId"],
            reservation="0.01",
        )
        self.observe("dust-after-a", self.a)
        after = self.observe("dust-after-b", self.b)
        if free_balance(after["balances"], purr["index"]) >= Decimal("0.00001"):
            self.send(
                "dust-return-purr",
                self.b,
                self.a.address.lower(),
                "0.00001",
                token=purr["name"] + ":" + purr["tokenId"],
                reservation="0.01",
            )
            self.observe("dust-returned-a", self.a)
            self.observe("dust-returned-b", self.b)

    def nonce(self):
        before = self.observe("nonce-before-c", self.c)
        if before["role"] != {"role": "user"}:
            raise RuntimeError(
                "Nonce campaign requires existing C; run admission first"
            )
        envelopes = []
        for index in range(105):
            envelope, result = self.send(
                f"nonce-floor-seed-{index:03d}",
                self.c,
                self.a.address.lower(),
                "0",
                offset=-3_600_000,
                reservation="0",
            )
            if result.json != {
                "status": "err",
                "response": "Send amount cannot be zero",
            }:
                raise RuntimeError(
                    "Unexpected seed result; do not assume nonce admitted"
                )
            envelopes.append(envelope)
        self.replay("nonce-pruned-replay", envelopes[0])
        self.replay("nonce-retained-replay", envelopes[-1])
        self.send(
            "nonce-unused-below-floor",
            self.c,
            self.a.address.lower(),
            "0",
            offset=-7_200_000,
            reservation="0",
        )
        self.observe("nonce-after-c", self.c)

    def finish(self):
        create(self.recorder.root / "results.json", encode(self.results))
        self.recorder.event(
            {
                "state": "scenario_completed",
                "scenario": "account-parity",
                "reserved_maximum_debit": str(self.reserved),
            }
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cases", required=True)
    parser.add_argument("--max-spend", type=Decimal, default=Decimal(5))
    parser.add_argument(
        "--nonce-file", type=Path, default=Path("captures/testnet/nonces.jsonl")
    )
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    cases = args.cases.split(",")
    if (
        not args.execute
        or not cases
        or len(cases) != len(set(cases))
        or any(case not in ("admission", "transfer", "nonce", "dust") for case in cases)
    ):
        parser.error(
            "Explicit --execute and unique cases admission,transfer,nonce,dust required"
        )
    if not args.max_spend.is_finite() or not Decimal(0) <= args.max_spend <= Decimal(
        10
    ):
        parser.error("--max-spend must be finite and between0 and10USDC")
    campaign = Campaign(args)
    try:
        for case in cases:
            getattr(campaign, case)()
        campaign.finish()
    except (Exception, KeyboardInterrupt) as exc:
        campaign.recorder.event(
            {"state": "run_aborted", "reason": str(exc), "automatic_retry": False}
        )
        raise


if __name__ == "__main__":
    main()
