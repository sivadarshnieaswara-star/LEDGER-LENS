"""Deterministic demo data: three independent exports of ~40 orders with
realistic formatting differences and one planted instance of each problem."""
from __future__ import annotations

import csv
import datetime as dt
import os
import random

AS_OF = dt.date(2026, 10, 15)
# The demo replays three daily close-outs so history shows issues appearing,
# escalating and auto-clearing as more data arrives.
SNAPSHOTS = [dt.date(2026, 10, 8), dt.date(2026, 10, 12), AS_OF]

# day-of-month → payout id. Expected settlement = last day + 2.
PAYOUTS = {**{d: "po_7Hq2A" for d in (1, 2, 3)}, **{d: "po_7Hq2B" for d in (4, 5, 6)},
           **{d: "po_7Hq2C" for d in (7, 8)}, **{d: "po_7Hq2D" for d in (9, 10)},
           11: "po_7Hq2E", 12: "po_7Hq2F", 13: "po_7Hq2F"}

SCENARIOS = {
    "1003": "duplicate webhook (same txn delivered twice)",
    "1005": "customer double-charged (two distinct txns)",
    "1008": "amount mismatch: gateway captured $5 less",
    "1011": "in ledger, never captured at gateway",
    "1014": "gateway reference garbled → fuzzy match on amount+date",
    "1017": "fee overcharged vs contract",
    "1002": "refunded, correctly recorded on both sides",
    "1020": "refunded at gateway but refund not posted in ledger",
    "1023": "ledger journal posted twice",
    "1032": "gateway ref garbled AND $4.20 short → suggested candidate for human review",
    "2001": "gateway charge with no ledger order",
    "po_7Hq2B": "bank narrative lacks payout id → matched on amount",
    "po_7Hq2C": "settled 4 days late",
    "po_7Hq2D": "bank short by one transaction's net",
    "po_7Hq2E": "overdue, never arrived",
    "po_7Hq2F": "recent, still in transit (not an exception)",
    "BNK-90417": "unidentified wire deposit",
}


def fee(gross: int) -> int:
    return (gross * 290 + 5000) // 10000 + 30


def d(cents: int) -> str:
    return f"{cents / 100:.2f}"


def generate(out_dir: str) -> dict[str, str]:
    rnd = random.Random(7)
    os.makedirs(out_dir, exist_ok=True)
    ledger, gateway, bank = [], [], []
    special = {"1011": 6650, "1014": 6321, "1008": 5000, "1032": 8420}
    orders = []
    n = 1000
    for day in range(1, 14):
        for k in range(3):
            n += 1
            oid = str(n)
            amt = special.get(oid, rnd.choice([1999, 2500, 4999, 7500, 12000, 899, 3450]))
            ts = dt.datetime(2026, 10, day, 9 + k * 3, rnd.randint(0, 59))
            orders.append((oid, ts, amt))

    def gw_row(txn, ref, ts, kind, gross, f=None, payout="", evt=None):
        f = fee(gross) if f is None and kind == "charge" else (f or 0)
        net = gross - f if kind == "charge" else -gross
        return {"txn_id": txn, "merchant_ref": ref, "created": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "type": kind, "gross": d(gross), "fee": d(f), "net": d(net), "currency": "USD",
                "payout_id": payout, "webhook_id": evt or f"evt_{txn[3:]}{rnd.randint(100, 999)}"}

    je = 5000
    for oid, ts, amt in orders:
        je += 1
        ledger.append({"entry_id": f"JE-{je}", "order_ref": f"ORD-{oid}",
                       "posted_at": ts.strftime("%d/%m/%Y %H:%M"), "type": "sale",
                       "amount": d(amt), "currency": "USD", "refund_of": ""})
        if oid == "1023":
            je += 1
            ledger.append({**ledger[-1], "entry_id": f"JE-{je}"})
        if oid == "1011":
            continue
        gts = ts + dt.timedelta(seconds=rnd.randint(5, 90))
        ref = {"1014": "cust-ref-88", "1032": "WEB-CHK-5521"}.get(oid, rnd.choice([f"ord_{oid}", f"ORD-{oid}", f"order #{oid}"]))
        gross = {"1008": amt - 500, "1032": amt - 420}.get(oid, amt)
        f = fee(gross) + 45 if oid == "1017" else None
        row = gw_row(f"ch_{oid}A", ref, gts, "charge", gross, f, PAYOUTS[ts.day])
        gateway.append(row)
        if oid == "1003":
            gateway.append({**row, "webhook_id": row["webhook_id"] + "-retry"})
        if oid == "1005":
            gateway.append(gw_row(f"ch_{oid}B", ref, gts + dt.timedelta(minutes=2), "charge",
                                  gross, None, PAYOUTS[ts.day]))

    # refunds
    for oid, day, ledger_too in (("1002", 4, True), ("1020", 10, False)):
        amt = next(a for o, _, a in orders if o == oid)
        rts = dt.datetime(2026, 10, day, 15, 12)
        gateway.append(gw_row(f"re_{oid}", f"ord_{oid}", rts, "refund", amt, 0, PAYOUTS[day]))
        if ledger_too:
            je += 1
            ledger.append({"entry_id": f"JE-{je}", "order_ref": f"ORD-{oid}",
                           "posted_at": rts.strftime("%d/%m/%Y %H:%M"), "type": "refund",
                           "amount": d(amt), "currency": "USD", "refund_of": f"ORD-{oid}"})
    # orphan gateway charge
    gateway.append(gw_row("ch_2001A", "ord_2001", dt.datetime(2026, 10, 6, 18, 40), "charge",
                          4410, None, PAYOUTS[6]))

    # bank: settle payouts from the gateway's own (deduplicated) numbers
    seen, totals = set(), {}
    for g in gateway:
        if g["txn_id"] in seen:
            continue
        seen.add(g["txn_id"])
        totals[g["payout_id"]] = totals.get(g["payout_id"], 0) + round(float(g["net"]) * 100)
    short = next(round(float(g["net"]) * 100) for g in gateway if g["txn_id"] == "ch_1027A")
    plan = [("po_7Hq2A", dt.date(2026, 10, 5), "STRIPE PAYOUT po_7Hq2A", 0),
            ("po_7Hq2B", dt.date(2026, 10, 8), "STRIPE TRANSFER", 0),
            ("po_7Hq2C", dt.date(2026, 10, 14), "STRIPE PAYOUT po_7Hq2C", 0),
            ("po_7Hq2D", dt.date(2026, 10, 12), "STRIPE PAYOUT po_7Hq2D", -short)]
    for i, (pid, vd, desc, adj) in enumerate(plan):
        bank.append({"bank_ref": f"BNK-9040{i + 1}", "value_date": vd.isoformat(),
                     "description": desc, "amount": d(totals[pid] + adj), "currency": "USD"})
    bank.append({"bank_ref": "BNK-90417", "value_date": "2026-10-09",
                 "description": "WIRE IN ACME TRADING LTD INV 3381", "amount": "250.00", "currency": "USD"})
    bank.append({"bank_ref": "BNK-90418", "value_date": "2026-10-10",
                 "description": "MONTHLY ACCOUNT FEE", "amount": "-15.00", "currency": "USD"})

    rnd.shuffle(gateway)   # exports never arrive in a convenient order
    paths = {}
    for name, rows in (("ledger", ledger), ("gateway", gateway), ("bank", bank)):
        p = os.path.join(out_dir, f"{name}.csv")
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        paths[name] = p
    return paths
