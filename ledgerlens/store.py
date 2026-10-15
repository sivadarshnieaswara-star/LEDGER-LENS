"""SQLite persistence: runs and their reports, matches, the exception queue,
reviewer-confirmed links, and an append-only, hash-chained event log that is
the audit trail.

Exceptions are keyed by the engine's fingerprint, so re-running against newer
source files updates the same exception: still present → stays open; gone →
auto-cleared; back after being resolved → reopened. Every transition is an
event, and each event's hash covers the previous one, so any edit to history
is detectable with `verify()`.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from dataclasses import asdict

from .engine import Result

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY, created_at TEXT, as_of TEXT, sources TEXT, stats TEXT,
  traces TEXT, payouts TEXT, reports TEXT DEFAULT '[]', pipeline TEXT DEFAULT '{}',
  label TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS matches(
  run_id INTEGER, layer TEXT, left_ids TEXT, right_ids TEXT, method TEXT,
  confidence REAL, explanation TEXT);
CREATE TABLE IF NOT EXISTS exceptions(
  fingerprint TEXT PRIMARY KEY, kind TEXT, severity TEXT, status TEXT, summary TEXT,
  explanation TEXT, suggested_action TEXT, refs TEXT, amount_delta INTEGER, currency TEXT,
  assignee TEXT DEFAULT '', first_seen_run INTEGER, last_seen_run INTEGER, updated_at TEXT,
  pair TEXT DEFAULT '', candidates TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS run_discrepancies(
  run_id INTEGER, fingerprint TEXT, kind TEXT, severity TEXT, pair TEXT, amount_delta INTEGER,
  summary TEXT);
CREATE TABLE IF NOT EXISTS manual_links(
  ledger_id TEXT, gateway_id TEXT, actor TEXT, note TEXT, ts TEXT,
  PRIMARY KEY(ledger_id, gateway_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT, run_id INTEGER, ts TEXT,
  actor TEXT, action TEXT, from_status TEXT, to_status TEXT, note TEXT,
  prev_hash TEXT, hash TEXT);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
  BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""
# columns added after the first release; ALTERed in if an older database is opened
MIGRATIONS = {
    "runs": {"reports": "TEXT DEFAULT '[]'", "pipeline": "TEXT DEFAULT '{}'", "label": "TEXT DEFAULT ''",
             "disputes": "TEXT DEFAULT '[]'"},
    "exceptions": {"pair": "TEXT DEFAULT ''", "candidates": "TEXT DEFAULT '[]'",
                   "pending_action": "TEXT DEFAULT ''", "requested_by": "TEXT DEFAULT ''"},
    "matches": {"factors": "TEXT DEFAULT '[]'"},
}

GENESIS = "0" * 64
OPEN_STATES = {"open", "investigating", "pending_approval"}
# Maker-checker: closing a high-severity exception, or one above this amount, needs a second person.
APPROVAL_THRESHOLD = 10_000   # cents
LINKABLE = {"MISSING_IN_GATEWAY", "MISSING_IN_LEDGER", "UNRECORDED_REFUND"}

# action → (allowed from-states, to-state, note required)
ACTIONS = {
    "comment": (None, None, True),
    "assign": (None, None, True),
    "investigate": ({"open"}, "investigating", False),
    "link": ({"open", "investigating"}, "resolved", False),
    "resolve": ({"open", "investigating"}, "resolved", True),
    "accept": ({"open", "investigating"}, "accepted", True),
    "approve": ({"pending_approval"}, None, False),
    "reject": ({"pending_approval"}, "investigating", True),
    "reopen": ({"resolved", "accepted", "auto_cleared"}, "open", True),
}


def needs_approval(row) -> bool:
    return row["severity"] == "high" or abs(row["amount_delta"] or 0) >= APPROVAL_THRESHOLD


class ActionError(ValueError):
    pass


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _hash(prev: str, fields: dict) -> str:
    body = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ events
    def _event(self, fp: str, run_id: int | None, actor: str, action: str,
               frm: str | None, to: str | None, note: str) -> None:
        row = self.db.execute("SELECT hash FROM events ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["hash"] if row else GENESIS
        fields = {"fingerprint": fp, "run_id": run_id, "ts": now(), "actor": actor,
                  "action": action, "from_status": frm, "to_status": to, "note": note}
        self.db.execute(
            "INSERT INTO events(fingerprint,run_id,ts,actor,action,from_status,to_status,note,prev_hash,hash)"
            " VALUES(:fingerprint,:run_id,:ts,:actor,:action,:from_status,:to_status,:note,:prev,:hash)",
            {**fields, "prev": prev, "hash": _hash(prev, fields)})

    def verify(self) -> tuple[bool, int, str]:
        prev = GENESIS
        n = 0
        for r in self.db.execute("SELECT * FROM events ORDER BY id"):
            fields = {k: r[k] for k in ("fingerprint", "run_id", "ts", "actor", "action",
                                        "from_status", "to_status", "note")}
            if r["prev_hash"] != prev or _hash(prev, fields) != r["hash"]:
                return False, n, f"chain broken at event #{r['id']}"
            prev = r["hash"]
            n += 1
        return True, n, "intact"

    # -------------------------------------------------------------- runs
    def record_run(self, res: Result, sources: dict, label: str = "") -> int:
        d = res.to_dict()
        queue = {"new": 0, "still_open": 0, "reopened": 0, "auto_cleared": 0, "accepted_kept": 0}
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs(created_at,as_of,sources,stats,traces,payouts,reports,pipeline,label)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (now(), d["as_of"], json.dumps(sources), json.dumps(d["stats"]), json.dumps(d["traces"]),
                 json.dumps(d["payouts"]), json.dumps(d["reports"]), "{}", label))
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT INTO matches(run_id,layer,left_ids,right_ids,method,confidence,explanation,factors)"
                " VALUES(?,?,?,?,?,?,?,?)",
                [(run_id, m.layer, json.dumps(m.left), json.dumps(m.right), m.method,
                  m.confidence, m.explanation, json.dumps(m.factors)) for m in res.matches])
            self.db.execute("UPDATE runs SET disputes=? WHERE id=?", (json.dumps(d["disputes"]), run_id))
            self.db.executemany(
                "INSERT INTO run_discrepancies VALUES(?,?,?,?,?,?,?)",
                [(run_id, x.fingerprint, x.kind, x.severity, x.pair, x.amount_delta, x.summary)
                 for x in res.discrepancies])
            seen = set()
            for disc in res.discrepancies:
                seen.add(disc.fingerprint)
                x = asdict(disc)
                existing = self.db.execute("SELECT status FROM exceptions WHERE fingerprint=?",
                                           (disc.fingerprint,)).fetchone()
                vals = (x["kind"], x["severity"], x["summary"], x["explanation"],
                        x["suggested_action"], json.dumps(x["refs"]), x["amount_delta"],
                        x["currency"], x["pair"], json.dumps(x["candidates"]), run_id, now(),
                        disc.fingerprint)
                if existing is None:
                    self.db.execute(
                        "INSERT INTO exceptions(kind,severity,summary,explanation,suggested_action,refs,"
                        "amount_delta,currency,pair,candidates,last_seen_run,updated_at,fingerprint,"
                        "status,first_seen_run) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                        (*vals, run_id))
                    self._event(disc.fingerprint, run_id, "engine", "detected", None, "open",
                                disc.summary)
                    queue["new"] += 1
                    continue
                self.db.execute(
                    "UPDATE exceptions SET kind=?,severity=?,summary=?,explanation=?,suggested_action=?,"
                    "refs=?,amount_delta=?,currency=?,pair=?,candidates=?,last_seen_run=?,updated_at=?"
                    " WHERE fingerprint=?", vals)
                if existing["status"] in ("resolved", "auto_cleared"):
                    self._set_status(disc.fingerprint, "open")
                    self._event(disc.fingerprint, run_id, "engine", "reopened", existing["status"],
                                "open", f"Detected again in run #{run_id}. The source data still "
                                        f"does not reconcile.")
                    queue["reopened"] += 1
                elif existing["status"] == "accepted":
                    queue["accepted_kept"] += 1
                else:
                    queue["still_open"] += 1
            for r in self.db.execute("SELECT fingerprint,status FROM exceptions").fetchall():
                if r["fingerprint"] not in seen and r["status"] in OPEN_STATES:
                    self._set_status(r["fingerprint"], "auto_cleared")
                    self._event(r["fingerprint"], run_id, "engine", "auto_cleared", r["status"],
                                "auto_cleared", f"Not detected in run #{run_id}. The sources "
                                                f"now reconcile.")
                    queue["auto_cleared"] += 1
            pipeline = {**d["pipeline"], "queue": queue}
            self.db.execute("UPDATE runs SET pipeline=? WHERE id=?", (json.dumps(pipeline), run_id))
        return run_id

    def _set_status(self, fp: str, status: str) -> None:
        self.db.execute("UPDATE exceptions SET status=?, updated_at=? WHERE fingerprint=?",
                        (status, now(), fp))

    # ----------------------------------------------------------- actions
    def act(self, fp: str, action: str, actor: str, note: str = "", candidate: str = "") -> dict:
        if action not in ACTIONS:
            raise ActionError(f"unknown action {action!r}")
        allowed, to, need_note = ACTIONS[action]
        if not actor.strip():
            raise ActionError("actor is required")
        if need_note and not note.strip():
            raise ActionError(f"'{action}' requires a note explaining why")
        with self.db:
            row = self.db.execute("SELECT * FROM exceptions WHERE fingerprint=?", (fp,)).fetchone()
            if row is None:
                raise ActionError(f"no exception {fp!r}")
            frm = row["status"]
            if allowed is not None and frm not in allowed:
                raise ActionError(f"cannot {action} an exception that is {frm}")
            if action == "link":
                self._link(row, candidate, actor.strip(), note.strip())
                return self.exception(fp)
            if action in ("resolve", "accept") and needs_approval(row):
                self.db.execute("UPDATE exceptions SET pending_action=?, requested_by=? WHERE fingerprint=?",
                                (action, actor.strip(), fp))
                self._set_status(fp, "pending_approval")
                self._event(fp, None, actor.strip(), f"request_{action}", frm, "pending_approval",
                            note.strip() + " · awaiting approval by a second reviewer")
                return self.exception(fp)
            if action == "approve":
                if actor.strip().lower() == (row["requested_by"] or "").lower():
                    raise ActionError("maker-checker: a different reviewer must approve this request")
                to = {"resolve": "resolved", "accept": "accepted"}[row["pending_action"]]
            if action in ("approve", "reject"):
                self.db.execute("UPDATE exceptions SET pending_action='', requested_by='' WHERE fingerprint=?",
                                (fp,))
            if to:
                self._set_status(fp, to)
            if action == "assign":
                self.db.execute("UPDATE exceptions SET assignee=? WHERE fingerprint=?", (note.strip(), fp))
            self._event(fp, None, actor.strip(), action, frm, to or frm, note.strip())
        return self.exception(fp)

    def _link(self, row: sqlite3.Row, candidate: str, actor: str, note: str) -> None:
        """Human confirms a probabilistic candidate. The pair is remembered and used
        as a match in every later run, and both one-sided exceptions close."""
        if row["kind"] not in LINKABLE:
            raise ActionError(f"{row['kind']} cannot be resolved by linking records")
        cands = {c["id"]: c for c in json.loads(row["candidates"] or "[]")}
        if candidate not in cands:
            raise ActionError(f"{candidate!r} is not a suggested candidate for this exception")
        refs = json.loads(row["refs"])
        if cands[candidate]["side"] == "gateway":
            ledger_id, gateway_id = refs["ledger"][0], candidate
        else:
            ledger_id, gateway_id = candidate, refs["gateway"][0]
        self.db.execute("INSERT OR REPLACE INTO manual_links VALUES(?,?,?,?,?)",
                        (ledger_id, gateway_id, actor, note, now()))
        why = (f"Confirmed {ledger_id} ↔ {gateway_id} as the same payment "
               f"(candidate score {cands[candidate]['score']:.0%})" + (f": {note}" if note else ""))
        affected = [row["fingerprint"]]
        for other in self.db.execute(
                "SELECT fingerprint, refs FROM exceptions WHERE status IN ('open','investigating')"
                " AND kind IN ('MISSING_IN_GATEWAY','MISSING_IN_LEDGER','UNRECORDED_REFUND')"
                " AND fingerprint != ?", (row["fingerprint"],)).fetchall():
            ids = [i for v in json.loads(other["refs"]).values() for i in v]
            if candidate in ids:
                affected.append(other["fingerprint"])
        for f in affected:
            frm = self.db.execute("SELECT status FROM exceptions WHERE fingerprint=?", (f,)).fetchone()[0]
            self._set_status(f, "resolved")
            self._event(f, None, actor, "link", frm, "resolved", why)

    def manual_links(self) -> list[tuple[str, str]]:
        return [(r["ledger_id"], r["gateway_id"]) for r in self.db.execute("SELECT * FROM manual_links")]

    # ----------------------------------------------------------- queries
    def _run(self, r: sqlite3.Row, full: bool = True) -> dict:
        out = {"id": r["id"], "created_at": r["created_at"], "as_of": r["as_of"], "label": r["label"],
               "sources": json.loads(r["sources"]), "stats": json.loads(r["stats"]),
               "reports": json.loads(r["reports"] or "[]"), "pipeline": json.loads(r["pipeline"] or "{}"),
               "disputes": json.loads(r["disputes"] or "[]")}
        if full:
            out["traces"] = json.loads(r["traces"])
            out["payouts"] = json.loads(r["payouts"])
        return out

    def latest_run(self) -> dict | None:
        r = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return self._run(r) if r else None

    def runs(self) -> list[dict]:
        return [self._run(r, full=False) for r in self.db.execute("SELECT * FROM runs ORDER BY id DESC")]

    def reports(self) -> list[dict]:
        out = []
        for run in self.runs():
            for rep in run["reports"]:
                out.append({**rep, "run_id": run["id"], "as_of": run["as_of"],
                            "created_at": run["created_at"], "label": run["label"]})
        return out

    def report(self, run_id: int, pair: str) -> dict | None:
        r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not r:
            return None
        run = self._run(r, full=False)
        rep = next((x for x in run["reports"] if x["pair"] == pair), None)
        if not rep:
            return None
        layer = {"ledger_gateway": "ledger↔gateway", "gateway_bank": "gateway↔bank"}[pair]
        discs = [{**dict(x), "status": x["status"] or "open"} for x in self.db.execute(
            "SELECT d.*, e.status FROM run_discrepancies d LEFT JOIN exceptions e USING(fingerprint)"
            " WHERE d.run_id=? AND d.pair=? ORDER BY CASE d.severity WHEN 'high' THEN 0"
            " WHEN 'medium' THEN 1 ELSE 2 END, d.kind", (run_id, pair))]
        return {**rep, "run": {k: run[k] for k in ("id", "as_of", "created_at", "label", "sources")},
                "matches": [m for m in self.matches(run_id) if m["layer"] == layer],
                "discrepancies": discs}

    def exceptions(self, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM exceptions", ()
        if status == "active":
            q += " WHERE status IN ('open','investigating','pending_approval')"
        elif status:
            q, args = q + " WHERE status=?", (status,)
        q += (" ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,"
              " kind, fingerprint")
        as_of = {r["id"]: r["as_of"] for r in self.db.execute("SELECT id, as_of FROM runs")}
        latest = max(as_of.values()) if as_of else None
        out = []
        for r in self.db.execute(q, args):
            d = self._row(r)
            first = as_of.get(d["first_seen_run"])
            d["age_days"] = ((dt.date.fromisoformat(latest) - dt.date.fromisoformat(first)).days
                             if first and latest else 0)
            out.append(d)
        return out

    def bulk(self, fps: list[str], action: str, actor: str, note: str = "") -> dict:
        done, failed = [], {}
        for fp in fps:
            try:
                self.act(fp, action, actor, note)
                done.append(fp)
            except ActionError as exc:
                failed[fp] = str(exc)
        return {"done": done, "failed": failed}

    def exception(self, fp: str) -> dict | None:
        r = self.db.execute("SELECT * FROM exceptions WHERE fingerprint=?", (fp,)).fetchone()
        if not r:
            return None
        out = self._row(r)
        out["events"] = [dict(e) for e in self.db.execute(
            "SELECT * FROM events WHERE fingerprint=? ORDER BY id", (fp,))]
        return out

    def events(self, limit: int = 300) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT ev.*, ex.summary, ex.kind FROM events ev LEFT JOIN exceptions ex USING(fingerprint)"
            " ORDER BY ev.id DESC LIMIT ?", (limit,))]

    def review_stats(self) -> dict:
        human = {r["action"]: r["n"] for r in self.db.execute(
            "SELECT action, COUNT(*) n FROM events WHERE actor != 'engine' GROUP BY action")}
        statuses = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) n FROM exceptions GROUP BY status")}
        links = self.db.execute("SELECT COUNT(*) FROM manual_links").fetchone()[0]
        return {"human_actions": human, "statuses": statuses, "manual_links": links}

    def matches(self, run_id: int) -> list[dict]:
        return [{**dict(r), "left_ids": json.loads(r["left_ids"]), "right_ids": json.loads(r["right_ids"]),
                 "factors": json.loads(r["factors"] or "[]")}
                for r in self.db.execute("SELECT * FROM matches WHERE run_id=?", (run_id,))]

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["refs"] = json.loads(d["refs"])
        d["candidates"] = json.loads(d.get("candidates") or "[]")
        return d
