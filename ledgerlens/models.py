"""Canonical record types. All money is held as integer minor units (cents)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field


@dataclass
class LedgerEntry:
    id: str
    ref: str
    ts: dt.datetime
    kind: str            # "charge" | "refund" | "chargeback" | "chargeback_reversal"
    amount: int          # positive magnitude
    currency: str
    parent_ref: str = ""
    source_row: int = 0
    customer: str = ""


@dataclass
class GatewayEvent:
    id: str              # gateway transaction id
    ref: str             # merchant reference as the gateway reports it
    ts: dt.datetime
    kind: str            # "charge" | "refund" | "chargeback" | "chargeback_reversal"
    gross: int           # positive magnitude
    fee: int             # positive magnitude
    net: int             # signed contribution to the payout
    currency: str
    payout_id: str = ""
    webhook_id: str = ""
    source_row: int = 0
    gateway: str = "Gateway"
    customer: str = ""


@dataclass
class BankLine:
    id: str
    value_date: dt.date
    amount: int          # signed (deposits positive)
    currency: str
    description: str
    source_row: int = 0
    account: str = "Bank"


@dataclass
class Match:
    layer: str           # "ledger↔gateway" | "gateway↔bank"
    left: list[str]
    right: list[str]
    method: str
    confidence: float
    explanation: str
    factors: list[dict] = field(default_factory=list)   # explainable confidence breakdown


@dataclass
class Discrepancy:
    kind: str
    severity: str        # "high" | "medium" | "low"
    fingerprint: str     # stable across runs → carries resolution history forward
    summary: str
    explanation: str
    suggested_action: str
    refs: dict[str, list[str]] = field(default_factory=dict)
    amount_delta: int = 0
    currency: str = ""
    pair: str = ""                    # which reconciliation report it belongs to
    candidates: list[dict] = field(default_factory=list)   # probabilistic suggestions for review
