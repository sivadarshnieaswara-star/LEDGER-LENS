"""CSV ingestion. Each source has its own column vocabulary; aliases map them
onto the canonical models so differently-shaped exports still load."""
from __future__ import annotations

import csv
import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from .models import BankLine, GatewayEvent, LedgerEntry

LEDGER_COLS = {
    "id": ["entry_id", "id", "journal_id"],
    "ref": ["order_ref", "reference", "order_id"],
    "ts": ["posted_at", "created_at", "date"],
    "kind": ["type", "kind", "entry_type"],
    "amount": ["amount", "value"],
    "currency": ["currency", "ccy"],
    "parent": ["refund_of", "parent_ref"],
    "customer": ["customer", "customer_id", "email"],
}
GATEWAY_COLS = {
    "id": ["txn_id", "transaction_id", "id"],
    "ref": ["merchant_ref", "reference", "order_ref", "metadata_order"],
    "ts": ["created", "created_at", "timestamp"],
    "kind": ["type", "kind"],
    "gross": ["gross", "amount"],
    "fee": ["fee", "fees"],
    "net": ["net"],
    "currency": ["currency"],
    "payout": ["payout_id", "settlement_id", "transfer_id"],
    "webhook": ["webhook_id", "event_id"],
    "gateway": ["gateway", "provider", "psp"],
    "customer": ["customer", "customer_id", "email"],
}
BANK_COLS = {
    "id": ["bank_ref", "id", "transaction_ref"],
    "date": ["value_date", "date", "booking_date"],
    "amount": ["amount", "credit"],
    "currency": ["currency"],
    "desc": ["description", "narrative", "details"],
    "account": ["account", "account_id", "iban", "bank_account"],
}

REFUND_WORDS = {"refund", "credit_note", "partial_refund"}
CHARGEBACK_WORDS = {"chargeback", "dispute", "dispute_lost"}
REVERSAL_WORDS = {"chargeback_reversal", "dispute_won", "reversal", "dispute_reversal"}
OUTFLOW = {"refund", "chargeback"}   # money leaving the merchant


class IngestError(ValueError):
    pass


def parse_money(raw: str) -> int:
    s = (raw or "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s)
    if not s:
        return 0
    try:
        cents = int((Decimal(s) * 100).quantize(Decimal("1")))
    except InvalidOperation as exc:
        raise IngestError(f"bad amount {raw!r}") from exc
    return -cents if neg else cents


def parse_ts(raw: str) -> dt.datetime:
    s = (raw or "").strip()
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y%m%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise IngestError(f"bad date {raw!r}")


def _reader(path: str, cols: dict[str, list[str]]):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rdr = csv.DictReader(fh)
        header = {h.strip().lower(): h for h in (rdr.fieldnames or [])}
        mapping = {}
        for key, aliases in cols.items():
            mapping[key] = next((header[a] for a in aliases if a in header), None)
        for n, row in enumerate(rdr, start=2):
            yield n, {k: (row.get(src) or "").strip() if src else "" for k, src in mapping.items()}


def _kind(raw: str, amount: int) -> str:
    w = raw.lower().strip()
    if w in CHARGEBACK_WORDS:
        return "chargeback"
    if w in REVERSAL_WORDS:
        return "chargeback_reversal"
    return "refund" if w in REFUND_WORDS or amount < 0 else "charge"


def _source_name(path: str, prefix: str) -> str:
    """'gateway_novapay.csv' → 'Novapay'; 'gateway.csv' → ''."""
    import os
    stem = os.path.splitext(os.path.basename(path))[0]
    rest = stem[len(prefix):].strip("_- ") if stem.lower().startswith(prefix) else ""
    return rest.replace("_", " ").title()


def load_ledger(path: str) -> list[LedgerEntry]:
    out = []
    for n, r in _reader(path, LEDGER_COLS):
        amt = parse_money(r["amount"])
        out.append(LedgerEntry(
            id=r["id"] or f"L{n}", ref=r["ref"], ts=parse_ts(r["ts"]),
            kind=_kind(r["kind"], amt), amount=abs(amt),
            currency=(r["currency"] or "USD").upper(), parent_ref=r["parent"], source_row=n,
            customer=r["customer"]))
    return out


def load_gateway(path: str, name: str = "") -> list[GatewayEvent]:
    name = name or _source_name(path, "gateway") or "Gateway"
    out = []
    for n, r in _reader(path, GATEWAY_COLS):
        gross = parse_money(r["gross"])
        kind = _kind(r["kind"], gross)
        fee = abs(parse_money(r["fee"]))
        if r["net"]:
            net = parse_money(r["net"])
        elif kind == "chargeback":
            net = -(abs(gross) + fee)
        elif kind in ("refund", "chargeback_reversal"):
            net = abs(gross)          # sign fixed below
        else:
            net = abs(gross) - fee
        if kind in OUTFLOW and net > 0:
            net = -net
        if kind == "chargeback_reversal" and net < 0:
            net = -net
        out.append(GatewayEvent(
            id=r["id"] or f"G{n}", ref=r["ref"], ts=parse_ts(r["ts"]), kind=kind,
            gross=abs(gross), fee=fee, net=net, currency=(r["currency"] or "USD").upper(),
            payout_id=r["payout"], webhook_id=r["webhook"], source_row=n,
            gateway=r["gateway"] or name, customer=r["customer"]))
    return out


def load_bank(path: str, name: str = "") -> list[BankLine]:
    name = name or _source_name(path, "bank") or "Bank"
    out = []
    for n, r in _reader(path, BANK_COLS):
        out.append(BankLine(
            id=r["id"] or f"B{n}", value_date=parse_ts(r["date"]).date(),
            amount=parse_money(r["amount"]), currency=(r["currency"] or "USD").upper(),
            description=r["desc"], source_row=n, account=r["account"] or name))
    return out


def load_many(loader, paths: list[str]) -> list:
    out = []
    for p in paths:
        out.extend(loader(p))
    return out
