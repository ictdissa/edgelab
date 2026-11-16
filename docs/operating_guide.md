# Operating Guide

## Offline First

The root README commands require no API keys. Synthetic fixtures live under
`edgelab/data/samples` and the retained `sharpedge/data/samples`; root `data/`
is runtime/private storage. Tests block real network connections.
The predecessor is [deprecated compatibility code](legacy_compatibility.md);
use the EdgeLab entry points below for current research.

Use the sealed example to generate inputs, settings, source hashes, decision
CSVs and a manifest in a new empty run directory. Verification detects changed
artifacts/source. After editing code, use the recorded source version to replay
an older package; `--archive-only` verifies archived integrity only.

```bash
python scripts/run_edge_scan.py --mock
python scripts/run_backtest.py --mock
python scripts/reproduce_research.py --mock --output-dir reports/my_run
python scripts/reproduce_research.py --verify --output-dir reports/my_run
python scripts/reproduce_research.py --replay-from reports/my_run --output-dir reports/my_replay
```

The backtest command is a synthetic execution check, not a performance estimate.
Use a new output directory for each sealed run. Hash verification detects changed
inputs and code; it does not establish provider truth or external timestamping.

## Live Research

The narrow feasibility mode is cache-only by default, including cache misses:

```bash
python scripts/run_prospective_study.py --pilot \
  --design research/soccer_1x2_pilot_design.json \
  --study-dir reports/my_feasibility
```

Only add `--refresh` when deliberately authorizing provider requests. This mode
admits no trades, caps odds attempts at three per run including failures and
retains the shared 90/hour local limit. Credentials come from `ODDS_API_IO_KEY`
in a private `.env` or environment. Do not reset or duplicate rate-limit state
to bypass the account budget. Provider plan/entitlement and vendor limits still apply.

Copy field names from `edgelab/config/example.env`; never commit the private
file. Rotate credentials previously shared in chats/logs. Separate source designs
must be explicit; the registered study adapter does not substitute denied books.

General exploratory collectors may request on cache misses. They are not the
strict cache-only feasibility command and not preregistered evaluations.

## Future Study Registration

Before registering, review settlement pairs and choose fixed future UTC dates,
one league/slug, exact source identifiers, scope, cost/risk settings and cadence.
Training calibration must precede a held-out evaluation. The supplied protocol
is a template, not an existing evaluation; example settlement terms cannot enable trades.

```bash
python scripts/run_prospective_study.py --register \
  --protocol research/my_reviewed_protocol.json \
  --design research/my_frozen_design.json \
  --settlement-registry research/my_reviewed_pairs.json \
  --study-dir data/studies/my_pilot

python scripts/run_prospective_study.py --run --study-dir data/studies/my_pilot
```

Each invocation performs at most one due cycle. There is no installed scheduler.
Missed slots remain missing; the end boundary is not a completeness certificate.
The runner freezes settings and refuses changed source, conflicting portfolio
state, future data and unauthorized entry windows. Follow-ups continue after
the entry cutoff for the fixed horizon plus allowed lateness.

Default source gates are 30 seconds for books, 180 seconds for odds and 60
seconds cross-source skew. These are conservative settings, not calibrated
execution estimates. Broad serial scans can exceed the timing budget; validate
batch/stream collection or narrow the universe rather than relaxing gates to
manufacture opportunities.

## Reading Results

- Inspect matching and freshness diagnostics before pricing or PnL.
- Reviewed settlement is separate from matching names and times.
- Buys use ask, funded inventory sales use bid, never midpoint fills.
- Cost drag includes per-share slippage, quote-notional fees, heuristic
  uncertainty, resolution buffer and safety margin. It is not all cash expense.
- Exact-token follow-ups require a first eligible quote within 60 seconds of
  the horizon and enough displayed exit size. Missing CLV is not zero CLV.
- Aggregate by canonical fixture, not by individual trade count. Block
  independence, sample power and missingness remain research questions.

Real-money controls, operational support and verified empirical profitability
are not supplied by these scripts. Live order placement remains impossible.
