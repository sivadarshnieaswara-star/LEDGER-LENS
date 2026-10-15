"""The reconciliation engine.

Two matching layers, each running through progressively looser strategies:

  ledger ↔ gateway   0. reviewer-confirmed links
                     1. exact: normalised reference (1:1, split captures many-to-one,
                        multi-line orders one-to-many)
                     2. composite: customer + amount + currency + date when references differ
                     3. aggregate: 2–3 records on one side summing to one on the other
                     4. probabilistic: scored candidates (auto-accept or suggest for review)
  gateway ↔ bank     payouts (events grouped per gateway + payout id, summed net) against
                     bank deposits across every bank account: payout ids in narratives
                     (incl. split deposits and batched deposits), then amount + date,
                     then aggregate sums

Every match carries an explainable confidence: a list of weighted factors that
add up to the score. Every rule that fails produces a Discrepancy with a
plain-English explanation and a fingerprint that is stable across runs, so the
store can carry resolution history forward.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from itertools import combinations

from .models import BankLine, Discrepancy, GatewayEvent, LedgerEntry, Match

# kind → (severity, suggested action)
KINDS: dict[str, tuple[str, str]] = {
    "DUPLICATE_CHARGE": ("high", "Customer was charged twice. Refund the extra charge and confirm with the customer."),
    "DUPLICATE_REFUND": ("high", "Money went out twice. Contact the gateway or recover the funds."),
    "UNRECORDED_REFUND": ("high", "Refund happened at the gateway but not in the books. Post the refund to the ledger."),
    "UNRECORDED_CHARGEBACK": ("high", "A chargeback pulled funds but the ledger has no entry. Post it and open a dispute case."),
    "OVER_REFUND": ("high", "More money went back to the customer than was captured. Recover the excess or reverse one of them."),
    "MISSING_SETTLEMENT": ("high", "Payout is overdue and no bank deposit was found. Raise it with the gateway."),
    "PAYOUT_AMOUNT_MISMATCH": ("high", "The bank deposit differs from what the gateway says it paid out. Investigate the gap."),
    "AMOUNT_MISMATCH": ("medium", "Ledger and gateway disagree on the amount. Correct the side that is wrong."),
    "MISSING_IN_GATEWAY": ("medium", "Ledger expects money the gateway never captured. Check whether the payment failed."),
    "MISSING_IN_LEDGER": ("medium", "The gateway captured money the ledger has no record of. Post the entry or investigate."),
    "DUPLICATE_LEDGER_ENTRY": ("medium", "The same entry was posted twice. Reverse the duplicate journal."),
    "CHARGEBACK_OPEN": ("medium", "Submit evidence to the gateway before the response deadline."),
    "UNRECORDED_REVERSAL": ("medium", "A dispute was won and funds returned, but the ledger has no reversal. Post it."),
    "FEE_VARIANCE": ("medium", "Gateway fee differs from the contracted rate. Dispute it or update the rate card."),
    "NET_INCONSISTENT": ("medium", "Gateway net ≠ gross − fee. Ask the gateway for a corrected report."),
    "NOT_PAID_OUT": ("medium", "Captured funds were never assigned to a payout. Check for holds or a reserve."),
    "UNIDENTIFIED_DEPOSIT": ("medium", "Bank deposit with no matching payout. Identify the sender."),
    "PARTIAL_SETTLEMENT": ("low", "Part of the payout has arrived. Watch for the remainder before the due date."),
    "DELAYED_SETTLEMENT": ("low", "Settlement arrived after its SLA. Track it if this keeps happening."),
    "REFERENCE_MISMATCH": ("low", "Matched on amount and date because the references differ. Confirm the pairing."),
    "DUPLICATE_WEBHOOK": ("low", "Gateway reported the same event more than once. It was counted once. No money moved."),
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Which source pair (= reconciliation report) each kind belongs to.
PAIRS = {
    "ledger_gateway": ("Internal ledger", "Gateway settlement feed"),
    "gateway_bank": ("Gateway settlement feed", "Bank settlement"),
}
PAIR_OF_KIND = {k: "gateway_bank" for k in (
    "MISSING_SETTLEMENT", "PAYOUT_AMOUNT_MISMATCH", "UNIDENTIFIED_DEPOSIT", "DELAYED_SETTLEMENT",
    "NOT_PAID_OUT", "PARTIAL_SETTLEMENT")}
PAIR_OF_KIND.update({k: "ledger_gateway" for k in KINDS if k not in PAIR_OF_KIND})
NO_MONEY_KINDS = ("DUPLICATE_WEBHOOK", "DELAYED_SETTLEMENT", "REFERENCE_MISMATCH", "CHARGEBACK_OPEN")
OUTFLOW_KINDS = ("refund", "chargeback")
KIND_NOUN = {"charge": "charge", "refund": "refund", "chargeback": "chargeback",
             "chargeback_reversal": "chargeback reversal"}


@dataclass
class Config:
    settlement_days: int = 2          # expected T+N for payouts
    grace_days: int = 1               # tolerance before a payout counts as late
    fuzzy_window_days: int = 3        # ledger↔gateway fuzzy date window
    payout_window_days: int = 7       # gateway↔bank fuzzy date window
    fee_rate_bp: int = 290            # default contracted fee: 2.90 % …
    fee_fixed: int = 30               # … + 30¢
    fee_tolerance: int = 1            # cents
    gateway_fees: dict = field(default_factory=lambda: {"Paystream": (250, 25)})   # per-gateway rate cards
    auto_match_score: float = 0.80    # probabilistic: accept automatically at/above this…
    auto_match_margin: float = 0.10   # …when the runner-up trails by at least this much
    suggest_score: float = 0.45       # below auto but above this → candidate for human review
    dispute_response_days: int = 7    # time to submit chargeback evidence
    max_aggregate_parts: int = 3      # one-to-many / many-to-one search depth


@dataclass
class Payout:
    id: str
    gateway: str
    currency: str
    expected_amount: int
    expected_date: dt.date
    event_ids: list[str]
    status: str = "unmatched"         # settled | partially_settled | in_transit | missing
    bank_id: str = ""                 # comma-joined when several lines settle one payout
    bank_amount: int | None = None
    bank_date: dt.date | None = None
    bank_account: str = ""
    delay_days: int | None = None     # + late / − early; for unsettled, days overdue


@dataclass
class Result:
    as_of: dt.date
    matches: list[Match]
    discrepancies: list[Discrepancy]
    payouts: list[Payout]
    traces: list[dict]
    stats: dict = field(default_factory=dict)
    reports: list[dict] = field(default_factory=list)
    pipeline: dict = field(default_factory=dict)
    disputes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "stats": self.stats,
            "matches": [asdict(m) for m in self.matches],
            "discrepancies": [asdict(d) for d in self.discrepancies],
            "payouts": [_jsonable(asdict(p)) for p in self.payouts],
            "traces": self.traces,
            "reports": self.reports,
            "pipeline": self.pipeline,
            "disputes": self.disputes,
        }


def _jsonable(d: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v) for k, v in d.items()}


def norm_ref(ref: str) -> str:
    """'ORD-01001', 'ord_1001', 'Order #1001' → '1001'."""
    s = re.sub(r"[^a-z0-9]", "", (ref or "").lower())
    s = re.sub(r"^(order|ord|invoice|inv)", "", s)
    return s.lstrip("0") or s


def money(cents: int, ccy: str = "") -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}{ccy + ' ' if ccy else ''}{abs(cents) / 100:,.2f}"


# ------------------------------------------------------------ explainability
def factor(name: str, weight: float, score: float, detail: str) -> dict:
    score = max(0.0, min(1.0, score))
    return {"factor": name, "weight": weight, "score": round(score, 3),
            "contribution": round(weight * score, 3), "detail": detail}


def confidence(factors: list[dict]) -> float:
    return round(min(1.0, sum(f["contribution"] for f in factors)), 2)


def amount_score(expected: int, actual: int) -> float:
    diff = abs(expected - actual)
    return 1.0 if diff == 0 else max(0.0, 1 - diff / max(abs(expected), 1) * 5)   # 20 % off → 0


def date_score(days: float, window: int) -> float:
    return max(0.0, 1 - abs(days) / (window + 1))


def _days(a: dt.datetime, b: dt.datetime) -> float:
    return abs((a - b).total_seconds()) / 86400


def _amount_detail(a: int, b: int) -> str:
    return "identical" if a == b else f"differs by {money(abs(a - b))}"


def _date_detail(days: float) -> str:
    return "same time" if days < 0.1 else f"{days:.1f} days apart"


def score_candidate(l: LedgerEntry, e: GatewayEvent, cfg: Config) -> tuple[float, list[str], list[dict]]:
    """Likelihood (0–1) that a ledger entry and a gateway event are the same payment,
    as weighted, human-readable factors. Amount dominates; date and reference text
    break ties."""
    if e.currency != l.currency:
        f = [factor("Currency", 1.0, 0.0, f"{l.currency} ≠ {e.currency}")]
        return 0.0, [f[0]["detail"]], f
    days = _days(e.ts, l.ts)
    ref = SequenceMatcher(None, norm_ref(l.ref), norm_ref(e.ref)).ratio()
    fs = [factor("Amount", 0.55, amount_score(l.amount, e.gross), _amount_detail(l.amount, e.gross)),
          factor("Date proximity", 0.30, date_score(days, cfg.fuzzy_window_days), _date_detail(days)),
          factor("Reference similarity", 0.15, ref, f"{l.ref!r} vs {e.ref!r} ({ref:.0%})")]
    reasons = [f"amount {fs[0]['detail']}", fs[1]["detail"], f"reference similarity {ref:.0%}"]
    return sum(f["contribution"] for f in fs), reasons, fs


def _subset_sums(items: list, target: int, value, max_parts: int) -> list[tuple]:
    """All combinations of 2..max_parts items whose values sum exactly to target."""
    pool = [x for x in items if 0 < value(x) < target][:25]   # bound the search
    found = []
    for n in range(2, max_parts + 1):
        for combo in combinations(pool, n):
            if sum(value(x) for x in combo) == target:
                found.append(combo)
                if len(found) > 1:
                    return found
    return found


class Reconciler:
    def __init__(self, ledger: list[LedgerEntry], gateway: list[GatewayEvent],
                 bank: list[BankLine], as_of: dt.date, config: Config | None = None,
                 manual_links: list[tuple[str, str]] | None = None):
        self.manual_links = manual_links or []
        self.pipeline: dict = {
            "ingestion": {},
            "deterministic": {"ledger_gateway": 0, "gateway_bank": 0, "manual": 0, "composite": 0,
                              "one_to_many": 0, "many_to_one": 0},
            "probabilistic": {"evaluated": 0, "auto_matched": 0, "aggregate": 0, "suggested": 0,
                              "no_candidate": 0, "payouts_matched": 0},
            "classifier": {}}
        self.ledger_raw = ledger
        self.gateway_raw = gateway
        self.bank = bank
        self.as_of = as_of
        self.cfg = config or Config()
        self.matches: list[Match] = []
        self.found: dict[str, Discrepancy] = {}
        self.gw_link: dict[str, str] = {}       # gateway id → (anchor) ledger id
        self.l_done: set[str] = set()
        self.gw_dupes: dict[str, int] = {}      # gateway id → extra copies seen
        self.payouts: dict[str, Payout] = {}    # "gateway:payout_id" → Payout
        self.payout_of_event: dict[str, Payout] = {}
        self.pending_events: list[GatewayEvent] = []
        self.disputes: list[dict] = []

    # ------------------------------------------------------------------ util
    def flag(self, kind: str, key: str, summary: str, explanation: str,
             refs: dict[str, list[str]], delta: int = 0, ccy: str = "",
             candidates: list[dict] | None = None) -> None:
        sev, action = KINDS[kind]
        fp = f"{kind}:{key}"
        self.found[fp] = Discrepancy(kind, sev, fp, summary, explanation, action,
                                     {k: v for k, v in refs.items() if v}, delta, ccy,
                                     PAIR_OF_KIND[kind], candidates or [])

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        self._dedupe_gateway()
        self._dedupe_ledger()
        self._match_ledger_gateway()
        self._check_fees()
        self._check_lifecycle()
        self._match_payouts()
        discrepancies = sorted(self.found.values(),
                               key=lambda d: (SEVERITY_ORDER[d.severity], d.kind, d.fingerprint))
        traces = self._traces()
        res = Result(self.as_of, self.matches, discrepancies,
                     list(self.payouts.values()), traces, disputes=self.disputes)
        res.stats = self._stats(res)
        self.pipeline["ingestion"] = {
            "ledger": len(self.ledger_raw), "gateway": len(self.gateway_raw), "bank": len(self.bank),
            "gateways": sorted({e.gateway for e in self.gateway_raw}),
            "bank_accounts": sorted({b.account for b in self.bank}),
            "duplicates_removed": sum(self.gw_dupes.values()) + sum(map(len, self.ledger_dupes.values())),
            "references_normalised": sum(1 for r in [*self.ledger_raw, *self.gw] if norm_ref(r.ref) != r.ref)}
        self.pipeline["classifier"] = res.stats["by_kind"]
        res.pipeline = self.pipeline
        res.reports = self._reports(res)
        return res

    # -------------------------------------------------------- deduplication
    def _dedupe_gateway(self) -> None:
        seen: dict[str, GatewayEvent] = {}
        for e in self.gateway_raw:
            if e.id in seen:
                self.gw_dupes[e.id] = self.gw_dupes.get(e.id, 0) + 1
            else:
                seen[e.id] = e
        self.gw = list(seen.values())
        for tid, extra in self.gw_dupes.items():
            e = seen[tid]
            self.flag("DUPLICATE_WEBHOOK", tid,
                      f"{tid} appears {extra + 1}× in the {e.gateway} report",
                      f"Transaction {tid} ({e.ref}, {money(e.gross, e.currency)}) was delivered "
                      f"{extra + 1} times by {e.gateway}, probably because a webhook was retried. Only "
                      f"one copy was counted toward the payout. Adding up the raw rows would "
                      f"overstate gateway volume by {money(e.net * extra, e.currency)}.",
                      {"gateway": [tid]}, e.net * extra, e.currency)

    def _dedupe_ledger(self) -> None:
        groups: dict[tuple, list[LedgerEntry]] = defaultdict(list)
        for l in self.ledger_raw:
            groups[(norm_ref(l.ref), l.kind, l.amount, l.currency)].append(l)
        self.ledger: list[LedgerEntry] = []
        self.ledger_dupes: dict[str, list[str]] = {}
        for (_, kind, amount, ccy), ls in groups.items():
            self.ledger.append(ls[0])
            if len(ls) > 1:
                ids = [l.id for l in ls]
                self.ledger_dupes[ls[0].id] = ids[1:]
                self.flag("DUPLICATE_LEDGER_ENTRY", ls[0].ref + ":" + kind,
                          f"{ls[0].ref} {KIND_NOUN[kind]} posted {len(ls)}× in the ledger",
                          f"Ledger entries {', '.join(ids)} all record a {money(amount, ccy)} "
                          f"{KIND_NOUN[kind]} for {ls[0].ref}. The gateway shows one, so the books "
                          f"are overstated by {money(amount * (len(ls) - 1), ccy)}.",
                          {"ledger": ids}, amount * (len(ls) - 1), ccy)

    # ------------------------------------------------- ledger ↔ gateway
    def _link(self, ls: list[LedgerEntry], es: list[GatewayEvent], method: str,
              factors: list[dict], why: str, conf: float | None = None) -> Match:
        conf = confidence(factors) if conf is None else conf
        m = Match("ledger↔gateway", [l.id for l in ls], [e.id for e in es], method, conf, why, factors)
        self.matches.append(m)
        for e in es:
            self.gw_link[e.id] = ls[0].id
        self.l_done.update(l.id for l in ls)
        led_total, gw_total = sum(l.amount for l in ls), sum(e.gross for e in es)
        l_ccy, g_ccy = ls[0].currency, es[0].currency
        if l_ccy != g_ccy or led_total != gw_total:
            delta = gw_total - led_total
            lref = " + ".join(l.ref for l in ls) if len(ls) > 1 else ls[0].ref
            gids = " + ".join(e.id for e in es)
            cur_note = (f" They also use different currencies ({l_ccy} vs {g_ccy}), which "
                        f"points to an FX conversion problem." if l_ccy != g_ccy else "")
            partial = (" The gateway captured less than the ledger expects, which usually means a "
                       "partial capture, a discount applied after checkout, or a typo when the entry "
                       "was posted." if delta < 0 else
                       " The gateway captured more than the ledger expects: check for a tip, a "
                       "surcharge or a mis-keyed amount.")
            self.flag("AMOUNT_MISMATCH", f"{'+'.join(m.left)}:{'+'.join(m.right)}",
                      f"{lref}: ledger {money(led_total, l_ccy)} vs gateway {money(gw_total, g_ccy)}",
                      f"The ledger recorded {money(led_total, l_ccy)} for {lref}, but the gateway "
                      f"has {money(gw_total, g_ccy)} in {gids}, a difference of "
                      f"{money(abs(delta))}.{partial}{cur_note}",
                      {"ledger": m.left, "gateway": m.right}, delta, g_ccy)
        return m

    def _exact_factors(self, ls, es, ref_detail: str) -> list[dict]:
        led, gw = sum(l.amount for l in ls), sum(e.gross for e in es)
        days = max(_days(l.ts, e.ts) for l in ls for e in es)
        return [factor("Reference", 0.60, 1.0, ref_detail),
                factor("Amount", 0.25, amount_score(led, gw), _amount_detail(led, gw)),
                factor("Date proximity", 0.15, date_score(days, self.cfg.fuzzy_window_days), _date_detail(days))]

    def _match_ledger_gateway(self) -> None:
        cfg = self.cfg
        det, prob = self.pipeline["deterministic"], self.pipeline["probabilistic"]
        # stage 0: links a human reviewer confirmed in the exception queue
        led_ids = {l.id: l for l in self.ledger}
        gw_ids = {e.id: e for e in self.gw}
        for lid, gid in self.manual_links:
            if lid in led_ids and gid in gw_ids and lid not in self.l_done and gid not in self.gw_link:
                self._link([led_ids[lid]], [gw_ids[gid]], "manual",
                           [factor("Reviewer confirmation", 1.0, 1.0, "Confirmed in the exception queue")],
                           "Confirmed by a reviewer in the exception queue")
                det["manual"] += 1

        # stage 1: exact, by normalised reference (+ split captures and multi-line orders)
        gw_by_key: dict[tuple, list[GatewayEvent]] = defaultdict(list)
        for e in self.gw:
            if e.id not in self.gw_link:
                gw_by_key[(norm_ref(e.ref), e.kind)].append(e)
        led_by_key: dict[tuple, list[LedgerEntry]] = defaultdict(list)
        for l in self.ledger:
            if l.id not in self.l_done:
                led_by_key[(norm_ref(l.ref), l.kind)].append(l)

        unmatched: list[LedgerEntry] = []
        for key, ls in led_by_key.items():
            pool = list(gw_by_key.get(key, []))
            if not pool or not key[0]:
                unmatched.extend(ls)
                continue
            ls = sorted(ls, key=lambda x: x.ts)
            # one ledger entry settled by several gateway captures (split / partial captures)
            if (len(ls) == 1 and len(pool) > 1 and sum(e.gross for e in pool) == ls[0].amount
                    and all(e.gross != ls[0].amount for e in pool)):
                self._link(ls, pool, "exact · many-to-one",
                           self._exact_factors(ls, pool, f"all {len(pool)} captures carry {ls[0].ref!r}"),
                           f"{len(pool)} captures for {ls[0].ref} ({' + '.join(money(e.gross) for e in pool)}) "
                           f"add up to the ledger amount {money(ls[0].amount)}: a split capture")
                det["many_to_one"] += 1
                continue
            # several ledger lines for one gateway capture (order booked in parts)
            if (len(ls) > 1 and len(pool) == 1 and sum(l.amount for l in ls) == pool[0].gross
                    and all(l.amount != pool[0].gross for l in ls)):
                self._link(ls, pool, "exact · one-to-many",
                           self._exact_factors(ls, pool, f"all {len(ls)} ledger lines carry {pool[0].ref!r}"),
                           f"{len(ls)} ledger lines for {ls[0].ref} add up to the single gateway capture "
                           f"{pool[0].id} of {money(pool[0].gross)}")
                det["one_to_many"] += 1
                continue
            for l in ls:
                if not pool:
                    unmatched.append(l)
                    continue
                pool.sort(key=lambda e: (e.gross != l.amount, abs((e.ts - l.ts).total_seconds())))
                e = pool.pop(0)
                same_text = l.ref == e.ref
                self._link([l], [e], "reference",
                           self._exact_factors([l], [e], "identical" if same_text else
                                               f"{l.ref!r} ≡ {e.ref!r} after normalising"),
                           f"Reference {l.ref!r} ↔ {e.ref!r}"
                           + ("" if same_text else " (same reference after normalising format)"))
                det["ledger_gateway"] += 1
            for extra in pool:   # more gateway events than ledger entries for this reference
                anchor = ls[0]
                self.gw_link[extra.id] = anchor.id
                first = next(m.right[0] for m in self.matches if anchor.id in m.left)
                gap = abs((extra.ts - anchor.ts).total_seconds()) / 60
                kind = "DUPLICATE_CHARGE" if extra.kind == "charge" else "DUPLICATE_REFUND"
                noun = KIND_NOUN[extra.kind]
                self.flag(kind, f"{anchor.ref}:{extra.id}",
                          f"{anchor.ref}: {noun} recorded twice at {extra.gateway} ({first}, {extra.id})",
                          f"The ledger has one {noun} of {money(anchor.amount, anchor.currency)} for "
                          f"{anchor.ref}, but {extra.gateway} has a second distinct {noun} {extra.id} "
                          f"of {money(extra.gross, extra.currency)}, {gap:.0f} min apart. The transaction "
                          f"ids are different, so this is not a webhook replay. Money really moved "
                          f"twice, probably from a double-submitted checkout or a client retry.",
                          {"ledger": [anchor.id], "gateway": [first, extra.id]},
                          extra.net, extra.currency)

        orphans = [e for k, es in gw_by_key.items() if k not in led_by_key or not k[0] for e in es]
        claimed: set[str] = set()

        def take(es):
            claimed.update(e.id for e in es)

        def compatible(l: LedgerEntry, e: GatewayEvent) -> bool:
            return (e.kind == l.kind and e.currency == l.currency
                    and _days(e.ts, l.ts) <= cfg.fuzzy_window_days
                    and (not l.customer or not e.customer or l.customer == e.customer))

        # stage 2: composite key — customer + amount + currency + date
        for l in list(unmatched):
            if not l.customer:
                continue
            cands = [e for e in orphans if e.id not in claimed and e.customer == l.customer
                     and e.kind == l.kind and e.currency == l.currency and e.gross == l.amount
                     and _days(e.ts, l.ts) <= 1]
            if len(cands) == 1:
                e = cands[0]
                days = _days(e.ts, l.ts)
                self._link([l], [e], "composite", [
                    factor("Customer", 0.35, 1.0, f"{l.customer} on both sides"),
                    factor("Amount", 0.35, 1.0, "identical"),
                    factor("Date proximity", 0.20, date_score(days, 1), _date_detail(days)),
                    factor("Currency", 0.10, 1.0, l.currency)],
                    f"Composite key match: customer {l.customer}, {money(l.amount, l.currency)}, "
                    f"{_date_detail(days)}. Reference {e.ref or '(blank)'!r} could not be used")
                take([e])
                unmatched.remove(l)
                det["composite"] += 1

        # stage 3a: aggregate one-to-many — one gateway capture pays several ledger entries
        for e in orphans:
            if e.id in claimed:
                continue
            pool = [l for l in unmatched if compatible(l, e)]
            combos = _subset_sums(pool, e.gross, lambda l: l.amount, cfg.max_aggregate_parts)
            if len(combos) == 1:
                ls = list(combos[0])
                same_cust = bool(e.customer) and all(l.customer == e.customer for l in ls)
                worst = max(_days(l.ts, e.ts) for l in ls)
                self._link(ls, [e], "aggregate · one-to-many", [
                    factor("Sum of amounts", 0.50, 1.0, f"{' + '.join(money(l.amount) for l in ls)} = {money(e.gross)}"),
                    factor("Date window", 0.20, date_score(worst, cfg.fuzzy_window_days), _date_detail(worst)),
                    factor("Customer", 0.20, 1.0 if same_cust else 0.4,
                           f"all {e.customer}" if same_cust else "not available on both sides"),
                    factor("Unique combination", 0.10, 1.0, "no other subset sums to the same total")],
                    f"One {e.gateway} capture {e.id} ({e.ref!r}) settles {len(ls)} ledger orders "
                    f"({', '.join(l.ref for l in ls)}): a consolidated checkout")
                take([e])
                for l in ls:
                    unmatched.remove(l)
                prob["aggregate"] += 1

        # stage 3b: aggregate many-to-one — several captures pay one ledger entry
        for l in list(unmatched):
            pool = [e for e in orphans if e.id not in claimed and compatible(l, e)]
            combos = _subset_sums(pool, l.amount, lambda e: e.gross, cfg.max_aggregate_parts)
            if len(combos) == 1:
                es = list(combos[0])
                worst = max(_days(l.ts, e.ts) for e in es)
                same_cust = bool(l.customer) and all(e.customer == l.customer for e in es)
                self._link([l], es, "aggregate · many-to-one", [
                    factor("Sum of amounts", 0.50, 1.0, f"{' + '.join(money(e.gross) for e in es)} = {money(l.amount)}"),
                    factor("Date window", 0.20, date_score(worst, cfg.fuzzy_window_days), _date_detail(worst)),
                    factor("Customer", 0.20, 1.0 if same_cust else 0.4,
                           f"all {l.customer}" if same_cust else "not available on both sides"),
                    factor("Unique combination", 0.10, 1.0, "no other subset sums to the same total")],
                    f"{len(es)} gateway captures add up to {l.ref} ({money(l.amount)}): instalments "
                    f"or a split tender")
                take(es)
                unmatched.remove(l)
                prob["aggregate"] += 1

        # stage 4: probabilistic candidate matching over whatever is left
        orphans = [e for e in orphans if e.id not in claimed]
        prob["evaluated"] = len(unmatched) + len(orphans)
        scored = {l.id: sorted(((score_candidate(l, e, cfg), e) for e in orphans if e.kind == l.kind),
                               key=lambda x: -x[0][0]) for l in unmatched}
        suggestions: dict[str, list[dict]] = defaultdict(list)   # gateway id → ledger candidates
        # most confident ledger entries pick first, so a strong match is never stolen by a weak one
        for l in sorted(unmatched, key=lambda l: -(scored[l.id][0][0][0] if scored[l.id] else 0)):
            ranked = [(s, e) for s, e in scored[l.id] if e.id not in claimed]
            top = ranked[0] if ranked else None
            runner = ranked[1][0][0] if len(ranked) > 1 else 0.0
            if top and top[0][0] >= cfg.auto_match_score and top[0][0] - runner >= cfg.auto_match_margin:
                (score, reasons, fs), e = top
                take([e])
                prob["auto_matched"] += 1
                fs = fs + [factor("Margin over runner-up", 0.0, 1.0, f"next best {runner:.0%}")]
                self._link([l], [e], "probabilistic", fs,
                           f"Scored {score:.0%} ({'; '.join(reasons)}), next best {runner:.0%}",
                           conf=round(score, 2))
                self.flag("REFERENCE_MISMATCH", f"{l.id}:{e.id}",
                          f"{l.ref} matched to {e.id} whose reference is {e.ref!r}",
                          f"No gateway record carries reference {l.ref!r}. Probabilistic matching "
                          f"scored {e.id} at {score:.0%} ({'; '.join(reasons)}), and the next best "
                          f"candidate scored {runner:.0%}, so the pair was accepted automatically. "
                          f"The gateway reference {e.ref!r} was probably entered by hand or "
                          f"overwritten by an integration.",
                          {"ledger": [l.id], "gateway": [e.id]}, 0, l.currency)
                continue
            cands = []
            for (s, r, fs), e in ranked[:3]:
                if s < cfg.suggest_score:
                    continue
                cands.append({"id": e.id, "score": round(s, 2), "reasons": r, "factors": fs, "side": "gateway",
                              "label": f"{e.gateway} · {e.ref} · {money(e.gross, e.currency)} · {e.ts:%Y-%m-%d}"})
                suggestions[e.id].append({"id": l.id, "score": round(s, 2), "reasons": r, "factors": fs,
                                          "side": "ledger",
                                          "label": f"{l.ref} · {money(l.amount, l.currency)} · {l.ts:%Y-%m-%d}"})
            prob["suggested" if cands else "no_candidate"] += 1
            why = (f"the closest gateway record, {cands[0]['id']}, scored only {cands[0]['score']:.0%}, "
                   f"which is below the {cfg.auto_match_score:.0%} needed to match automatically. "
                   f"Review the suggested candidates" if cands else
                   "no gateway record has that reference, a matching customer, a summing combination, "
                   "or a similar amount and date")
            noun = KIND_NOUN[l.kind]
            if l.kind == "charge":
                expl = (f"The ledger booked a {money(l.amount, l.currency)} charge for {l.ref} on "
                        f"{l.ts:%Y-%m-%d}, but {why}. If nothing matches, the payment may have failed "
                        f"or been abandoned after the order was posted.")
            else:
                expl = (f"The ledger records a {money(l.amount, l.currency)} {noun} for {l.ref}, "
                        f"but {why}. Check whether it was actually processed by the gateway.")
            self.flag("MISSING_IN_GATEWAY", l.id, f"{l.ref} {noun} not found at any gateway", expl,
                      {"ledger": [l.id]}, -l.amount, l.currency, cands)

        for e in orphans:
            if e.id in claimed:
                continue
            cands = sorted(suggestions.get(e.id, []), key=lambda c: -c["score"])
            if not cands:
                prob["no_candidate"] += 1
            hint = (f" A possible ledger counterpart, {cands[0]['id']}, scored {cands[0]['score']:.0%}. "
                    f"Confirm it if it is the same payment." if cands else "")
            refs = {"gateway": [e.id]}
            if e.kind == "refund":
                self.flag("UNRECORDED_REFUND", e.id, f"Refund {e.id} ({e.ref}) missing from ledger",
                          f"{e.gateway} refunded {money(e.gross, e.currency)} against {e.ref} on "
                          f"{e.ts:%Y-%m-%d} and took it out of payout {e.payout_id or '(none)'}. The "
                          f"ledger has no matching refund, so recorded revenue is overstated by "
                          f"that amount.{hint}", refs, -e.gross, e.currency, cands)
            elif e.kind == "chargeback":
                self.flag("UNRECORDED_CHARGEBACK", e.id, f"Chargeback {e.id} ({e.ref}) missing from ledger",
                          f"The cardholder disputed {e.ref} and {e.gateway} withdrew "
                          f"{money(e.gross, e.currency)} plus a {money(e.fee, e.currency)} dispute fee on "
                          f"{e.ts:%Y-%m-%d}. The ledger still shows the sale as good revenue.{hint}",
                          refs, -(e.gross + e.fee), e.currency, cands)
            elif e.kind == "chargeback_reversal":
                self.flag("UNRECORDED_REVERSAL", e.id, f"Dispute won: reversal {e.id} ({e.ref}) not in ledger",
                          f"{e.gateway} returned {money(e.gross, e.currency)} on {e.ts:%Y-%m-%d} after a "
                          f"dispute on {e.ref} was decided in the merchant's favour. The ledger has no "
                          f"reversal entry, so revenue is understated.{hint}",
                          refs, e.gross, e.currency, cands)
            else:
                self.flag("MISSING_IN_LEDGER", e.id, f"{e.gateway} charge {e.id} ({e.ref or 'no ref'}) not in ledger",
                          f"{e.gateway} captured {money(e.gross, e.currency)} for reference "
                          f"{e.ref!r} on {e.ts:%Y-%m-%d}. No ledger entry matches its reference, "
                          f"customer, amount combination, or scores high enough on amount and date. "
                          f"Possible causes are an order created outside the main system, a test "
                          f"transaction in live mode, or a failed ledger write.{hint}",
                          refs, e.gross, e.currency, cands)

    # ------------------------------------------------------------- fees
    def fee_card(self, gateway: str) -> tuple[int, int]:
        return self.cfg.gateway_fees.get(gateway, (self.cfg.fee_rate_bp, self.cfg.fee_fixed))

    def expected_fee(self, gross: int, gateway: str = "") -> int:
        bp, fixed = self.fee_card(gateway)
        return (gross * bp + 5000) // 10000 + fixed

    def _check_fees(self) -> None:
        for e in self.gw:
            if e.kind != "charge":
                continue
            exp = self.expected_fee(e.gross, e.gateway)
            bp, fixed = self.fee_card(e.gateway)
            if abs(e.fee - exp) > self.cfg.fee_tolerance:
                rate = e.fee / e.gross * 100 if e.gross else 0
                self.flag("FEE_VARIANCE", e.id,
                          f"{e.id} fee {money(e.fee, e.currency)}, expected {money(exp, e.currency)}",
                          f"The {e.gateway} contract is {bp / 100:.2f}% + {money(fixed)}, so a "
                          f"{money(e.gross, e.currency)} charge should cost {money(exp, e.currency)}. "
                          f"The gateway took {money(e.fee, e.currency)} (an effective {rate:.2f}%), "
                          f"which is {money(e.fee - exp, e.currency)} {'over' if e.fee > exp else 'under'}. "
                          f"Possible causes are an international or premium-card surcharge, or a rate "
                          f"change nobody recorded.",
                          {"gateway": [e.id]}, -(e.fee - exp), e.currency)
            if e.net != e.gross - e.fee:
                self.flag("NET_INCONSISTENT", e.id, f"{e.id} net ≠ gross − fee",
                          f"{money(e.gross)} − {money(e.fee)} should be {money(e.gross - e.fee)}, but the "
                          f"report says the net is {money(e.net)}.",
                          {"gateway": [e.id]}, e.net - (e.gross - e.fee), e.currency)

    # ---------------------------------------- refunds, chargebacks, reversals
    def _order_key(self, e: GatewayEvent) -> str:
        led = {l.id: l for l in self.ledger_raw}
        anchor = led.get(self.gw_link.get(e.id, ""))
        return norm_ref(anchor.ref if anchor else e.ref) or e.id

    def _check_lifecycle(self) -> None:
        """Per order: captured vs refunded vs charged back vs reversed. Flags over-refunds
        and tracks every dispute with its evidence deadline."""
        orders: dict[str, list[GatewayEvent]] = defaultdict(list)
        for e in self.gw:
            orders[self._order_key(e)].append(e)
        self.order_state: dict[str, dict] = {}
        for key, evs in orders.items():
            tot = defaultdict(int)
            for e in evs:
                tot[e.kind] += e.gross
            captured, refunded = tot["charge"], tot["refund"]
            charged_back, reversed_ = tot["chargeback"], tot["chargeback_reversal"]
            outflow = refunded + charged_back - reversed_
            ccy = evs[0].currency
            ref = next((e.ref for e in evs if e.kind == "charge"), evs[0].ref)
            self.order_state[key] = {"captured": captured, "refunded": refunded,
                                     "charged_back": charged_back, "reversed": reversed_,
                                     "status": ("fully refunded" if captured and refunded >= captured else
                                                "partially refunded" if refunded else "")}
            if captured and outflow > captured:
                ids = [e.id for e in evs]
                self.flag("OVER_REFUND", key,
                          f"Order {ref}: {money(outflow, ccy)} returned on a {money(captured, ccy)} capture",
                          f"{ref} captured {money(captured, ccy)}, but {money(refunded, ccy)} was refunded "
                          f"and {money(charged_back, ccy)} charged back"
                          + (f" ({money(reversed_, ccy)} reversed)" if reversed_ else "")
                          + f". The customer has been compensated {money(outflow - captured, ccy)} more "
                          f"than they paid, typically a refund issued while a chargeback was already "
                          f"in flight.", {"gateway": ids}, -(outflow - captured), ccy)
            for cb in (e for e in evs if e.kind == "chargeback"):
                rev = next((e for e in evs if e.kind == "chargeback_reversal"), None)
                deadline = cb.ts.date() + dt.timedelta(days=self.cfg.dispute_response_days)
                left = (deadline - self.as_of).days
                status = "won" if rev else ("open" if left >= 0 else "expired")
                self.disputes.append({
                    "id": cb.id, "ref": ref, "gateway": cb.gateway, "amount": cb.gross, "fee": cb.fee,
                    "currency": cb.currency, "opened": cb.ts.date().isoformat(),
                    "deadline": deadline.isoformat(), "days_left": left, "status": status,
                    "reversal_id": rev.id if rev else "", "recorded_in_ledger": cb.id in self.gw_link,
                    "reversed_on": rev.ts.date().isoformat() if rev else None})
                if not rev:
                    when = (f"Evidence is due by {deadline} ({left} day{'s' if left != 1 else ''} left)."
                            if left >= 0 else
                            f"The evidence deadline passed on {deadline}, so the dispute will likely "
                            f"be lost; write the amount off.")
                    self.flag("CHARGEBACK_OPEN", cb.id,
                              f"Open dispute on {ref}: {money(cb.gross, cb.currency)} at {cb.gateway}",
                              f"The cardholder disputed {ref} on {cb.ts:%Y-%m-%d} and {cb.gateway} "
                              f"withdrew {money(cb.gross, cb.currency)} plus a "
                              f"{money(cb.fee, cb.currency)} fee. No reversal has been received yet. "
                              f"{when}", {"gateway": [cb.id]}, -(cb.gross + cb.fee), cb.currency)

    # ------------------------------------------------------ gateway ↔ bank
    def _match_payouts(self) -> None:
        cfg = self.cfg
        groups: dict[str, list[GatewayEvent]] = defaultdict(list)
        for e in self.gw:
            if e.payout_id:
                groups[f"{e.gateway}:{e.payout_id.lower()}"].append(e)
            else:
                self.pending_events.append(e)
        for key, evs in groups.items():
            p = Payout(id=evs[0].payout_id, gateway=evs[0].gateway, currency=evs[0].currency,
                       expected_amount=sum(e.net for e in evs),
                       expected_date=max(e.ts.date() for e in evs) + dt.timedelta(days=cfg.settlement_days),
                       event_ids=[e.id for e in evs])
            self.payouts[key] = p
            for e in evs:
                self.payout_of_event[e.id] = p

        ids_seen = defaultdict(list)
        for key, p in self.payouts.items():
            ids_seen[p.id.lower()].append(key)
        index = {pid: keys[0] for pid, keys in ids_seen.items() if len(keys) == 1}   # skip ambiguous ids
        bank_left = {b.id: b for b in self.bank}
        named: dict[str, list[BankLine]] = defaultdict(list)
        for b in self.bank:
            desc = b.description.lower()
            keys = [k for pid, k in index.items()
                    if re.search(r"(?<![a-z0-9])" + re.escape(pid) + r"(?![a-z0-9])", desc)]
            if len(keys) == 1:
                named[keys[0]].append(b)
            elif len(keys) > 1:   # one deposit settling several payouts
                ps = [self.payouts[k] for k in keys if not self.payouts[k].bank_id]
                if ps:
                    self._settle(ps, [b], "payout ids · one-to-many", "Bank narrative names "
                                 + ", ".join(p.id for p in ps), named_ids=True)
                    self.pipeline["deterministic"]["one_to_many"] += 1
                    bank_left.pop(b.id)
        for key, lines in named.items():
            p = self.payouts[key]
            if p.bank_id:
                continue
            many = len(lines) > 1
            self._settle([p], lines, "payout id" + (" · many-to-one" if many else ""),
                         f"Bank narrative{'s' if many else ''} "
                         + ", ".join(repr(b.description) for b in lines) + f" name{'' if many else 's'} {p.id}"
                         + (f": settled in {len(lines)} parts" if many else ""), named_ids=True)
            self.pipeline["deterministic"]["many_to_one" if many else "gateway_bank"] += 1
            for b in lines:
                bank_left.pop(b.id, None)

        def in_window(b: BankLine, p: Payout) -> bool:
            return (b.currency == p.currency
                    and -1 <= (b.value_date - p.expected_date).days <= cfg.payout_window_days)

        # amount + date, single line
        for p in self.payouts.values():
            if p.bank_id:
                continue
            cands = [b for b in bank_left.values() if b.amount == p.expected_amount and in_window(b, p)]
            if len(cands) == 1:
                b = cands[0]
                off = (b.value_date - p.expected_date).days
                self.pipeline["probabilistic"]["payouts_matched"] += 1
                self._settle([p], [b], "probabilistic",
                             f"Exact amount {money(b.amount, b.currency)}, {off:+d} days from the "
                             f"expected date. The narrative {b.description!r} has no payout id.")
                bank_left.pop(b.id)
        # aggregate: one bank line = several payouts (batched transfer)
        for b in list(bank_left.values()):
            if b.amount <= 0:
                continue
            pool = [p for p in self.payouts.values() if not p.bank_id and in_window(b, p)]
            combos = _subset_sums(pool, b.amount, lambda p: p.expected_amount, cfg.max_aggregate_parts)
            if len(combos) == 1:
                ps = list(combos[0])
                self.pipeline["probabilistic"]["aggregate"] += 1
                self._settle(ps, [b], "aggregate · one-to-many",
                             f"{b.id} equals the sum of payouts {', '.join(p.id for p in ps)}: a batched transfer")
                bank_left.pop(b.id)
        # aggregate: several bank lines = one payout (split transfer)
        for p in self.payouts.values():
            if p.bank_id:
                continue
            pool = [b for b in bank_left.values() if b.amount > 0 and in_window(b, p)]
            combos = _subset_sums(pool, p.expected_amount, lambda b: b.amount, cfg.max_aggregate_parts)
            if len(combos) == 1:
                lines = list(combos[0])
                self.pipeline["probabilistic"]["aggregate"] += 1
                self._settle([p], lines, "aggregate · many-to-one",
                             f"{' + '.join(b.id for b in lines)} add up to payout {p.id}: a split transfer")
                for b in lines:
                    bank_left.pop(b.id)

        for p in self.payouts.values():
            if p.bank_id:
                continue
            due = p.expected_date + dt.timedelta(days=cfg.grace_days)
            refs = {"payout": [p.id], "gateway": p.event_ids}
            if self.as_of > due:
                p.status = "missing"
                p.delay_days = (self.as_of - p.expected_date).days
                self.flag("MISSING_SETTLEMENT", p.id,
                          f"{p.gateway} payout {p.id} ({money(p.expected_amount, p.currency)}) never reached the bank",
                          f"Payout {p.id} from {p.gateway} covers {len(p.event_ids)} transactions with a "
                          f"net of {money(p.expected_amount, p.currency)}. It was due on "
                          f"{p.expected_date} (T+{cfg.settlement_days}) and is now {p.delay_days} days "
                          f"overdue. No deposit in any bank account matches it by payout id, amount, "
                          f"or a summing combination within {cfg.payout_window_days} days. Possible "
                          f"causes are a gateway hold, a rejected transfer, or changed bank details.",
                          refs, -p.expected_amount, p.currency)
            else:
                p.status = "in_transit"

        for b in bank_left.values():
            if b.amount <= 0:
                continue
            self.flag("UNIDENTIFIED_DEPOSIT", b.id,
                      f"{b.account} deposit {b.id} ({money(b.amount, b.currency)}) has no source",
                      f"{money(b.amount, b.currency)} arrived in {b.account} on {b.value_date} with "
                      f"narrative {b.description!r}. No gateway payout matches it by id, by amount, or "
                      f"as a sum of payouts. It could be a direct customer transfer, a payment from "
                      f"another processor, or a gateway adjustment that is not in the report.",
                      {"bank": [b.id]}, b.amount, b.currency)

        for e in self.pending_events:
            due = e.ts.date() + dt.timedelta(days=cfg.settlement_days + cfg.grace_days)
            if self.as_of > due:
                self.flag("NOT_PAID_OUT", e.id, f"{e.id} captured but never assigned to a payout",
                          f"{e.id} ({e.ref}, net {money(e.net, e.currency)}) was captured by "
                          f"{e.gateway} on {e.ts:%Y-%m-%d} and is still not in any payout. Possible "
                          f"causes are a risk hold, a rolling reserve, or a pending dispute.",
                          {"gateway": [e.id]}, -e.net, e.currency)

    def _settle(self, ps: list[Payout], lines: list[BankLine], method: str, why: str,
                named_ids: bool = False) -> None:
        cfg = self.cfg
        expected = sum(p.expected_amount for p in ps)
        received = sum(b.amount for b in lines)
        date = max(b.value_date for b in lines)
        worst_late = max((date - p.expected_date).days for p in ps)
        timing = factor("Settlement timing", 0.15 if named_ids else 0.30,
                        date_score(max(0, worst_late - cfg.grace_days), cfg.payout_window_days),
                        "on time" if worst_late <= cfg.grace_days else f"{worst_late} days after expected")
        if named_ids:
            factors = [factor("Payout id in narrative", 0.60, 1.0, "named explicitly"),
                       factor("Amount", 0.25, amount_score(expected, received), _amount_detail(expected, received)),
                       timing]
        else:   # inferred from amounts alone: never fully certain
            factors = [factor("Payout id in narrative", 0.15, 0.0, "absent: matched on amount"),
                       factor("Amount", 0.55, amount_score(expected, received), _amount_detail(expected, received)),
                       timing]
        self.matches.append(Match("gateway↔bank", [p.id for p in ps], [b.id for b in lines], method,
                                  confidence(factors), why, factors))
        bank_ids = ", ".join(b.id for b in lines)
        for p in ps:
            p.bank_id, p.bank_date, p.bank_account, p.status = bank_ids, date, lines[0].account, "settled"
            p.bank_amount = received if len(ps) == 1 else p.expected_amount
            p.delay_days = (date - p.expected_date).days
        key = "+".join(p.id for p in ps)
        refs = {"payout": [p.id for p in ps], "bank": [b.id for b in lines],
                "gateway": [i for p in ps for i in p.event_ids]}
        delta = received - expected
        ccy = lines[0].currency
        due = max(p.expected_date for p in ps) + dt.timedelta(days=cfg.grace_days)
        if delta < 0 and len(ps) == 1 and self.as_of <= due:
            p = ps[0]
            p.status = "partially_settled"
            self.flag("PARTIAL_SETTLEMENT", p.id,
                      f"{p.gateway} payout {p.id}: {money(received, ccy)} of {money(expected, ccy)} received",
                      f"{len(lines)} deposit{'s' if len(lines) > 1 else ''} into {lines[0].account} "
                      f"({bank_ids}) cover {received / expected:.0%} of payout {p.id}. "
                      f"{money(-delta, ccy)} is still outstanding and is due by {due}. If the "
                      f"remainder does not arrive by then this escalates to a payout mismatch.",
                      refs, delta, ccy)
            return
        if delta != 0:
            evs = [e for e in self.gw if e.id in refs["gateway"]]
            hints = []
            net_hits = [e for e in evs if e.net == -delta]
            fee_hits = [e for e in evs if e.fee == -delta]
            dup_hits = [e for e in evs if e.net == delta and e.id in self.gw_dupes]
            if len(net_hits) == 1:
                e = net_hits[0]
                hints.append(f"The shortfall equals the net of {e.id} ({e.ref}, {money(e.net)}), "
                             f"so the bank settlement probably left that transaction out.")
            elif net_hits:
                hints.append(f"The shortfall equals the net of each of "
                             f"{', '.join(e.id for e in net_hits)} ({money(-delta)} each), so one "
                             f"of them was probably left out. An itemised payout report will show "
                             f"which.")
            if fee_hits:
                hints.append(f"The shortfall also equals the fee on {', '.join(e.id for e in fee_hits)}, "
                             f"so a fee may have been deducted twice.")
            for e in dup_hits:
                hints.append(f"The excess equals {e.id}, a duplicated webhook, so the gateway "
                             f"probably paid it out twice.")
            if not hints:
                hints.append("No single transaction or fee explains the gap. Request the itemised "
                             "payout breakdown from the gateway.")
            self.flag("PAYOUT_AMOUNT_MISMATCH", key,
                      f"Payout {key}: expected {money(expected, ccy)}, bank received {money(received, ccy)}",
                      f"Summing the net of the {len(evs)} gateway transactions in {key} gives "
                      f"{money(expected, ccy)}, but {bank_ids} in {lines[0].account} received "
                      f"{money(received, ccy)}, a difference of {money(delta, ccy)}. " + " ".join(hints),
                      refs, delta, ccy)
        for p in ps:
            late = (date - p.expected_date).days
            if late > cfg.grace_days:
                self.flag("DELAYED_SETTLEMENT", p.id,
                          f"{p.gateway} payout {p.id} settled {late} days late",
                          f"Payout {p.id} was due on {p.expected_date} (latest transaction date + "
                          f"T+{cfg.settlement_days}) but reached {lines[0].account} on {date}, {late} days "
                          f"late against a {cfg.grace_days}-day grace period. The funds did arrive, so "
                          f"only cash-flow timing was affected.",
                          {"payout": [p.id], "bank": [b.id for b in lines]}, 0, ccy)

    # ------------------------------------------------------------ outputs
    def _traces(self) -> list[dict]:
        """One 'follow the money' chain per order reference."""
        by_record: dict[str, list[str]] = defaultdict(list)
        for d in self.found.values():
            for ids in d.refs.values():
                for i in ids:
                    by_record[i].append(d.fingerprint)
        led_by_id = {l.id: l for l in self.ledger_raw}
        groups: dict[str, dict] = {}

        def group(key: str, label: str) -> dict:
            return groups.setdefault(key, {"ref": label, "ledger": [], "gateway": [], "payouts": {}})

        for l in self.ledger_raw:
            group(norm_ref(l.ref) or l.id, l.ref)["ledger"].append(
                {"id": l.id, "kind": l.kind, "amount": l.amount, "currency": l.currency,
                 "ts": l.ts.isoformat(), "customer": l.customer})
        for e in self.gw:
            anchor = led_by_id.get(self.gw_link.get(e.id, ""))
            key = norm_ref(anchor.ref) if anchor else (norm_ref(e.ref) or e.id)
            g = group(key, anchor.ref if anchor else (e.ref or e.id))
            g["gateway"].append({"id": e.id, "ref": e.ref, "kind": e.kind, "gross": e.gross,
                                 "fee": e.fee, "net": e.net, "currency": e.currency,
                                 "ts": e.ts.isoformat(), "payout_id": e.payout_id, "gateway": e.gateway,
                                 "copies": 1 + self.gw_dupes.get(e.id, 0)})
            p = self.payout_of_event.get(e.id)
            if p:
                g["payouts"][p.id] = {"id": p.id, "gateway": p.gateway, "status": p.status,
                                      "expected_date": p.expected_date.isoformat(),
                                      "bank_id": p.bank_id, "bank_account": p.bank_account,
                                      "bank_date": p.bank_date.isoformat() if p.bank_date else None}
        out = []
        for key, g in groups.items():
            ids = [x["id"] for x in g["ledger"] + g["gateway"]]
            ids += [p["id"] for p in g["payouts"].values()]
            ids += [i.strip() for p in g["payouts"].values() if p["bank_id"] for i in p["bank_id"].split(",")]
            fps = sorted({fp for i in ids for fp in by_record.get(i, [])})
            statuses = {p["status"] for p in g["payouts"].values()}
            if fps:
                status = "exception"
            elif not g["gateway"]:
                status = "unmatched"
            elif not statuses or statuses <= {"in_transit", "partially_settled"}:
                status = "in_transit"
            else:
                status = "settled"
            st = self.order_state.get(key, {})
            out.append({**g, "payouts": list(g["payouts"].values()), "discrepancies": fps,
                        "status": status, "lifecycle": st.get("status", ""),
                        "disputed": bool(st.get("charged_back"))})
        out.sort(key=lambda g: g["ref"])
        return out

    def _reports(self, res: Result) -> list[dict]:
        """One report per source pair: how many records reconciled cleanly and how much
        money sits in discrepancies, with a breakdown per gateway and bank account."""
        involved: dict[str, set[str]] = defaultdict(set)
        mismatched: dict[str, int] = defaultdict(int)
        open_n: dict[str, int] = defaultdict(int)
        for d in res.discrepancies:
            open_n[d.pair] += 1
            for ids in d.refs.values():
                involved[d.pair].update(ids)
            if d.kind not in NO_MONEY_KINDS:
                mismatched[d.pair] += abs(d.amount_delta)
        led_records = {l.id: l for l in self.ledger_raw}
        gw_by_id = {e.id: e for e in self.gw}
        lg_matches = [m for m in res.matches if m.layer == "ledger↔gateway"]
        gw_of_ledger = {}
        for m in lg_matches:
            for lid in m.left:
                gw_of_ledger[lid] = gw_by_id[m.right[0]].gateway
        lg_ids = set(led_records) | set(gw_by_id)
        lg_clean = lg_ids - involved["ledger_gateway"]
        lg_amount = sum(led_records[lid].amount for m in lg_matches for lid in m.left
                        if lid in lg_clean and all(r in lg_clean for r in m.right))

        def money_for(pair: str, ids: set[str]) -> int:
            return sum(abs(d.amount_delta) for d in res.discrepancies
                       if d.pair == pair and d.kind not in NO_MONEY_KINDS
                       and any(i in ids for v in d.refs.values() for i in v))

        lg_break = []
        sources = defaultdict(set)
        for e in self.gw:
            sources[e.gateway].add(e.id)
        for lid in led_records:
            sources[gw_of_ledger.get(lid, "Unassigned ledger entries")].add(lid)
        for name, ids in sorted(sources.items()):
            clean = ids - involved["ledger_gateway"]
            lg_break.append({"source": name, "type": "gateway", "records": len(ids), "clean": len(clean),
                             "matched_pct": round(len(clean) / max(1, len(ids)), 4),
                             "mismatched_amount": money_for("ledger_gateway", ids)})

        credits = [b for b in self.bank if b.amount > 0]
        gb_ids = {p.id for p in res.payouts if p.status != "in_transit"} | {b.id for b in credits} \
            | {e.id for e in self.pending_events}
        gb_clean = gb_ids - involved["gateway_bank"]
        gb_amount = sum(p.bank_amount or 0 for p in res.payouts
                        if p.status == "settled" and p.id in gb_clean
                        and all(i.strip() in gb_clean for i in p.bank_id.split(",")))
        gb_break = []
        by_gw, by_acct = defaultdict(set), defaultdict(set)
        for p in res.payouts:
            if p.status != "in_transit":
                by_gw[p.gateway].add(p.id)
        for e in self.pending_events:
            by_gw[e.gateway].add(e.id)
        for b in credits:
            by_acct[b.account].add(b.id)
        for kind, table in (("gateway", by_gw), ("bank", by_acct)):
            for name, ids in sorted(table.items()):
                clean = ids - involved["gateway_bank"]
                gb_break.append({"source": name, "type": kind, "records": len(ids), "clean": len(clean),
                                 "matched_pct": round(len(clean) / max(1, len(ids)), 4),
                                 "mismatched_amount": money_for("gateway_bank", ids)})

        first = (self.ledger_raw or self.gw or [None])[0]
        ccy = getattr(first, "currency", "USD")
        out = []
        for pair, ids, clean, amount, layer, brk in (
                ("ledger_gateway", lg_ids, lg_clean, lg_amount, "ledger↔gateway", lg_break),
                ("gateway_bank", gb_ids, gb_clean, gb_amount, "gateway↔bank", gb_break)):
            ms = [m for m in res.matches if m.layer == layer]
            out.append({
                "pair": pair, "source_a": PAIRS[pair][0], "source_b": PAIRS[pair][1],
                "records": len(ids), "clean": len(clean),
                "matched_pct": round(len(clean) / max(1, len(ids)), 4),
                "matches": len(ms), "fuzzy_matches": sum(m.confidence < 1 for m in ms),
                "reconciled_amount": amount, "mismatched_amount": mismatched[pair],
                "discrepancies": open_n[pair], "currency": ccy, "breakdown": brk})
        return out

    def _settlement_stats(self, res: Result) -> dict:
        """Delay and aging analysis per gateway and per bank account."""
        out = {"by_gateway": [], "by_account": []}
        by_gw = defaultdict(list)
        for p in res.payouts:
            by_gw[p.gateway].append(p)
        for gw, ps in sorted(by_gw.items()):
            settled = [p for p in ps if p.status == "settled"]
            delays = [p.delay_days for p in settled if p.delay_days is not None]
            out["by_gateway"].append({
                "gateway": gw, "payouts": len(ps), "settled": len(settled),
                "on_time": sum(d <= self.cfg.grace_days for d in delays),
                "avg_delay": round(sum(delays) / len(delays), 1) if delays else None,
                "max_delay": max(delays) if delays else None,
                "partial": sum(p.status == "partially_settled" for p in ps),
                "missing": sum(p.status == "missing" for p in ps),
                "in_transit": sum(p.status == "in_transit" for p in ps),
                "outstanding": sum(p.expected_amount - (p.bank_amount or 0) for p in ps
                                   if p.status in ("missing", "in_transit", "partially_settled"))})
        by_acct = defaultdict(list)
        for b in self.bank:
            by_acct[b.account].append(b)
        for acct, bs in sorted(by_acct.items()):
            out["by_account"].append({"account": acct, "lines": len(bs),
                                      "credits": sum(b.amount for b in bs if b.amount > 0),
                                      "debits": sum(-b.amount for b in bs if b.amount < 0)})
        return out

    def _stats(self, res: Result) -> dict:
        lg = [m for m in res.matches if m.layer == "ledger↔gateway"]
        by_kind: dict[str, int] = defaultdict(int)
        by_sev: dict[str, int] = defaultdict(int)
        for d in res.discrepancies:
            by_kind[d.kind] += 1
            by_sev[d.severity] += 1
        pstat: dict[str, int] = defaultdict(int)
        for p in res.payouts:
            pstat[p.status] += 1
        by_method: dict[str, int] = defaultdict(int)
        for m in res.matches:
            by_method[m.method] += 1
        matched_ledger = {i for m in lg for i in m.left}
        exactish = ("reference", "manual", "payout id", "composite")
        return {
            "records": {"ledger": len(self.ledger_raw), "gateway": len(self.gateway_raw),
                        "gateway_unique": len(self.gw), "bank": len(self.bank)},
            "ledger_match_rate": round(len(matched_ledger) / max(1, len(self.ledger)), 4),
            "matches": {"exact": sum(m.method.split(" ·")[0] in exactish or m.method.startswith("exact")
                                     for m in res.matches),
                        "fuzzy": sum(not (m.method.split(" ·")[0] in exactish or m.method.startswith("exact"))
                                     for m in res.matches),
                        "by_method": dict(by_method),
                        "avg_confidence": round(sum(m.confidence for m in res.matches) / max(1, len(res.matches)), 3)},
            "payouts": dict(pstat),
            "discrepancies": len(res.discrepancies),
            "by_kind": dict(by_kind),
            "by_severity": dict(by_sev),
            "exposure": sum(abs(d.amount_delta) for d in res.discrepancies if d.kind not in NO_MONEY_KINDS),
            "clean_traces": sum(t["status"] != "exception" for t in res.traces),
            "total_traces": len(res.traces),
            "disputes": {s: sum(d["status"] == s for d in res.disputes) for s in ("open", "won", "expired")},
            "settlement": self._settlement_stats(res),
        }


def reconcile(ledger, gateway, bank, as_of: dt.date, config: Config | None = None,
              manual_links: list[tuple[str, str]] | None = None) -> Result:
    return Reconciler(ledger, gateway, bank, as_of, config, manual_links).run()
