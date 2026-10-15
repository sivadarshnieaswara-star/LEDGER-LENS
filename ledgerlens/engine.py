"""The reconciliation engine.

Two matching layers, each exact-first then fuzzy:

  ledger ↔ gateway   by normalised merchant reference, then amount + date window
  gateway ↔ bank     payouts (gateway events grouped by payout id, summed net)
                     against bank deposits, by payout id in the narrative,
                     then amount + date window

Every rule that fails produces a Discrepancy with a plain-English explanation
and a fingerprint that is stable across runs, so the store can carry
resolution history forward.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from .models import BankLine, Discrepancy, GatewayEvent, LedgerEntry, Match

# kind → (severity, suggested action)
KINDS: dict[str, tuple[str, str]] = {
    "DUPLICATE_CHARGE": ("high", "Customer was charged twice. Refund the extra charge and confirm with the customer."),
    "DUPLICATE_REFUND": ("high", "Refund was issued twice. Contact the gateway or recover the funds."),
    "UNRECORDED_REFUND": ("high", "Refund happened at the gateway but not in the books. Post the refund to the ledger."),
    "MISSING_SETTLEMENT": ("high", "Payout is overdue and no bank deposit was found. Raise it with the gateway."),
    "PAYOUT_AMOUNT_MISMATCH": ("high", "The bank deposit differs from what the gateway says it paid out. Investigate the gap."),
    "AMOUNT_MISMATCH": ("medium", "Ledger and gateway disagree on the amount. Correct the side that is wrong."),
    "MISSING_IN_GATEWAY": ("medium", "Ledger expects money the gateway never captured. Check whether the payment failed."),
    "MISSING_IN_LEDGER": ("medium", "The gateway captured money the ledger has no record of. Post the entry or investigate."),
    "DUPLICATE_LEDGER_ENTRY": ("medium", "The same entry was posted twice. Reverse the duplicate journal."),
    "FEE_VARIANCE": ("medium", "Gateway fee differs from the contracted rate. Dispute it or update the rate card."),
    "NET_INCONSISTENT": ("medium", "Gateway net ≠ gross − fee. Ask the gateway for a corrected report."),
    "NOT_PAID_OUT": ("medium", "Captured funds were never assigned to a payout. Check for holds or a reserve."),
    "UNIDENTIFIED_DEPOSIT": ("medium", "Bank deposit with no matching payout. Identify the sender."),
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
    "NOT_PAID_OUT")}
PAIR_OF_KIND.update({k: "ledger_gateway" for k in KINDS if k not in PAIR_OF_KIND})
NO_MONEY_KINDS = ("DUPLICATE_WEBHOOK", "DELAYED_SETTLEMENT", "REFERENCE_MISMATCH")


@dataclass
class Config:
    settlement_days: int = 2          # expected T+N for payouts
    grace_days: int = 1               # tolerance before a payout counts as late
    fuzzy_window_days: int = 3        # ledger↔gateway fuzzy date window
    payout_window_days: int = 7       # gateway↔bank fuzzy date window
    fee_rate_bp: int = 290            # contracted fee: 2.90 % …
    fee_fixed: int = 30               # … + 30¢
    fee_tolerance: int = 1            # cents
    auto_match_score: float = 0.80    # probabilistic: accept automatically at/above this…
    auto_match_margin: float = 0.10   # …when the runner-up trails by at least this much
    suggest_score: float = 0.45       # below auto but above this → candidate for human review


@dataclass
class Payout:
    id: str
    currency: str
    expected_amount: int
    expected_date: dt.date
    event_ids: list[str]
    status: str = "unmatched"         # settled | in_transit | missing | unmatched
    bank_id: str = ""
    bank_amount: int | None = None
    bank_date: dt.date | None = None


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


def score_candidate(l: LedgerEntry, e: GatewayEvent, cfg: Config) -> tuple[float, list[str]]:
    """Likelihood (0–1) that a ledger entry and a gateway event are the same payment,
    with human-readable reasons. Amount dominates; date and reference text break ties."""
    if e.currency != l.currency:
        return 0.0, [f"currency {l.currency} ≠ {e.currency}"]
    diff = abs(e.gross - l.amount)
    amt = 1.0 if diff == 0 else max(0.0, 1 - diff / max(l.amount, 1) * 5)   # 20 % off → 0
    days = abs((e.ts - l.ts).total_seconds()) / 86400
    date = max(0.0, 1 - days / (cfg.fuzzy_window_days + 1))
    ref = SequenceMatcher(None, norm_ref(l.ref), norm_ref(e.ref)).ratio()
    score = 0.55 * amt + 0.30 * date + 0.15 * ref
    reasons = ["amount identical" if diff == 0 else f"amount differs by {money(diff)}",
               f"{days:.1f} days apart" if days >= 0.1 else "same time",
               f"reference similarity {ref:.0%}"]
    return score, reasons


PAYOUT_RE = re.compile(r"\b(po_[a-z0-9]+)\b", re.I)


class Reconciler:
    def __init__(self, ledger: list[LedgerEntry], gateway: list[GatewayEvent],
                 bank: list[BankLine], as_of: dt.date, config: Config | None = None,
                 manual_links: list[tuple[str, str]] | None = None):
        self.manual_links = manual_links or []
        self.pipeline: dict = {
            "ingestion": {}, "deterministic": {"ledger_gateway": 0, "gateway_bank": 0, "manual": 0},
            "probabilistic": {"evaluated": 0, "auto_matched": 0, "suggested": 0, "no_candidate": 0,
                              "payouts_matched": 0},
            "classifier": {}}
        self.ledger_raw = ledger
        self.gateway_raw = gateway
        self.bank = bank
        self.as_of = as_of
        self.cfg = config or Config()
        self.matches: list[Match] = []
        self.found: dict[str, Discrepancy] = {}
        self.gw_link: dict[str, str] = {}       # gateway id → ledger id
        self.gw_dupes: dict[str, int] = {}      # gateway id → extra copies seen
        self.payouts: dict[str, Payout] = {}
        self.pending_events: list[GatewayEvent] = []

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
        self._match_payouts()
        discrepancies = sorted(self.found.values(),
                               key=lambda d: (SEVERITY_ORDER[d.severity], d.kind, d.fingerprint))
        traces = self._traces()
        res = Result(self.as_of, self.matches, discrepancies,
                     list(self.payouts.values()), traces)
        res.stats = self._stats(res)
        self.pipeline["ingestion"] = {
            "ledger": len(self.ledger_raw), "gateway": len(self.gateway_raw), "bank": len(self.bank),
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
                      f"{tid} appears {extra + 1}× in the gateway report",
                      f"Transaction {tid} ({e.ref}, {money(e.gross, e.currency)}) was delivered "
                      f"{extra + 1} times, probably because a webhook was retried. Only one copy was "
                      f"counted toward the payout. Adding up the raw rows would overstate gateway "
                      f"volume by {money(e.net * extra, e.currency)}.",
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
                          f"{ls[0].ref} {kind} posted {len(ls)}× in the ledger",
                          f"Ledger entries {', '.join(ids)} all record a {money(amount, ccy)} {kind} "
                          f"for {ls[0].ref}. The gateway shows one, so revenue is overstated by "
                          f"{money(amount * (len(ls) - 1), ccy)}.",
                          {"ledger": ids}, amount * (len(ls) - 1), ccy)

    # ------------------------------------------------- ledger ↔ gateway
    def _link(self, l: LedgerEntry, e: GatewayEvent, method: str, conf: float, why: str) -> None:
        self.gw_link[e.id] = l.id
        self.matches.append(Match("ledger↔gateway", [l.id], [e.id], method, conf, why))
        if e.currency != l.currency or e.gross != l.amount:
            delta = e.gross - l.amount
            cur_note = (f" They also use different currencies ({l.currency} vs {e.currency}), which "
                        f"points to an FX conversion problem." if e.currency != l.currency else "")
            direction = "more" if delta > 0 else "less"
            self.flag("AMOUNT_MISMATCH", f"{l.id}:{e.id}",
                      f"{l.ref}: ledger {money(l.amount, l.currency)} vs gateway {money(e.gross, e.currency)}",
                      f"The ledger recorded {money(l.amount, l.currency)} for {l.ref}, but the gateway "
                      f"captured {money(e.gross, e.currency)} in {e.id}, {money(abs(delta))} {direction}. "
                      f"Possible causes are a partial capture, a discount applied after checkout, or a "
                      f"typo when the entry was posted.{cur_note}",
                      {"ledger": [l.id], "gateway": [e.id]}, delta, e.currency)

    def _match_ledger_gateway(self) -> None:
        # stage 0: links a human reviewer confirmed in the exception queue
        led_ids = {l.id: l for l in self.ledger}
        gw_ids = {e.id: e for e in self.gw}
        manual_l, manual_g = set(), set()
        for lid, gid in self.manual_links:
            if lid in led_ids and gid in gw_ids and lid not in manual_l and gid not in manual_g:
                manual_l.add(lid)
                manual_g.add(gid)
                self._link(led_ids[lid], gw_ids[gid], "manual", 1.0,
                           "Confirmed by a reviewer in the exception queue")
                self.pipeline["deterministic"]["manual"] += 1

        # stage 1: deterministic, by normalised reference
        gw_by_key: dict[tuple, list[GatewayEvent]] = defaultdict(list)
        for e in self.gw:
            if e.id not in manual_g:
                gw_by_key[(norm_ref(e.ref), e.kind)].append(e)
        led_by_key: dict[tuple, list[LedgerEntry]] = defaultdict(list)
        for l in self.ledger:
            if l.id not in manual_l:
                led_by_key[(norm_ref(l.ref), l.kind)].append(l)

        unmatched: list[LedgerEntry] = []
        for key, ls in led_by_key.items():
            pool = list(gw_by_key.get(key, []))
            for l in sorted(ls, key=lambda x: x.ts):
                if not pool:
                    unmatched.append(l)
                    continue
                pool.sort(key=lambda e: (e.gross != l.amount, abs((e.ts - l.ts).total_seconds())))
                e = pool.pop(0)
                same_text = l.ref == e.ref
                self._link(l, e, "reference", 1.0,
                           f"Reference {l.ref!r} ↔ {e.ref!r}"
                           + ("" if same_text else " (same reference after normalising format)"))
                self.pipeline["deterministic"]["ledger_gateway"] += 1
            for extra in pool:   # more gateway events than ledger entries for this reference
                anchor = ls[0]
                self.gw_link[extra.id] = anchor.id
                kind = "DUPLICATE_CHARGE" if extra.kind == "charge" else "DUPLICATE_REFUND"
                first = next(m.right[0] for m in self.matches if m.left == [anchor.id])
                gap = abs((extra.ts - anchor.ts).total_seconds()) / 60
                self.flag(kind, f"{anchor.ref}:{extra.id}",
                          f"{anchor.ref} {extra.kind}d twice at the gateway ({first}, {extra.id})",
                          f"The ledger has one {anchor.kind} of {money(anchor.amount, anchor.currency)} for "
                          f"{anchor.ref}, but the gateway has a second distinct {extra.kind} {extra.id} "
                          f"of {money(extra.gross, extra.currency)}, {gap:.0f} min apart. The transaction "
                          f"ids are different, so this is not a webhook replay. Money really moved "
                          f"twice, probably from a double-submitted checkout or a client retry.",
                          {"ledger": [anchor.id], "gateway": [first, extra.id]},
                          extra.net, extra.currency)

        # stage 2: probabilistic candidate matching over whatever is left
        orphans = [e for k, es in gw_by_key.items() if k not in led_by_key for e in es]
        cfg = self.cfg
        prob = self.pipeline["probabilistic"]
        prob["evaluated"] = len(unmatched) + len(orphans)
        scored = {l.id: sorted(((score_candidate(l, e, cfg), e) for e in orphans if e.kind == l.kind),
                               key=lambda x: -x[0][0]) for l in unmatched}
        claimed: set[str] = set()
        suggestions: dict[str, list[dict]] = defaultdict(list)   # gateway id → ledger candidates
        # most confident ledger entries pick first, so a strong match is never stolen by a weak one
        for l in sorted(unmatched, key=lambda l: -(scored[l.id][0][0][0] if scored[l.id] else 0)):
            ranked = [(s, e) for s, e in scored[l.id] if e.id not in claimed]
            top = ranked[0] if ranked else None
            runner = ranked[1][0][0] if len(ranked) > 1 else 0.0
            if top and top[0][0] >= cfg.auto_match_score and top[0][0] - runner >= cfg.auto_match_margin:
                (score, reasons), e = top
                claimed.add(e.id)
                prob["auto_matched"] += 1
                self._link(l, e, "probabilistic", round(score, 2),
                           f"Scored {score:.0%} ({'; '.join(reasons)}), next best {runner:.0%}")
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
            for (s, r), e in ranked[:3]:
                if s < cfg.suggest_score:
                    continue
                cands.append({"id": e.id, "score": round(s, 2), "reasons": r, "side": "gateway",
                              "label": f"{e.ref} · {money(e.gross, e.currency)} · {e.ts:%Y-%m-%d}"})
                suggestions[e.id].append({"id": l.id, "score": round(s, 2), "reasons": r, "side": "ledger",
                                          "label": f"{l.ref} · {money(l.amount, l.currency)} · {l.ts:%Y-%m-%d}"})
            prob["suggested" if cands else "no_candidate"] += 1
            why = (f"the closest gateway record, {cands[0]['id']}, scored only {cands[0]['score']:.0%}, "
                   f"which is below the {cfg.auto_match_score:.0%} needed to match automatically. "
                   f"Review the suggested candidates" if cands else
                   "no gateway record has that reference, or a similar amount and date")
            if l.kind == "charge":
                expl = (f"The ledger booked a {money(l.amount, l.currency)} charge for {l.ref} on "
                        f"{l.ts:%Y-%m-%d}, but {why}. If nothing matches, the payment may have failed "
                        f"or been abandoned after the order was posted.")
            else:
                expl = (f"The ledger records a {money(l.amount, l.currency)} refund for {l.ref}, "
                        f"but {why}. The customer may not have received their money.")
            self.flag("MISSING_IN_GATEWAY", l.id, f"{l.ref} {l.kind} not found at gateway", expl,
                      {"ledger": [l.id]}, -l.amount, l.currency, cands)

        for e in orphans:
            if e.id in claimed:
                continue
            cands = sorted(suggestions.get(e.id, []), key=lambda c: -c["score"])
            if not cands:
                prob["no_candidate"] += 1
            hint = (f" A possible ledger counterpart, {cands[0]['id']}, scored {cands[0]['score']:.0%}. "
                    f"Confirm it if it is the same payment." if cands else "")
            if e.kind == "refund":
                self.flag("UNRECORDED_REFUND", e.id, f"Refund {e.id} ({e.ref}) missing from ledger",
                          f"The gateway refunded {money(e.gross, e.currency)} against {e.ref} on "
                          f"{e.ts:%Y-%m-%d} and took it out of payout {e.payout_id or '(none)'}. The "
                          f"ledger has no matching refund, so recorded revenue is overstated by "
                          f"that amount.{hint}",
                          {"gateway": [e.id]}, -e.gross, e.currency, cands)
            else:
                self.flag("MISSING_IN_LEDGER", e.id, f"Gateway charge {e.id} ({e.ref}) not in ledger",
                          f"The gateway captured {money(e.gross, e.currency)} for reference {e.ref!r} "
                          f"on {e.ts:%Y-%m-%d}. No ledger entry matches its reference or scores high "
                          f"enough on amount and date. Possible causes are an order created outside "
                          f"the main system, a test transaction in live mode, or a failed ledger "
                          f"write.{hint}",
                          {"gateway": [e.id]}, e.gross, e.currency, cands)

    # ------------------------------------------------------------- fees
    def expected_fee(self, gross: int) -> int:
        return (gross * self.cfg.fee_rate_bp + 5000) // 10000 + self.cfg.fee_fixed

    def _check_fees(self) -> None:
        for e in self.gw:
            if e.kind != "charge":
                continue
            exp = self.expected_fee(e.gross)
            if abs(e.fee - exp) > self.cfg.fee_tolerance:
                rate = e.fee / e.gross * 100 if e.gross else 0
                self.flag("FEE_VARIANCE", e.id,
                          f"{e.id} fee {money(e.fee, e.currency)}, expected {money(exp, e.currency)}",
                          f"The contracted pricing is {self.cfg.fee_rate_bp / 100:.2f}% + "
                          f"{money(self.cfg.fee_fixed)}, so a {money(e.gross, e.currency)} charge should "
                          f"cost {money(exp, e.currency)}. The gateway took {money(e.fee, e.currency)} "
                          f"(an effective {rate:.2f}%), which is {money(e.fee - exp, e.currency)} "
                          f"{'over' if e.fee > exp else 'under'}. Possible causes are an international "
                          f"or premium-card surcharge, or a rate change nobody recorded.",
                          {"gateway": [e.id]}, -(e.fee - exp), e.currency)
            if e.net != e.gross - e.fee:
                self.flag("NET_INCONSISTENT", e.id, f"{e.id} net ≠ gross − fee",
                          f"{money(e.gross)} − {money(e.fee)} should be {money(e.gross - e.fee)}, but the "
                          f"report says the net is {money(e.net)}.",
                          {"gateway": [e.id]}, e.net - (e.gross - e.fee), e.currency)

    # ------------------------------------------------------ gateway ↔ bank
    def _match_payouts(self) -> None:
        cfg = self.cfg
        groups: dict[str, list[GatewayEvent]] = defaultdict(list)
        for e in self.gw:
            if e.payout_id:
                groups[e.payout_id.lower()].append(e)
            else:
                self.pending_events.append(e)
        for pid, evs in groups.items():
            self.payouts[pid] = Payout(
                id=evs[0].payout_id, currency=evs[0].currency,
                expected_amount=sum(e.net for e in evs),
                expected_date=max(e.ts.date() for e in evs) + dt.timedelta(days=cfg.settlement_days),
                event_ids=[e.id for e in evs])

        bank_left = {b.id: b for b in self.bank}
        # pass 1: payout id in bank narrative
        for b in self.bank:
            m = PAYOUT_RE.search(b.description)
            if m and m.group(1).lower() in self.payouts and not self.payouts[m.group(1).lower()].bank_id:
                p = self.payouts[m.group(1).lower()]
                self._settle(p, b, "payout id in narrative", 1.0,
                             f"Bank narrative {b.description!r} names {p.id}")
                bank_left.pop(b.id)
                self.pipeline["deterministic"]["gateway_bank"] += 1
        # pass 2: amount + date window
        for p in self.payouts.values():
            if p.bank_id:
                continue
            cands = [b for b in bank_left.values() if b.amount == p.expected_amount
                     and b.currency == p.currency
                     and -1 <= (b.value_date - p.expected_date).days <= cfg.payout_window_days]
            if len(cands) == 1:
                b = cands[0]
                off = (b.value_date - p.expected_date).days
                self.pipeline["probabilistic"]["payouts_matched"] += 1
                self._settle(p, b, "probabilistic", round(0.9 - 0.05 * abs(off), 2),
                             f"Exact amount {money(b.amount, b.currency)}, {off:+d} days from the "
                             f"expected date. The narrative {b.description!r} has no payout id.")
                bank_left.pop(b.id)

        for p in self.payouts.values():
            if p.bank_id:
                continue
            due = p.expected_date + dt.timedelta(days=cfg.grace_days)
            refs = {"payout": [p.id], "gateway": p.event_ids}
            if self.as_of > due:
                p.status = "missing"
                self.flag("MISSING_SETTLEMENT", p.id,
                          f"Payout {p.id} ({money(p.expected_amount, p.currency)}) never reached the bank",
                          f"Payout {p.id} covers {len(p.event_ids)} gateway transactions with a net of "
                          f"{money(p.expected_amount, p.currency)}. It was due on {p.expected_date} "
                          f"(T+{cfg.settlement_days}) and is now {(self.as_of - p.expected_date).days} "
                          f"days overdue. No bank deposit matches it by payout id or by amount within "
                          f"{cfg.payout_window_days} days. Possible causes are a gateway hold, a rejected "
                          f"transfer, or changed bank details.",
                          refs, -p.expected_amount, p.currency)
            else:
                p.status = "in_transit"

        for b in bank_left.values():
            if b.amount <= 0:
                continue
            self.flag("UNIDENTIFIED_DEPOSIT", b.id,
                      f"Bank deposit {b.id} ({money(b.amount, b.currency)}) has no source",
                      f"{money(b.amount, b.currency)} arrived on {b.value_date} with narrative "
                      f"{b.description!r}. No gateway payout matches it by id or by amount. It could be "
                      f"a direct customer transfer, a payment from another processor, or a gateway "
                      f"adjustment that is not in the report.",
                      {"bank": [b.id]}, b.amount, b.currency)

        for e in self.pending_events:
            due = e.ts.date() + dt.timedelta(days=cfg.settlement_days + cfg.grace_days)
            if self.as_of > due:
                self.flag("NOT_PAID_OUT", e.id, f"{e.id} captured but never assigned to a payout",
                          f"{e.id} ({e.ref}, net {money(e.net, e.currency)}) was captured on "
                          f"{e.ts:%Y-%m-%d} and is still not in any payout. Possible causes are a "
                          f"risk hold, a rolling reserve, or a pending dispute.",
                          {"gateway": [e.id]}, -e.net, e.currency)

    def _settle(self, p: Payout, b: BankLine, method: str, conf: float, why: str) -> None:
        cfg = self.cfg
        p.bank_id, p.bank_amount, p.bank_date, p.status = b.id, b.amount, b.value_date, "settled"
        self.matches.append(Match("gateway↔bank", [p.id], [b.id], method, conf, why))
        refs = {"payout": [p.id], "bank": [b.id]}
        delta = b.amount - p.expected_amount
        if delta != 0:
            evs = [e for e in self.gw if e.id in p.event_ids]
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
            self.flag("PAYOUT_AMOUNT_MISMATCH", p.id,
                      f"Payout {p.id}: expected {money(p.expected_amount, p.currency)}, bank "
                      f"received {money(b.amount, b.currency)}",
                      f"Summing the net of the {len(evs)} gateway transactions in {p.id} gives "
                      f"{money(p.expected_amount, p.currency)}, but bank line {b.id} received "
                      f"{money(b.amount, b.currency)}, a difference of {money(delta, b.currency)}. "
                      + " ".join(hints),
                      {**refs, "gateway": p.event_ids}, delta, b.currency)
        late = (b.value_date - p.expected_date).days
        if late > cfg.grace_days:
            self.flag("DELAYED_SETTLEMENT", p.id,
                      f"Payout {p.id} settled {late} days late",
                      f"Payout {p.id} was due on {p.expected_date} (latest transaction date + "
                      f"T+{cfg.settlement_days}) but reached the bank on {b.value_date}, {late} days "
                      f"late against a {cfg.grace_days}-day grace period. The funds did arrive, so "
                      f"only cash-flow timing was affected.",
                      refs, 0, b.currency)

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
            group(norm_ref(l.ref), l.ref)["ledger"].append(
                {"id": l.id, "kind": l.kind, "amount": l.amount, "currency": l.currency,
                 "ts": l.ts.isoformat()})
        for e in self.gw:
            anchor = led_by_id.get(self.gw_link.get(e.id, ""))
            key = norm_ref(anchor.ref) if anchor else norm_ref(e.ref)
            g = group(key, anchor.ref if anchor else e.ref)
            g["gateway"].append({"id": e.id, "ref": e.ref, "kind": e.kind, "gross": e.gross,
                                 "fee": e.fee, "net": e.net, "currency": e.currency,
                                 "ts": e.ts.isoformat(), "payout_id": e.payout_id,
                                 "copies": 1 + self.gw_dupes.get(e.id, 0)})
            p = self.payouts.get(e.payout_id.lower()) if e.payout_id else None
            if p:
                g["payouts"][p.id] = {"id": p.id, "status": p.status,
                                      "expected_date": p.expected_date.isoformat(),
                                      "bank_id": p.bank_id,
                                      "bank_date": p.bank_date.isoformat() if p.bank_date else None}
        out = []
        for g in groups.values():
            ids = [x["id"] for x in g["ledger"] + g["gateway"]]
            ids += [p["id"] for p in g["payouts"].values()]
            ids += [p["bank_id"] for p in g["payouts"].values() if p["bank_id"]]
            fps = sorted({fp for i in ids for fp in by_record.get(i, [])})
            statuses = {p["status"] for p in g["payouts"].values()}
            if fps:
                status = "exception"
            elif not g["gateway"]:
                status = "unmatched"
            elif not statuses or statuses == {"in_transit"}:
                status = "in_transit"
            else:
                status = "settled"
            out.append({**g, "payouts": list(g["payouts"].values()),
                        "discrepancies": fps, "status": status})
        out.sort(key=lambda g: g["ref"])
        return out

    def _reports(self, res: Result) -> list[dict]:
        """One report per source pair: how many records reconciled cleanly and how much
        money sits in discrepancies."""
        involved: dict[str, set[str]] = defaultdict(set)
        mismatched: dict[str, int] = defaultdict(int)
        open_n: dict[str, int] = defaultdict(int)
        for d in res.discrepancies:
            open_n[d.pair] += 1
            for ids in d.refs.values():
                involved[d.pair].update(ids)
            if d.kind not in NO_MONEY_KINDS:
                mismatched[d.pair] += abs(d.amount_delta)
        matched_ledger = {m.left[0]: m.right[0] for m in res.matches if m.layer == "ledger↔gateway"}
        led_records = {l.id: l for l in self.ledger_raw}
        lg_ids = set(led_records) | {e.id for e in self.gw}
        lg_clean = lg_ids - involved["ledger_gateway"]
        lg_amount = sum(led_records[i].amount for i in matched_ledger
                        if i in lg_clean and matched_ledger[i] in lg_clean)
        credits = [b for b in self.bank if b.amount > 0]
        gb_ids = {p.id for p in res.payouts if p.status != "in_transit"} | {b.id for b in credits} \
            | {e.id for e in self.pending_events}
        gb_clean = gb_ids - involved["gateway_bank"]
        gb_amount = sum(p.bank_amount or 0 for p in res.payouts
                        if p.status == "settled" and p.id in gb_clean and p.bank_id in gb_clean)
        ccy = (self.ledger_raw or self.gw or [None])[0]
        ccy = getattr(ccy, "currency", "USD")
        out = []
        for pair, ids, clean, amount, layer in (
                ("ledger_gateway", lg_ids, lg_clean, lg_amount, "ledger↔gateway"),
                ("gateway_bank", gb_ids, gb_clean, gb_amount, "gateway↔bank")):
            ms = [m for m in res.matches if m.layer == layer]
            out.append({
                "pair": pair, "source_a": PAIRS[pair][0], "source_b": PAIRS[pair][1],
                "records": len(ids), "clean": len(clean),
                "matched_pct": round(len(clean) / max(1, len(ids)), 4),
                "matches": len(ms), "fuzzy_matches": sum(m.confidence < 1 for m in ms),
                "reconciled_amount": amount, "mismatched_amount": mismatched[pair],
                "discrepancies": open_n[pair], "currency": ccy})
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
        matched_ledger = {i for m in lg for i in m.left}
        return {
            "records": {"ledger": len(self.ledger_raw), "gateway": len(self.gateway_raw),
                        "gateway_unique": len(self.gw), "bank": len(self.bank)},
            "ledger_match_rate": round(len(matched_ledger) / max(1, len(self.ledger)), 4),
            "matches": {"exact": sum(m.confidence == 1.0 for m in res.matches),
                        "fuzzy": sum(m.confidence < 1.0 for m in res.matches)},
            "payouts": dict(pstat),
            "discrepancies": len(res.discrepancies),
            "by_kind": dict(by_kind),
            "by_severity": dict(by_sev),
            "exposure": sum(abs(d.amount_delta) for d in res.discrepancies
                            if d.kind not in ("DUPLICATE_WEBHOOK", "DELAYED_SETTLEMENT")),
            "clean_traces": sum(t["status"] != "exception" for t in res.traces),
            "total_traces": len(res.traces),
        }


def reconcile(ledger, gateway, bank, as_of: dt.date, config: Config | None = None,
              manual_links: list[tuple[str, str]] | None = None) -> Result:
    return Reconciler(ledger, gateway, bank, as_of, config, manual_links).run()
