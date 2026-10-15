"""Tiny stdlib HTTP server: JSON API, the bank API simulator, and the dashboard."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import bank_api, ingest
from .export import exceptions_csv, report_csv
from .service import run_reconciliation
from .store import ActionError, Store

STATIC = os.path.join(os.path.dirname(__file__), "static")
RUN_LOCK = threading.Lock()
MAX_UPLOAD = 5 * 1024 * 1024   # per file


def make_handler(db_path: str, data_dir: str, port: int):
    paths = {k: os.path.join(data_dir, f"{k}.csv") for k in ("ledger", "gateway", "bank")}
    sim_url = f"http://127.0.0.1:{port}/sim/bank/v1"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter console
            pass

        def _send(self, code: int, body, ctype="application/json", headers=None):
            if isinstance(body, str):
                body = body.encode()
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            return json.loads(raw or b"{}")

        def do_GET(self):
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            if not parts:
                with open(os.path.join(STATIC, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if len(parts) == 2 and parts[0] == "templates" and parts[1] in ("ledger.csv", "gateway.csv", "bank.csv"):
                p = paths[parts[1][:-4]]
                if not os.path.exists(p):
                    return self._send(404, {"error": "run the demo first"})
                with open(p, "rb") as fh:
                    return self._send(200, fh.read(), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{parts[1]}"'})
            if parts == ["sim", "bank", "v1", "transactions"]:
                try:
                    return self._send(200, bank_api.transactions(
                        paths["bank"], qs.get("from"), qs.get("to"),
                        int(qs.get("cursor", 0)), min(200, int(qs.get("limit", 50)))))
                except (ValueError, OSError) as exc:
                    return self._send(400, {"error": str(exc)})
            store = Store(db_path)
            try:
                if parts == ["api", "summary"]:
                    run = store.latest_run()
                    ok, n, msg = store.verify()
                    return self._send(200, {
                        "run": {k: v for k, v in run.items() if k != "traces"} if run else None,
                        "runs": store.runs(), "review": store.review_stats(),
                        "audit": {"ok": ok, "events": n, "message": msg},
                        "sources": {**paths, "bank_api": sim_url},
                        "data_ready": all(os.path.exists(p) for p in paths.values())})
                if parts == ["api", "reports"]:
                    return self._send(200, store.reports())
                if len(parts) == 4 and parts[:2] == ["api", "reports"]:
                    rep = store.report(int(parts[2]), parts[3])
                    return self._send(200 if rep else 404, rep or {"error": "not found"})
                if parts == ["api", "exceptions"]:
                    return self._send(200, store.exceptions(qs.get("status")))
                if parts == ["api", "export", "exceptions.csv"]:
                    return self._send(200, exceptions_csv(store.exceptions(qs.get("status"))),
                                      "text/csv; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="ledgerlens-exceptions.csv"'})
                if len(parts) == 5 and parts[:2] == ["api", "export"] and parts[2] == "report":
                    rep = store.report(int(parts[3]), parts[4].removesuffix(".csv"))
                    if not rep:
                        return self._send(404, {"error": "not found"})
                    name = f"ledgerlens-run{parts[3]}-{parts[4]}"
                    return self._send(200, report_csv(rep), "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
                if len(parts) == 3 and parts[:2] == ["api", "exceptions"]:
                    ex = store.exception(parts[2])
                    return self._send(200 if ex else 404, ex or {"error": "not found"})
                if parts == ["api", "traces"]:
                    run = store.latest_run()
                    return self._send(200, run["traces"] if run else [])
                if parts == ["api", "audit"]:
                    ok, n, msg = store.verify()
                    return self._send(200, {"ok": ok, "events": n, "message": msg,
                                            "log": store.events(int(qs.get("limit", 300)))})
                return self._send(404, {"error": "not found"})
            finally:
                store.close()

        def do_POST(self):
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid JSON"})
            if parts == ["api", "runs"]:
                return self._run(body)
            if parts == ["api", "upload"]:
                return self._upload(body)
            if parts == ["api", "exceptions", "bulk"]:
                store = Store(db_path)
                try:
                    fps = [str(f) for f in body.get("fingerprints", [])][:500]
                    return self._send(200, store.bulk(fps, str(body.get("action", "")),
                                                      str(body.get("actor", "")), str(body.get("note", ""))))
                finally:
                    store.close()
            if len(parts) != 4 or parts[:2] != ["api", "exceptions"] or parts[3] != "actions":
                return self._send(404, {"error": "not found"})
            store = Store(db_path)
            try:
                ex = store.act(parts[2], str(body.get("action", "")), str(body.get("actor", "")),
                               str(body.get("note", "")), str(body.get("candidate", "")))
                return self._send(200, ex)
            except ActionError as exc:
                return self._send(400, {"error": str(exc)})
            finally:
                store.close()

        def _run(self, body: dict):
            try:
                as_of = dt.date.fromisoformat(body.get("as_of") or dt.date.today().isoformat())
            except ValueError:
                return self._send(400, {"error": "as_of must be YYYY-MM-DD"})
            use_api = body.get("bank_source") == "api"
            if not RUN_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "a run is already in progress"})
            store = Store(db_path)
            try:
                run_id, res = run_reconciliation(
                    store, paths["ledger"], paths["gateway"],
                    bank_path=None if use_api else paths["bank"], bank_api=sim_url if use_api else None,
                    as_of=as_of, snapshot=bool(body.get("snapshot", True)),
                    label=str(body.get("label") or f"Manual run · {as_of:%d %b}"))
                return self._send(200, {"run_id": run_id, "stats": res.stats})
            except (OSError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})
            finally:
                store.close()
                RUN_LOCK.release()

        def _upload(self, body: dict):
            """Reconcile three user-supplied CSVs (sent as text)."""
            files = body.get("files") or {}
            if set(files) != {"ledger", "gateway", "bank"}:
                return self._send(400, {"error": "upload all three files: ledger, gateway and bank"})
            if any(len(str(v).encode()) > MAX_UPLOAD for v in files.values()):
                return self._send(400, {"error": "each file must be under 5 MB"})
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            folder = os.path.join(data_dir, "uploads", stamp)
            os.makedirs(folder, exist_ok=True)
            up = {}
            for k, text in files.items():
                up[k] = os.path.join(folder, f"{k}.csv")
                with open(up[k], "w", encoding="utf-8") as fh:
                    fh.write(str(text))
            try:   # fail fast with a readable message before touching the store
                for k, fn in (("ledger", ingest.load_ledger), ("gateway", ingest.load_gateway),
                              ("bank", ingest.load_bank)):
                    if not fn(up[k]):
                        raise ingest.IngestError(f"{k} file has no rows")
            except (ingest.IngestError, ValueError, KeyError) as exc:
                return self._send(400, {"error": f"could not read upload: {exc}"})
            try:
                as_of = dt.date.fromisoformat(body.get("as_of") or dt.date.today().isoformat())
            except ValueError:
                return self._send(400, {"error": "as_of must be YYYY-MM-DD"})
            if not RUN_LOCK.acquire(blocking=False):
                return self._send(409, {"error": "a run is already in progress"})
            store = Store(db_path)
            try:
                run_id, res = run_reconciliation(store, up["ledger"], up["gateway"], bank_path=up["bank"],
                                                 as_of=as_of, label=f"Upload · {stamp}")
                return self._send(200, {"run_id": run_id, "stats": res.stats})
            except (OSError, ValueError) as exc:
                return self._send(400, {"error": str(exc)})
            finally:
                store.close()
                RUN_LOCK.release()

    return Handler


def serve(db_path: str, data_dir: str = "samples", host: str = "127.0.0.1", port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), make_handler(db_path, data_dir, port))
    print(f"LedgerLens dashboard → http://{host}:{port}")
    print(f"Bank API simulator  → http://{host}:{port}/sim/bank/v1/transactions  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
