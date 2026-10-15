import csv
import datetime as dt
import os
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from ledgerlens import ingest, samples
from ledgerlens.bank_api import load_bank_api
from ledgerlens.engine import norm_ref, reconcile
from ledgerlens.server import make_handler
from ledgerlens.service import run_reconciliation
from ledgerlens.store import ActionError, Store


def load(paths):
    return (ingest.load_ledger(paths["ledger"]), ingest.load_many(ingest.load_gateway, paths["gateway"]),
            ingest.load_many(ingest.load_bank, paths["bank"]))


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.paths = samples.generate(cls.tmp)
        cls.res = reconcile(*load(cls.paths), samples.AS_OF)
        cls.fps = {d.fingerprint for d in cls.res.discrepancies}

    def test_normalises_reference_formats(self):
        self.assertEqual({norm_ref(r) for r in ["ORD-01001", "ord_1001", "order #1001", "1001"]}, {"1001"})

    def test_detects_every_planted_scenario(self):
        expected = {
            "DUPLICATE_WEBHOOK:ch_1003A", "DUPLICATE_CHARGE:ORD-1005:ch_1005B",
            "AMOUNT_MISMATCH:JE-5008:ch_1008A", "MISSING_IN_GATEWAY:JE-5011",
            "REFERENCE_MISMATCH:JE-5014:ch_1014A", "FEE_VARIANCE:ch_1017A",
            "UNRECORDED_REFUND:re_1020", "DUPLICATE_LEDGER_ENTRY:ORD-1023:charge",
            "MISSING_IN_LEDGER:ch_2001A", "DELAYED_SETTLEMENT:po_7Hq2C",
            "PAYOUT_AMOUNT_MISMATCH:po_7Hq2D", "MISSING_SETTLEMENT:po_7Hq2E",
            "UNIDENTIFIED_DEPOSIT:BNK-90417",
            # probabilistic candidate below auto-match threshold → both sides queued for review
            "MISSING_IN_GATEWAY:JE-5033", "MISSING_IN_LEDGER:ch_1032A",
            # second gateway / bank: chargebacks, over-refund, settlement timing
            "OVER_REFUND:3007", "UNRECORDED_CHARGEBACK:ps_cb_3007", "CHARGEBACK_OPEN:ps_cb_3007",
            "CHARGEBACK_OPEN:ps_cb_3009", "DELAYED_SETTLEMENT:PS-STL-02", "PARTIAL_SETTLEMENT:PS-STL-04",
        }
        self.assertEqual(self.fps, expected)

    def test_clean_cases_produce_no_noise(self):
        joined = " ".join(self.fps)
        self.assertNotIn("1002", joined)          # refund recorded both sides
        self.assertNotIn("po_7Hq2F", joined)      # in transit, not overdue
        self.assertNotIn("BNK-90418", joined)     # bank fee debit
        statuses = {p.id: p.status for p in self.res.payouts}
        self.assertEqual(statuses["po_7Hq2F"], "in_transit")
        self.assertEqual(statuses["po_7Hq2B"], "settled")   # fuzzy-matched without payout id
        for clean in ("3001", "3002", "3005", "3006", "3008"):  # split, consolidated, composite, partial refund, won
            self.assertNotIn(clean, joined)

    def test_advanced_matching_strategies(self):
        by = {m.method: m for m in self.res.matches}
        self.assertEqual(by["exact · many-to-one"].right, ["ps_txn_3001a", "ps_txn_3001b"])
        self.assertEqual(len(by["aggregate · one-to-many"].left), 3)
        self.assertEqual(by["composite"].right, ["ps_txn_3005"])
        self.assertEqual(sorted(by["payout ids · one-to-many"].left), ["PS-STL-02", "PS-STL-03"])
        self.assertEqual(by["payout id · many-to-one"].right, ["HB-1101", "HB-1102"])
        for m in self.res.matches:   # explainable: factor contributions add up to the score
            if m.method != "probabilistic":
                self.assertAlmostEqual(sum(f["contribution"] for f in m.factors), m.confidence, delta=0.011)
        po_b = next(m for m in self.res.matches if m.left == ["po_7Hq2B"])
        self.assertLess(po_b.confidence, 1.0)

    def test_disputes_and_settlement_analytics(self):
        d = {x["id"]: x["status"] for x in self.res.disputes}
        self.assertEqual(d, {"ps_cb_3007": "open", "ps_cb_3008": "won", "ps_cb_3009": "open"})
        gws = {g["gateway"]: g for g in self.res.stats["settlement"]["by_gateway"]}
        self.assertEqual(set(gws), {"NovaPay", "Paystream"})
        self.assertEqual(gws["Paystream"]["partial"], 1)
        self.assertEqual(len(self.res.stats["settlement"]["by_account"]), 2)

    def test_payout_mismatch_explains_gap(self):
        d = next(d for d in self.res.discrepancies if d.kind == "PAYOUT_AMOUNT_MISMATCH")
        self.assertIn("ch_1027A", d.explanation)

    def test_fuzzy_match_has_lower_confidence(self):
        m = next(m for m in self.res.matches if m.right == ["ch_1014A"])
        self.assertEqual(m.method, "probabilistic")
        self.assertGreaterEqual(m.confidence, 0.8)
        self.assertLess(m.confidence, 1.0)

    def test_weak_candidate_is_suggested_not_matched(self):
        d = next(d for d in self.res.discrepancies if d.fingerprint == "MISSING_IN_GATEWAY:JE-5033")
        self.assertEqual([c["id"] for c in d.candidates], ["ch_1032A"])
        self.assertTrue(0.45 <= d.candidates[0]["score"] < 0.8)

    def test_reports_per_source_pair(self):
        reps = {r["pair"]: r for r in self.res.reports}
        self.assertEqual(set(reps), {"ledger_gateway", "gateway_bank"})
        for r in reps.values():
            self.assertLessEqual(r["clean"], r["records"])
            self.assertGreater(r["mismatched_amount"], 0)
        self.assertEqual(self.res.pipeline["probabilistic"]["suggested"], 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = samples.generate(self.tmp)
        self.store = Store(os.path.join(self.tmp, "t.db"))
        self.store.record_run(reconcile(*load(self.paths), samples.AS_OF), {})

    def tearDown(self):
        self.store.close()

    def test_workflow_rules(self):
        fp = "FEE_VARIANCE:ch_1017A"
        with self.assertRaises(ActionError):
            self.store.act(fp, "resolve", "ana")                 # note required
        with self.assertRaises(ActionError):
            self.store.act(fp, "reopen", "ana", "x")             # not closed yet
        self.store.act(fp, "investigate", "ana")
        ex = self.store.act(fp, "accept", "ana", "Amex surcharge, per contract addendum")
        self.assertEqual(ex["status"], "accepted")
        self.assertEqual([e["action"] for e in ex["events"]], ["detected", "investigate", "accept"])

    def test_rerun_auto_clears_and_reopens(self):
        # fix the ledger: post the missing refund for 1020
        with open(self.paths["ledger"]) as fh:
            rows = list(csv.DictReader(fh))
        amt = next(r["amount"] for r in rows if r["order_ref"] == "ORD-1020")
        rows.append({"entry_id": "JE-9999", "order_ref": "ORD-1020", "posted_at": "10/10/2026 15:12",
                     "type": "refund", "amount": amt, "currency": "USD", "refund_of": "ORD-1020"})
        with open(self.paths["ledger"], "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        fp = "UNRECORDED_REFUND:re_1020"
        self.store.record_run(reconcile(*load(self.paths), samples.AS_OF), {})
        self.assertEqual(self.store.exception(fp)["status"], "auto_cleared")

        # resolved-but-still-broken items reopen on the next run
        fp2 = "MISSING_IN_LEDGER:ch_2001A"
        self.store.act(fp2, "resolve", "ana", "posted it")
        self.store.record_run(reconcile(*load(self.paths), samples.AS_OF), {})
        ex = self.store.exception(fp2)
        self.assertEqual(ex["status"], "open")
        self.assertEqual(ex["events"][-1]["action"], "reopened")

    def test_reviewer_link_feeds_next_run(self):
        self.store.act("MISSING_IN_GATEWAY:JE-5033", "link", "ana", "promo applied", "ch_1032A")
        self.assertEqual(self.store.exception("MISSING_IN_LEDGER:ch_1032A")["status"], "resolved")
        with self.assertRaises(ActionError):
            self.store.act("MISSING_IN_GATEWAY:JE-5011", "link", "ana", "", "ch_2001A")  # not a candidate
        res = reconcile(*load(self.paths), samples.AS_OF, manual_links=self.store.manual_links())
        self.store.record_run(res, {})
        m = next(m for m in res.matches if m.right == ["ch_1032A"])
        self.assertEqual(m.method, "manual")
        self.assertEqual(self.store.exception("AMOUNT_MISMATCH:JE-5033:ch_1032A")["status"], "open")

    def test_history_is_append_only_and_tamper_evident(self):
        self.store.act("FEE_VARIANCE:ch_1017A", "comment", "ana", "looking")
        self.assertTrue(self.store.verify()[0])
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.db.execute("UPDATE events SET note='edited' WHERE id=1")
        # bypass the trigger as a determined attacker would; the chain still catches it
        self.store.db.execute("DROP TRIGGER events_no_update")
        self.store.db.execute("UPDATE events SET note='edited' WHERE id=1")
        ok, _, msg = self.store.verify()
        self.assertFalse(ok)
        self.assertIn("#1", msg)


class SnapshotAndApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = samples.generate(self.tmp)
        self.db = os.path.join(self.tmp, "t.db")

    def test_daily_snapshots_escalate_then_clear(self):
        store = Store(self.db)
        for day in samples.SNAPSHOTS:
            run_reconciliation(store, self.paths["ledger"], self.paths["gateway"],
                               bank_path=self.paths["bank"], as_of=day, snapshot=True)
        ex = store.exception("MISSING_SETTLEMENT:po_7Hq2C")   # overdue on the 12th, arrived on the 14th
        self.assertEqual([e["action"] for e in ex["events"]], ["detected", "auto_cleared"])
        self.assertEqual(store.exception("DELAYED_SETTLEMENT:po_7Hq2C")["status"], "open")
        self.assertEqual(len(store.reports()), 6)
        self.assertTrue(store.verify()[0])
        store.close()

    def test_bank_api_simulator_round_trip(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, self.tmp, 0))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_port}/sim/bank/v1"
            via_api = load_bank_api(url)
            via_csv = ingest.load_many(ingest.load_bank, self.paths["bank"])
            key = lambda b: (b.id, b.value_date, b.amount, b.description, b.account)
            self.assertEqual(sorted(map(key, via_api)), sorted(map(key, via_csv)))
            self.assertEqual(len(load_bank_api(url, dt.date(2026, 10, 9))), 4)
        finally:
            httpd.shutdown()
            httpd.server_close()


class DashboardApiTest(unittest.TestCase):
    """Upload, bulk triage and export endpoints, end to end over HTTP."""
    def setUp(self):
        import json, urllib.request
        self.json, self.urlreq = json, urllib.request
        self.tmp = tempfile.mkdtemp()
        self.paths = samples.generate(self.tmp)
        self.db = os.path.join(self.tmp, "t.db")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, self.tmp, 0))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def post(self, path, body):
        req = self.urlreq.Request(self.base + path, self.json.dumps(body).encode(),
                                  {"Content-Type": "application/json"})
        try:
            with self.urlreq.urlopen(req) as r:
                return r.status, self.json.load(r)
        except self.urlreq.HTTPError as e:
            return e.code, self.json.load(e)

    def get(self, path):
        with self.urlreq.urlopen(self.base + path) as r:
            return r.read().decode()

    def test_upload_bulk_and_export(self):
        from pathlib import Path
        files = {"ledger": Path(self.paths["ledger"]).read_text(),
                 **{k: [{"name": os.path.basename(p), "text": Path(p).read_text()} for p in self.paths[k]]
                    for k in ("gateway", "bank")}}
        code, body = self.post("/api/upload", {"files": files, "as_of": "2026-10-15"})
        self.assertEqual(code, 200)
        self.assertEqual(body["stats"]["discrepancies"], 21)
        code, body = self.post("/api/upload", {"files": {**files, "bank": "bank_ref,value_date,amount\nX,nope,1"}})
        self.assertEqual(code, 400)
        self.assertIn("bad date", body["error"])

        fps = ["FEE_VARIANCE:ch_1017A", "MISSING_SETTLEMENT:po_7Hq2E"]
        code, body = self.post("/api/exceptions/bulk", {"fingerprints": fps, "action": "accept", "actor": "j"})
        self.assertEqual(body["done"], [])                       # note required
        code, body = self.post("/api/exceptions/bulk",
                               {"fingerprints": fps, "action": "accept", "actor": "j", "note": "ok"})
        self.assertEqual(sorted(body["done"]), sorted(fps))
        # high severity needs a second reviewer (maker-checker)
        code, body = self.post("/api/exceptions/MISSING_SETTLEMENT:po_7Hq2E/actions",
                               {"action": "approve", "actor": "j"})
        self.assertEqual(code, 400)
        self.assertIn("different reviewer", body["error"])
        code, body = self.post("/api/exceptions/MISSING_SETTLEMENT:po_7Hq2E/actions",
                               {"action": "approve", "actor": "k"})
        self.assertEqual(body["status"], "accepted")

        csv_text = self.get("/api/export/exceptions.csv?status=accepted")
        self.assertEqual(len(csv_text.strip().splitlines()), 3)
        self.assertIn("Matched %", self.get("/api/export/report/1/gateway_bank.csv"))


if __name__ == "__main__":
    unittest.main()
