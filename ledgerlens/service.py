"""One reconciliation run end to end: ingest → normalise → match → classify →
exception queue. Shared by the CLI and the web server."""
from __future__ import annotations

import datetime as dt

from . import ingest
from .bank_api import load_bank_api
from .engine import Config, reconcile
from .store import Store


def run_reconciliation(store: Store, ledger_path: str, gateway_path: str, *,
                       bank_path: str | None = None, bank_api: str | None = None,
                       as_of: dt.date, config: Config | None = None,
                       snapshot: bool = False, label: str = "") -> tuple[int, object]:
    """With snapshot=True only records dated before `as_of` are used, i.e. the
    view a finance team would have had at that day's close."""
    ledger = ingest.load_ledger(ledger_path)
    gateway = ingest.load_gateway(gateway_path)
    if bank_api:
        bank = load_bank_api(bank_api, as_of if snapshot else None)
        bank_src = f"Bank API · {bank_api}"
    else:
        bank = ingest.load_bank(bank_path)
        bank_src = f"Bank CSV · {bank_path}"
    if snapshot:
        ledger = [r for r in ledger if r.ts.date() < as_of]
        gateway = [r for r in gateway if r.ts.date() < as_of]
        bank = [r for r in bank if r.value_date < as_of]
    res = reconcile(ledger, gateway, bank, as_of, config, store.manual_links())
    run_id = store.record_run(res, {"ledger": f"Ledger CSV · {ledger_path}",
                                    "gateway": f"Gateway CSV · {gateway_path}",
                                    "bank": bank_src}, label)
    return run_id, res
