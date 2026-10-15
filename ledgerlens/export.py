"""CSV exports for finance teams."""
from __future__ import annotations

import csv
import io

EXC_COLS = ["fingerprint", "kind", "severity", "status", "pair", "summary", "amount", "currency",
            "assignee", "age_days", "records", "explanation", "suggested_action"]


def _amount(cents) -> str:
    return f"{(cents or 0) / 100:.2f}"


def exceptions_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXC_COLS)
    w.writeheader()
    for e in rows:
        w.writerow({**{k: e.get(k, "") for k in EXC_COLS}, "amount": _amount(e.get("amount_delta")),
                    "records": " ".join(f"{k}:{i}" for k, ids in e.get("refs", {}).items() for i in ids)})
    return buf.getvalue()


def report_csv(rep: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["LedgerLens reconciliation report"])
    w.writerow(["Source A", rep["source_a"]]); w.writerow(["Source B", rep["source_b"]])
    w.writerow(["Run", rep["run"]["id"]]); w.writerow(["As of", rep["run"]["as_of"]])
    w.writerow(["Matched %", f"{rep['matched_pct'] * 100:.1f}"])
    w.writerow(["Reconciled amount", _amount(rep["reconciled_amount"]), rep["currency"]])
    w.writerow(["Amount difference", _amount(rep["mismatched_amount"]), rep["currency"]])
    w.writerow([])
    w.writerow(["DISCREPANCIES"])
    w.writerow(["fingerprint", "kind", "severity", "status", "amount", "summary"])
    for d in rep["discrepancies"]:
        w.writerow([d["fingerprint"], d["kind"], d["severity"], d["status"], _amount(d["amount_delta"]),
                    d["summary"]])
    w.writerow([])
    w.writerow(["MATCHES"])
    w.writerow(["source_a_ids", "source_b_ids", "method", "confidence", "explanation"])
    for m in rep["matches"]:
        w.writerow([" ".join(m["left_ids"]), " ".join(m["right_ids"]), m["method"], m["confidence"],
                    m["explanation"]])
    return buf.getvalue()
