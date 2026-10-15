"""Command-line entry point: `python -m ledgerlens <command>`."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

from . import samples
from .engine import Config, money
from .service import run_reconciliation
from .store import ActionError, Store

SEV_ICON = {"high": "●", "medium": "◐", "low": "○"}


def cmd_run(a) -> int:
    if not (a.bank or a.bank_api):
        print("error: give --bank CSV or --bank-api URL", file=sys.stderr)
        return 1
    as_of = dt.date.fromisoformat(a.as_of) if a.as_of else dt.date.today()
    cfg = Config(settlement_days=a.settlement_days, grace_days=a.grace_days,
                 fee_rate_bp=a.fee_bp, fee_fixed=a.fee_fixed)
    store = Store(a.db)
    run_id, res = run_reconciliation(store, a.ledger, a.gateway, bank_path=a.bank, bank_api=a.bank_api,
                                     as_of=as_of, config=cfg, snapshot=a.snapshot,
                                     label=getattr(a, "label", ""))
    s = res.stats
    print(f"Run #{run_id}  as of {as_of}")
    print(f"  records   ledger {s['records']['ledger']} · gateway {s['records']['gateway']} "
          f"({s['records']['gateway_unique']} unique) · bank {s['records']['bank']}")
    print(f"  matched   {s['ledger_match_rate']:.1%} of ledger · {s['matches']['exact']} exact, "
          f"{s['matches']['fuzzy']} fuzzy · payouts {s['payouts']}")
    print(f"  issues    {s['discrepancies']} ({', '.join(f'{v} {k}' for k, v in s['by_severity'].items())})"
          f" · exposure {money(s['exposure'], 'USD')}\n")
    for r in res.reports:
        print(f"  report    {r['source_a']} ↔ {r['source_b']}: {r['matched_pct']:.1%} clean, "
              f"{money(r['mismatched_amount'], r['currency'])} mismatched")
    print()
    if not a.quiet:
        for d in res.discrepancies:
            print(f"  {SEV_ICON[d.severity]} {d.kind:<24} {d.summary}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump(res.to_dict(), fh, indent=2, default=str)
        print(f"\nFull result written to {a.json}")
    store.close()
    return 0


def cmd_list(a) -> int:
    store = Store(a.db)
    rows = store.exceptions(a.status)
    for r in rows:
        print(f"{SEV_ICON[r['severity']]} [{r['status']:<13}] {r['fingerprint']:<44} {r['summary']}")
    print(f"\n{len(rows)} exception(s)")
    return 0


def cmd_show(a) -> int:
    ex = Store(a.db).exception(a.fingerprint)
    if not ex:
        print("not found", file=sys.stderr)
        return 1
    print(f"{ex['kind']}  ({ex['severity']}, {ex['status']})\n{ex['summary']}\n")
    print(ex["explanation"], "\n")
    print("Suggested:", ex["suggested_action"])
    print("Records:  ", json.dumps(ex["refs"]))
    print("\nHistory:")
    for e in ex["events"]:
        print(f"  {e['ts']}  {e['actor']:<10} {e['action']:<12} {e['from_status'] or '':>12} → "
              f"{e['to_status']:<13} {e['note']}")
    return 0


def cmd_act(a) -> int:
    try:
        ex = Store(a.db).act(a.fingerprint, a.action, a.actor, a.note or "", a.candidate or "")
    except ActionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{ex['fingerprint']} is now {ex['status']}")
    return 0


def cmd_verify(a) -> int:
    ok, n, msg = Store(a.db).verify()
    print(f"{'✓' if ok else '✗'} audit chain {msg} ({n} events verified)")
    return 0 if ok else 2


def cmd_demo(a) -> int:
    paths = samples.generate(a.out)
    print("Sample data written to", a.out)
    for k, v in samples.SCENARIOS.items():
        print(f"   {k:<10} {v}")
    print()
    if os.path.exists(a.db) and a.fresh:
        os.remove(a.db)
    for i, day in enumerate(samples.SNAPSHOTS):
        last = i == len(samples.SNAPSHOTS) - 1
        ns = argparse.Namespace(ledger=paths["ledger"], gateway=paths["gateway"], bank=paths["bank"],
                                bank_api=None, as_of=day.isoformat(), db=a.db, json=None,
                                snapshot=True, quiet=not last, label=f"Daily close {day:%d %b}",
                                settlement_days=2, grace_days=1, fee_bp=290, fee_fixed=30)
        cmd_run(ns)
    return 0


def cmd_up(a) -> int:
    """One command for judges: build demo data if needed, then open the dashboard."""
    if not os.path.exists(a.db):
        cmd_demo(argparse.Namespace(out=a.data, db=a.db, fresh=True))
    from .server import serve
    import threading
    import webbrowser
    if not a.no_browser:
        threading.Timer(1.0, webbrowser.open, [f"http://{a.host}:{a.port}"]).start()
    serve(a.db, a.data, a.host, a.port)
    return 0


def cmd_serve(a) -> int:
    from .server import serve
    serve(a.db, a.data, a.host, a.port)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ledgerlens", description="Intelligent payment reconciliation")
    p.add_argument("--db", default="ledgerlens.db", help="SQLite file (default: ledgerlens.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="reconcile three source files")
    r.add_argument("--ledger", required=True)
    r.add_argument("--gateway", required=True)
    r.add_argument("--bank", help="bank settlement CSV")
    r.add_argument("--bank-api", help="bank transactions API base URL, e.g. "
                                      "http://127.0.0.1:8765/sim/bank/v1")
    r.add_argument("--snapshot", action="store_true", help="only use records dated before --as-of")
    r.add_argument("--label", default="")
    r.add_argument("--quiet", action="store_true")
    r.add_argument("--as-of", help="evaluation date YYYY-MM-DD (default today)")
    r.add_argument("--settlement-days", type=int, default=2)
    r.add_argument("--grace-days", type=int, default=1)
    r.add_argument("--fee-bp", type=int, default=290, help="contracted fee in basis points")
    r.add_argument("--fee-fixed", type=int, default=30, help="contracted fixed fee in cents")
    r.add_argument("--json", help="also write the full result to this file")
    r.set_defaults(fn=cmd_run)

    d = sub.add_parser("demo", help="generate sample data and reconcile it")
    d.add_argument("--out", default="samples")
    d.add_argument("--fresh", action="store_true", help="delete the database first")
    d.set_defaults(fn=cmd_demo)

    l = sub.add_parser("list", help="list exceptions")
    l.add_argument("--status", help="open|investigating|resolved|accepted|auto_cleared|active")
    l.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="explain one exception and its history")
    s.add_argument("fingerprint")
    s.set_defaults(fn=cmd_show)

    x = sub.add_parser("act", help="record an action on an exception")
    x.add_argument("fingerprint")
    x.add_argument("action", choices=["comment", "assign", "investigate", "link", "resolve", "accept",
                                      "reopen"])
    x.add_argument("--candidate", help="for 'link': the suggested record id to confirm")
    x.add_argument("--actor", required=True)
    x.add_argument("--note")
    x.set_defaults(fn=cmd_act)

    v = sub.add_parser("verify", help="verify the audit hash chain")
    v.set_defaults(fn=cmd_verify)

    sv = sub.add_parser("serve", help="open the web dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--data", default="samples", help="folder with ledger.csv/gateway.csv/bank.csv")
    sv.set_defaults(fn=cmd_serve)

    u = sub.add_parser("up", help="demo data (first time) + dashboard, in one command")
    u.add_argument("--host", default="127.0.0.1")
    u.add_argument("--port", type=int, default=8765)
    u.add_argument("--data", default="samples")
    u.add_argument("--no-browser", action="store_true")
    u.set_defaults(fn=cmd_up)

    a = p.parse_args(argv)
    return a.fn(a)
