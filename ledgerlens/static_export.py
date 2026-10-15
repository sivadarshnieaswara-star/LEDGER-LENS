"""Pre-render LedgerLens into a static site for hosts that can't run Python
(Netlify, GitHub Pages, S3). The real server runs in-process during the
build, every GET endpoint the UI uses is fetched and saved under data/, and
index.html is told to read from there. The result is a read-only demo:
browsing works, while writes (reviews, uploads, runs) show a notice."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from . import samples
from .server import STATIC, make_handler
from .service import run_reconciliation
from .store import Store


def static_name(path: str) -> str:
    """Must match staticPath() in index.html."""
    p = urllib.parse.unquote(path.split("?")[0]).lstrip("/")
    return "data/" + re.sub(r"[^A-Za-z0-9._-]", "_", p) + ("" if p.endswith(".csv") else ".json")


def build_demo(workdir: str) -> tuple[str, str]:
    data, db = "samples", "ledgerlens.db"
    paths = samples.generate(os.path.join(workdir, data))
    store = Store(os.path.join(workdir, db))
    for day in samples.SNAPSHOTS:
        run_reconciliation(store, paths["ledger"], paths["gateway"], bank_path=paths["bank"],
                           as_of=day, snapshot=True, label=f"Daily close {day:%d %b}")
    # a little reviewer activity so the audit trail and approvals have something to show
    for fp, action, actor, note in [
        ("DUPLICATE_CHARGE:ORD-1005:ch_1005B", "investigate", "Priya", ""),
        ("DUPLICATE_CHARGE:ORD-1005:ch_1005B", "comment", "Priya", "Customer emailed support, ticket #4471"),
        ("FEE_VARIANCE:ch_1017A", "accept", "Marco", "Amex surcharge per contract addendum B"),
        ("MISSING_SETTLEMENT:po_7Hq2E", "resolve", "Priya", "NovaPay confirmed a re-send on 16 Oct"),
    ]:
        store.act(fp, action, actor, note)
    store.close()
    return os.path.join(workdir, db), os.path.join(workdir, data)


def export(out: str) -> int:
    work = tempfile.mkdtemp(prefix="ledgerlens-static-")
    db, data = build_demo(work)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, data, 0))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def get(path: str) -> bytes:
        with urllib.request.urlopen(base + path) as r:
            return r.read()

    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "data"))
    written = 0

    def save(path: str, body: bytes) -> None:
        nonlocal written
        with open(os.path.join(out, static_name(path)), "wb") as fh:
            fh.write(body)
        written += 1

    try:
        summary = json.loads(get("/api/summary"))
        # don't leak build-machine paths into the published demo
        summary["sources"] = {"ledger": "samples/ledger.csv",
                              "gateway": ["samples/gateway_novapay.csv", "samples/gateway_paystream.csv"],
                              "bank": ["samples/bank_harbor.csv", "samples/bank_northbank.csv"],
                              "bank_api": "/sim/bank/v1 (full app only)"}
        for r in summary.get("runs", []):
            r["sources"] = {k: re.sub(r"(?:/[^ ,]*)?/(samples/[^ ,]+)", r"\1", v) for k, v in r["sources"].items()}
        save("/api/summary", json.dumps(summary).encode())
        for path in ("/api/exceptions", "/api/reports", "/api/traces", "/api/audit?limit=300",
                     "/api/export/exceptions.csv", "/sim/bank/v1/transactions?limit=200",
                     "/templates/ledger.csv", "/templates/gateway.csv", "/templates/bank.csv"):
            save(path, get(path))
        for rep in json.loads(get("/api/reports")):
            for path in (f"/api/reports/{rep['run_id']}/{rep['pair']}",
                         f"/api/export/report/{rep['run_id']}/{rep['pair']}.csv"):
                save(path, get(path))
        for ex in json.loads(get("/api/exceptions")):
            path = "/api/exceptions/" + urllib.parse.quote(ex["fingerprint"], safe="")
            save(path, get(path))
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(work, ignore_errors=True)

    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("<script>\n", "<script>window.LEDGERLENS_STATIC = true;</script>\n<script>\n", 1)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Static demo written to {out}/ ({written} data files)")
    return 0
