# Public Evidence Record

As of 2026-10-13. Research only; profitability unproven; live orders disabled.

## What Can Be Reproduced

The repository includes synthetic fixtures and offline regression tests.
`scripts/reproduce_research.py` creates a hash-sealed synthetic run and replays
its numerical signal, decision and matching CSVs. The five-market integration
has four fair valuations, four matches, one rejected outright and zero fills
under code defaults. This tests the pipeline, not a strategy performance record.

A separate synthetic lifecycle deliberately manufactures two paper fills and
timed follow-ups across one fixture. It tests accounting and observation timing;
its positive markouts are not empirical alpha. Recreate it with
`scripts/run_prospective_study.py --mock --study-dir reports/my_synthetic_study`.

## Bounded Real Feasibility

The user authorized three Odds-API.io requests and public Polymarket reads.
Ten league-scoped fixtures were returned. Betfair Exchange odds were denied
with HTTP 403; the provider reported that this account's free plan excludes
sharp/exchange books. A separate Bet365-only diagnostic reused the fixture IDs.

| Measurement | Observation |
| --- | ---: |
| Provider fixtures | 10 |
| Recreational bookmaker 1X2 rows | 30 |
| Polymarket candidate cap reached | 30 markets |
| Algorithmic matches | 24 contracts across 8 fixtures |
| Rejected candidate markets | 6 |
| Captured token books | 48 |
| Individually fresh token books at original as-of | 12 |
| Jointly fresh priced outcomes | 0 |
| Real-data paper trades / live orders | 0 / 0 |
| Real CLV / net markout / settled strategy PnL | Unavailable |

The observation as-of was 2026-10-13 14:35:52 UTC (22:35:52 Hong Kong).
Candidate truncation prevents a universe census; algorithmic matches have not
been adjudicated for economic settlement equivalence. Bet365 coverage is not a
certified sharp fair-value anchor. Costs/uncertainty remain placeholders.

The actual responses exposed numeric-string CLOB timestamps being rejected.
Offline correction preserved the source, receipt and original observation
clocks and made no new HTTP calls. Serial collection took roughly 75 seconds;
timing remained insufficient for eligible entries even after the parsing fix.
This is not evidence that no economic edge existed.

## Limits Of The Public Package

The figures above are an aggregate account of private local research records,
not an externally audited or publicly replayable real-data study. Raw provider
payloads, credentials, private paths and portfolio databases are intentionally
excluded; this repository does not grant data-redistribution rights. Synthetic
inputs and control tests are public and independently rerunnable.

Legacy repeated snapshots and pre-hardening paper entries are not validated
performance. In particular, 570 repeated rows and 170 apparent positive edges
must not be marketed as independent signals or profitable rejected trades.

## Still Required

Accessible suitable anchors; reviewed settlement pairs; adjudicated matching;
synchronized training collection; calibrated costs, uncertainty and execution;
an independent-event power plan; and a completed untouched prospective
evaluation. There is no validated profitability, capacity or real-money readiness.
