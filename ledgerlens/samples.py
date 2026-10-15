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
    # second gateway (Paystream, 2.5 % + 25¢) settling into a second bank (Harbor Bank)
    "3001": "split capture: two Paystream charges add up to one order (many-to-one)",
    "3002-4": "consolidated checkout: one Paystream charge pays three orders (one-to-many)",
    "3005": "blank gateway reference → composite match on customer + amount + date",
    "3006": "partial refund, recorded on both sides",
    "3007": "refunded AND charged back → over-refund + unrecorded, open chargeback",
    "3008": "chargeback won: chargeback + reversal on both sides (tracked, no exception)",
    "3009": "open chargeback, recorded in ledger, evidence deadline approaching",
    "PS-STL-01": "settled in two bank deposits (many-to-one)",
    "PS-STL-02+03": "one batched bank deposit settles two payouts (one-to-many), 02 late",
    "PS-STL-04": "half received so far → partially settled",
}


def fee(gross: int) -> int:
    return (gross * 290 + 5000) // 10000 + 30


def d(cents: int) -> str:
    return f"{cents / 100:.2f}"


def ps_fee(gross: int) -> int:
    return (gross * 250 + 5000) // 10000 + 25


def generate(out_dir: str) -> dict:
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
                "payout_id": payout, "webhook_id": evt or f"evt_{txn[3:]}{rnd.randint(100, 999)}",
                "gateway": "NovaPay"}

    je = 5000
    for oid, ts, amt in orders:
        je += 1
        ledger.append({"entry_id": f"JE-{je}", "order_ref": f"ORD-{oid}",
                       "posted_at": ts.strftime("%d/%m/%Y %H:%M"), "type": "sale",
                       "amount": d(amt), "currency": "USD", "refund_of": "", "customer": f"cus_{oid}"})
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
                           "amount": d(amt), "currency": "USD", "refund_of": f"ORD-{oid}",
                           "customer": f"cus_{oid}"})
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
    plan = [("po_7Hq2A", dt.date(2026, 10, 5), "NOVAPAY PAYOUT po_7Hq2A", 0),
            ("po_7Hq2B", dt.date(2026, 10, 8), "NOVAPAY TRANSFER", 0),
            ("po_7Hq2C", dt.date(2026, 10, 14), "NOVAPAY PAYOUT po_7Hq2C", 0),
            ("po_7Hq2D", dt.date(2026, 10, 12), "NOVAPAY PAYOUT po_7Hq2D", -short)]
    NB = "Northbank Operating"
    for i, (pid, vd, desc, adj) in enumerate(plan):
        bank.append({"bank_ref": f"BNK-9040{i + 1}", "value_date": vd.isoformat(), "description": desc,
                     "amount": d(totals[pid] + adj), "currency": "USD", "account": NB})
    bank.append({"bank_ref": "BNK-90417", "value_date": "2026-10-09", "account": NB,
                 "description": "WIRE IN ACME TRADING LTD INV 3381", "amount": "250.00", "currency": "USD"})
    bank.append({"bank_ref": "BNK-90418", "value_date": "2026-10-10", "account": NB,
                 "description": "MONTHLY ACCOUNT FEE", "amount": "-15.00", "currency": "USD"})

    # ---------------- Paystream (second gateway) → Harbor Bank (second account)
    ps, harbor = [], []
    ps_payout = lambda day: ("PS-STL-01" if day <= 5 else "PS-STL-02" if day <= 7
                             else "PS-STL-03" if day <= 9 else "PS-STL-04")

    def ps_row(txn, ref, ts, kind, gross, cust, f=None):
        if f is None:
            f = ps_fee(gross) if kind == "charge" else (1500 if kind == "chargeback" else 0)
        net = {"charge": gross - f, "refund": -gross, "chargeback": -(gross + f),
               "chargeback_reversal": gross}[kind]
        ps.append({"transaction_id": txn, "reference": ref, "created_at": ts.isoformat(timespec="seconds"),
                   "type": kind, "gross": d(gross), "fees": d(f), "net": d(net), "currency": "USD",
                   "settlement_id": ps_payout(ts.day), "customer": cust, "gateway": "Paystream"})

    def led(ref, ts, kind, amt, cust):
        nonlocal je
        je += 1
        ledger.append({"entry_id": f"JE-{je}", "order_ref": ref, "posted_at": ts.strftime("%d/%m/%Y %H:%M"),
                       "type": kind, "amount": d(amt), "currency": "USD", "refund_of": "", "customer": cust})

    T = lambda day, h=11, m=0: dt.datetime(2026, 10, day, h, m)
    # plain volume
    for i, (day, amt) in enumerate([(2, 31500), (3, 26000), (5, 41000), (6, 28800), (7, 35500),
                                    (8, 39900), (8, 27500), (9, 44000), (9, 31000), (11, 29900),
                                    (12, 38800), (13, 25500)]):
        oid = 3010 + i
        led(f"ORD-{oid}", T(day, 10), "sale", amt, f"cus_{oid}")
        ps_row(f"ps_txn_{oid}", f"ORD-{oid}", T(day, 10, 1), "charge", amt, f"cus_{oid}")
    # 3001 split capture
    led("ORD-3001", T(3, 14), "sale", 15000, "cus_301")
    ps_row("ps_txn_3001a", "ORD-3001", T(3, 14, 1), "charge", 10000, "cus_301")
    ps_row("ps_txn_3001b", "ORD-3001", T(3, 14, 3), "charge", 5000, "cus_301")
    # 3002-3004 consolidated checkout
    for oid, amt in (("3002", 3000), ("3003", 4550), ("3004", 2450)):
        led(f"ORD-{oid}", T(4, 9, 30), "sale", amt, "cus_342")
    ps_row("ps_txn_cart77", "CART-77", T(4, 9, 31), "charge", 10000, "cus_342")
    # 3005 composite
    led("ORD-3005", T(5, 16), "sale", 7777, "cus_355")
    ps_row("ps_txn_3005", "", T(5, 16, 2), "charge", 7777, "cus_355")
    # 3006 partial refund
    led("ORD-3006", T(6, 12), "sale", 20000, "cus_306")
    ps_row("ps_txn_3006", "ORD-3006", T(6, 12, 1), "charge", 20000, "cus_306")
    led("ORD-3006", T(8, 9), "refund", 5000, "cus_306")
    ps_row("ps_re_3006", "ORD-3006", T(8, 9, 2), "refund", 5000, "cus_306")
    # 3007 refund + chargeback = over-refund
    led("ORD-3007", T(6, 15), "sale", 8000, "cus_307")
    ps_row("ps_txn_3007", "ORD-3007", T(6, 15, 1), "charge", 8000, "cus_307")
    led("ORD-3007", T(7, 10), "refund", 8000, "cus_307")
    ps_row("ps_re_3007", "ORD-3007", T(7, 10, 1), "refund", 8000, "cus_307")
    ps_row("ps_cb_3007", "ORD-3007", T(9, 8), "chargeback", 8000, "cus_307")
    # 3008 chargeback won
    led("ORD-3008", T(7, 13), "sale", 12000, "cus_308")
    ps_row("ps_txn_3008", "ORD-3008", T(7, 13, 1), "charge", 12000, "cus_308")
    led("ORD-3008", T(8, 16), "chargeback", 12000, "cus_308")
    ps_row("ps_cb_3008", "ORD-3008", T(8, 16), "chargeback", 12000, "cus_308")
    led("ORD-3008", T(12, 10), "chargeback_reversal", 12000, "cus_308")
    ps_row("ps_rv_3008", "ORD-3008", T(12, 10), "chargeback_reversal", 12000, "cus_308")
    # 3009 open chargeback, booked in ledger
    led("ORD-3009", T(11, 9), "sale", 6400, "cus_309")
    ps_row("ps_txn_3009", "ORD-3009", T(11, 9, 1), "charge", 6400, "cus_309")
    led("ORD-3009", T(12, 17), "chargeback", 6400, "cus_309")
    ps_row("ps_cb_3009", "ORD-3009", T(12, 17), "chargeback", 6400, "cus_309")

    pst = {}
    for r in ps:
        pst[r["settlement_id"]] = pst.get(r["settlement_id"], 0) + round(float(r["net"]) * 100)
    HB = "Harbor Bank Settlement"
    one = pst["PS-STL-01"]
    harbor += [
        {"bank_ref": "HB-1101", "value_date": "2026-10-07", "amount": d(one * 6 // 10),
         "description": "PAYSTREAM SETTLEMENT PS-STL-01 PART 1/2"},
        {"bank_ref": "HB-1102", "value_date": "2026-10-08", "amount": d(one - one * 6 // 10),
         "description": "PAYSTREAM SETTLEMENT PS-STL-01 PART 2/2"},
        {"bank_ref": "HB-1103", "value_date": "2026-10-11", "amount": d(pst["PS-STL-02"] + pst["PS-STL-03"]),
         "description": "PAYSTREAM BATCH PS-STL-02 PS-STL-03"},
        {"bank_ref": "HB-1104", "value_date": "2026-10-14", "amount": d(pst["PS-STL-04"] // 2),
         "description": "PAYSTREAM SETTLEMENT PS-STL-04 PART 1/2"},
        {"bank_ref": "HB-1105", "value_date": "2026-10-10", "amount": "-9.00",
         "description": "WIRE RECEIPT FEES"}]
    for r in harbor:
        r.update({"currency": "USD", "account": HB})

    rnd.shuffle(gateway)   # exports never arrive in a convenient order
    rnd.shuffle(ps)
    os.makedirs(out_dir, exist_ok=True)
    for stale in ("gateway.csv", "bank.csv"):
        if os.path.exists(os.path.join(out_dir, stale)):
            os.remove(os.path.join(out_dir, stale))
    files = {"ledger.csv": ledger, "gateway_novapay.csv": gateway, "gateway_paystream.csv": ps,
             "bank_northbank.csv": bank, "bank_harbor.csv": harbor}
    written = {}
    for name, rows in files.items():
        p = os.path.join(out_dir, name)
        cols = list(dict.fromkeys(k for r in rows for k in r))
        with open(p, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        written[name] = p
    return {"ledger": written["ledger.csv"],
            "gateway": [written["gateway_novapay.csv"], written["gateway_paystream.csv"]],
            "bank": [written["bank_northbank.csv"], written["bank_harbor.csv"]]}
