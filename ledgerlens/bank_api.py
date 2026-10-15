"""Bank API simulator.

Serves a bank settlement file through a JSON API shaped like a typical
open-banking transactions endpoint (string amounts, nested currency,
cursor pagination), and a client that ingests from it. This lets the
pipeline run against a "live" bank source instead of a CSV drop.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request

from .ingest import load_bank, load_many, parse_money
from .models import BankLine


def transactions(csv_paths, date_from: str | None, date_to: str | None,
                 cursor: int = 0, limit: int = 50, account: str | None = None) -> dict:
    """Simulated GET /sim/bank/v1/transactions across every connected account.
    Dates are inclusive / exclusive."""
    paths = [csv_paths] if isinstance(csv_paths, str) else csv_paths
    lines = sorted(load_many(load_bank, paths), key=lambda b: (b.value_date, b.id))
    if account:
        lines = [b for b in lines if b.account == account]
    lo = dt.date.fromisoformat(date_from) if date_from else dt.date.min
    hi = dt.date.fromisoformat(date_to) if date_to else dt.date.max
    sel = [b for b in lines if lo <= b.value_date < hi]
    page = sel[cursor:cursor + limit]
    nxt = cursor + limit if cursor + limit < len(sel) else None
    return {
        "data": [{
            "transactionId": b.id,
            "bookingDate": b.value_date.isoformat(),
            "valueDate": b.value_date.isoformat(),
            "transactionAmount": {"amount": f"{b.amount / 100:.2f}", "currency": b.currency},
            "creditDebitIndicator": "CRDT" if b.amount >= 0 else "DBIT",
            "remittanceInformationUnstructured": b.description,
            "account": {"name": b.account},
        } for b in page],
        "meta": {"total": len(sel), "next_cursor": nxt},
    }


def load_bank_api(base_url: str, date_to: dt.date | None = None, timeout: float = 10) -> list[BankLine]:
    """Pull every page from a bank transactions API into canonical BankLines."""
    out, cursor = [], 0
    while cursor is not None:
        q = {"cursor": cursor, "limit": 25}
        if date_to:
            q["to"] = date_to.isoformat()
        url = base_url.rstrip("/") + "/transactions?" + urllib.parse.urlencode(q)
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.load(resp)
        for t in body["data"]:
            amt = abs(parse_money(t["transactionAmount"]["amount"]))
            out.append(BankLine(
                id=t["transactionId"], value_date=dt.date.fromisoformat(t["valueDate"]),
                amount=amt if t["creditDebitIndicator"] == "CRDT" else -amt,
                currency=t["transactionAmount"]["currency"],
                description=t["remittanceInformationUnstructured"],
                account=(t.get("account") or {}).get("name") or "Bank API"))
        cursor = body["meta"]["next_cursor"]
    return out
