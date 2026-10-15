# LedgerLens: intelligent payment reconciliation

LedgerLens reconciles a merchant's **internal ledger**, **payment-gateway report** and
**bank settlement file**. It finds missing, duplicated, delayed and mismatched payments,
explains each one in plain English, and keeps a tamper-evident resolution history.

It runs on Python 3.10+ and uses only the standard library.

## Quick start

```bash
python3 -m ledgerlens demo --fresh     # sample data + three daily close-outs (8, 12, 15 Oct)
python3 -m ledgerlens serve            # dashboard at http://127.0.0.1:8765
python3 -m unittest -v tests.test_ledgerlens
```

Reconcile your own files, with the bank side from a CSV or from a bank API:

```bash
python3 -m ledgerlens run --ledger ledger.csv --gateway gateway.csv --bank bank.csv --as-of 2026-10-15
python3 -m ledgerlens run --ledger ledger.csv --gateway gateway.csv --bank-api http://127.0.0.1:8765/sim/bank/v1
python3 -m ledgerlens list --status active
python3 -m ledgerlens show MISSING_IN_GATEWAY:JE-5033
python3 -m ledgerlens act MISSING_IN_GATEWAY:JE-5033 link --candidate ch_1032A --actor ana --note "promo"
python3 -m ledgerlens verify
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
  server.py   JSON API, simulator endpoint, dashboard (static/index.html)
  samples.py  deterministic demo data with one planted case per problem
  cli.py
tests/test_ledgerlens.py
```
