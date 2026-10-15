# LedgerLens: intelligent payment reconciliation

LedgerLens reconciles a merchant's **internal ledger**, **payment-gateway report** and
**bank settlement file**. It finds missing, duplicated, delayed and mismatched payments,
explains each one in plain English, and keeps a tamper-evident resolution history.

It runs on Python 3.10+ and uses only the standard library.

## Quick start

```bash
python3 -m ledgerlens up        # builds demo data on first run, opens the intro page at http://127.0.0.1:8765
```

It needs Python 3.10+ and nothing else: no `pip install`, no database server.

## Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/sivadarshnieaswara-star/LEDGER-LENS)

- **Render (free):** click the button above, or in the Render dashboard choose *New → Blueprint* and pick
  this repository. `render.yaml` sets everything up: there is no build step, and the start command is
  `python -m ledgerlens up --no-browser`.
- **Railway / Heroku-style hosts:** the `Procfile` runs the same command.
- **Fly.io, Cloud Run, a VPS:** use the `Dockerfile` (`docker build -t ledgerlens . && docker run -p 8765:8765 ledgerlens`).

The app reads `$PORT` and binds to `0.0.0.0` when a host provides one. On first boot it generates the
demo data and three daily close-outs. Free hosts usually have ephemeral disks, so the demo resets on
each redeploy or restart, which is fine for a public demo. Set `LEDGERLENS_DB` to a path on a
persistent disk to keep history.

> The public demo has no login: anyone with the link can resolve exceptions or upload files. Don't
> upload real payment data to a public deployment.

## Features

| Capability | How LedgerLens does it |
|---|---|
| **Advanced matching engine** | Five strategies run in order: reviewer links → **exact** (normalised reference) → **composite** (customer + amount + currency + date) → **aggregate** sums → **probabilistic** scoring |
| **One-to-many & many-to-one** | Split captures (2 charges → 1 order), consolidated checkouts (1 charge → 3 orders), split bank deposits (2 lines → 1 payout), batched transfers (1 line → 2 payouts) |
| **Partial settlement & refunds** | Partial refunds tracked per order; partially received payouts are held as `PARTIAL_SETTLEMENT` until due, then escalate; over-refunds detected |
| **Chargebacks & reversals** | Chargebacks, dispute wins (reversals), evidence deadlines, win rate, and unrecorded chargebacks or reversals in the ledger |
| **Exception detection & classification** | 21 types, each with a severity, a plain-English cause, a suggested action and the exact records involved |
| **Explainable confidence** | Every match stores weighted factors (weight × score = contribution) that add up to its confidence; click any match to see them |
| **Manual review & approval** | Investigate, assign, comment, confirm candidates, bulk actions. **Maker-checker**: closing a high-severity or $100+ exception needs a second reviewer |
| **Immutable audit trail** | Append-only (SQLite triggers) and SHA-256 hash-chained; any edit is detected by `verify` and by the dashboard badge |
| **Settlement delay & aging** | Exception aging buckets, on-time rate and average or worst delay per gateway, per-payout delay timeline, outstanding amounts |
| **Multi-gateway & multi-bank** | Any number of `gateway*.csv` and `bank*.csv` files (or the bank API simulator); per-gateway fee contracts; reports broken down per gateway and per account |
| Also | CSV upload and export, trend charts, Follow-the-money traces, light and dark themes, keyboard shortcuts, responsive layout |

The demo data covers two gateways (**NovaPay**, **Paystream**) and two bank accounts
(**Northbank Operating**, **Harbor Bank Settlement**), with one planted case for every
feature above.

## Demo script for judges (3 minutes)

1. **Reconciliation.** Three daily close-outs are already loaded. The Gateway ↔ Bank match
   rate drops as payouts go missing (see the chart). Click a report row to see each
   discrepancy and every match with its method and confidence.
2. **Exception queue.** Open *"ORD-1032 charge not found at gateway"*. Probabilistic matching
   found a 73% candidate (same time, $4.20 short, different reference). Click
   **Confirm match**.
3. **Data sources.** Pick **API simulator** and click **Run reconciliation**. The confirmed
   pair is now matched, and the leftover $4.20 is flagged as an `AMOUNT_MISMATCH`, so
   nothing is lost.
4. **Follow the money.** Search `1005` to see a customer charged twice (two distinct
   transactions), as opposed to `1003`, which is a harmless webhook replay.
5. **Chargebacks.** ORD-3007 was refunded *and* charged back, so it shows an over-refund
   and an unrecorded chargeback. ORD-3008's dispute was won.
6. **Aging & delays.** Paystream's PS-STL-04 is half-paid (partial settlement), and
   NovaPay's po_7Hq2E is missing.
7. **Approvals.** Resolve a high-severity item as one person, then approve it as another.
   Approving your own request is refused.
8. **Audit trail.** Every detection and decision appears with its hash, and the badge
   confirms the chain is intact.
9. **Bring your own data.** Under Data sources, download the templates, edit them and
   upload them.

Keyboard shortcuts: `r` reports · `q` queue · `t` trace · `p` pipeline · `d` data sources ·
`a` audit · `/` search · `Esc` close panel.

## CLI

```bash
python3 -m ledgerlens demo --fresh
python3 -m ledgerlens serve
python3 -m ledgerlens run --ledger ledger.csv --gateway gateway.csv --bank bank.csv --as-of 2026-10-15
python3 -m ledgerlens run --ledger ledger.csv --gateway gateway.csv --bank-api http://127.0.0.1:8765/sim/bank/v1
python3 -m ledgerlens list --status active
python3 -m ledgerlens act MISSING_IN_GATEWAY:JE-5033 link --candidate ch_1032A --actor ana --note "promo"
python3 -m ledgerlens verify
python3 -m unittest -v tests.test_ledgerlens
```

## Architecture

```
 Bank CSV or API Simulator   Gateway Settlement Feed   Internal Payment Ledger
            └──────────────────────────┼──────────────────────────┘
                         Ingestion and Normalization          ingest.py, bank_api.py
                                       │
                           Deterministic Matching ──────────▶ Reconciliation Reports
                                       │                       (one per source pair, per run)
                        Probabilistic Candidate Matching      engine.py
                                       │
                            Discrepancy Classifier
                                       │
                               Exception Queue                store.py
                                       │
                         Human Review and Resolution          dashboard / CLI
                                       │                        (confirmed links feed back
                                  Audit Trail                    into deterministic matching)
```

1. **Ingestion and normalization.** Column aliases map different export layouts onto
   canonical records. Money is stored as integer cents and several date formats are
   accepted. References such as `ORD-01001`, `ord_1001` and `Order #1001` all normalise
   to `1001`. Webhook replays and double-posted journals are removed. The bank side
   comes from a CSV file or from the **bank API simulator**
   (`GET /sim/bank/v1/transactions`), which uses an open-banking shape with cursor
   pagination.
2. **Deterministic matching.** Ledger ↔ gateway on the normalised reference; gateway
   payouts ↔ bank on the payout ID in the bank narrative; plus any pairs a reviewer
   confirmed earlier.
3. **Probabilistic candidate matching.** Leftover records are scored as
   `0.55·amount + 0.30·date + 0.15·reference similarity`. A pair scoring ≥ 80% that
   leads the runner-up by at least 10 points is accepted automatically. A pair scoring
   45–80% becomes a *suggested candidate* attached to the exception for a human to
   confirm.
4. **Discrepancy classifier.** 16 types, each with a severity, a plain-English
   explanation, a suggested action and a stable fingerprint.
5. **Exception queue.** Re-runs update existing exceptions instead of duplicating them:
   fixed ones auto-clear and relapses reopen.
6. **Human review.** Investigate, assign, comment, resolve, accept, reopen, or
   **confirm a candidate match**. A confirmed match closes both one-sided exceptions and
   is used as a match in every later run, where any remaining amount difference then
   shows up as an `AMOUNT_MISMATCH`.
7. **Audit trail.** Every detection and decision is recorded as an append-only,
   SHA-256 hash-chained event.

**Reconciliation reports** are produced per source pair per run
(*Internal ledger ↔ Gateway* and *Gateway ↔ Bank*). Each report gives the share of
records that reconciled cleanly, the reconciled amount and the amount still in
discrepancies, and can be filtered by source and date.

## Discrepancy types

| Severity | Kind | Meaning |
|---|---|---|
| high | `DUPLICATE_CHARGE` / `DUPLICATE_REFUND` | two distinct gateway txns for one ledger entry |
| high | `UNRECORDED_REFUND` | refunded at the gateway, not in the ledger |
| high | `MISSING_SETTLEMENT` | payout overdue, no bank deposit |
| high | `PAYOUT_AMOUNT_MISMATCH` | bank deposit ≠ sum of payout nets |
| medium | `AMOUNT_MISMATCH` | ledger amount ≠ gateway gross |
| medium | `MISSING_IN_GATEWAY` / `MISSING_IN_LEDGER` | one-sided record |
| medium | `DUPLICATE_LEDGER_ENTRY` | same journal posted twice |
| medium | `FEE_VARIANCE` / `NET_INCONSISTENT` | fee off-contract, or bad net arithmetic |
| medium | `NOT_PAID_OUT` | captured but never put into a payout |
| medium | `UNIDENTIFIED_DEPOSIT` | bank credit with no source |
| low | `DELAYED_SETTLEMENT` | arrived after SLA + grace |
| low | `REFERENCE_MISMATCH` | paired by amount+date because references differ |
| low | `DUPLICATE_WEBHOOK` | same txn reported twice; counted once |

## Resolution history

- Status flow: `open → investigating → resolved | accepted`, plus `reopen`. Resolving,
  accepting and reopening all require a note.
- On a re-run, an open exception that is no longer detected becomes **auto_cleared**. A
  resolved exception that is still detected is **reopened** automatically, so a
  "resolved" status can't hide a problem that still exists.
- Events are append-only, enforced by SQLite triggers, and **SHA-256 chained**.
  `ledgerlens verify` or the dashboard badge detects any edit to the history, even one
  that bypasses the triggers.

## Layout

```
ledgerlens/
  ingest.py   CSV loaders, aliases, money/date parsing
  bank_api.py bank API simulator + paginated client
  engine.py   deterministic + probabilistic matching, classifier, reports
  store.py    SQLite: runs, reports, exception queue, reviewer links, hash-chained events
  service.py  one run end to end (shared by CLI and server)
  export.py   CSV exports for exceptions and reports
  server.py   JSON API, simulator endpoint, dashboard (static/index.html)
  samples.py  deterministic demo data with one planted case per problem
  cli.py
tests/test_ledgerlens.py
```
