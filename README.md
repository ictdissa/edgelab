# EdgeLab

Prediction-market relative-value research in Python.

EdgeLab compares Polymarket bid/ask quotes with external probability estimates,
checks whether the contracts are comparable, and records risk-checked paper
decisions. The main study covers pre-match soccer 1X2: home win, draw and away
win after regulation time.

**Research only. Live orders are disabled. Profitability has not been established.**

## Method

```text
External odds -> event / outcome / settlement matching -> no-vig probabilities
              -> executable quotes -> costs and risk checks -> paper ledger
              -> timed follow-ups, quote markouts and PnL reconciliation
```

- Use complete bookmaker snapshots; remove the margin within each book before
  averaging probabilities. Reject unsupported or ambiguous payoffs.
- Price purchases at the ask and inventory sales at the bid. Subtract modeled
  fees, slippage, uncertainty and safety buffers; never assume midpoint fills.
- Check source and receipt times, available size, funding and shared fixture
  exposure. Preserve rejected decisions and their reasons.
- Keep external-reference CLV, executable quote markouts and accounting PnL
  separate. Historical decisions cannot use observations received later.

## Quick Start

Python 3.11 on macOS or Linux; file locking requires POSIX. No API keys are needed.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

python scripts/run_research_analysis.py --mock --output reports/mock_research_analysis.md
python scripts/run_paper_trader.py --mock
python scripts/update_clv.py --mock
```

The bundled fixtures are historical synthetic data, not current opportunities or
a strategy-performance backtest. Read the [example report](reports/public/mock_example.md).

## Results

The 2026-10-13 feasibility check found 24 algorithmic contract matches across
eight soccer fixtures. Zero priced outcomes passed the joint freshness checks,
so no real-data paper trades were entered. The available bookmaker feed was
recreational, not a validated sharp anchor; settlement equivalence was not certified.
See the [evidence record](reports/public/evidence.md) for coverage and limitations.

Fees, slippage, uncertainty and actual fill behavior still require calibration.
Crypto thresholds and internal constraints are exploratory; weather is
synthetic-only and election pricing is disabled. No multi-leg orders are routed.

## Development

```bash
python -m pytest -q
ruff check edgelab scripts tests
mypy --explicit-package-bases edgelab scripts/run_prospective_study.py scripts/export_public_release.py
```

Tests prohibit real network connections. The [validation record](reports/public/validation.md)
covers clean-install checks and numerical replay. Hosted CI is not yet verified.
The pinned dependency snapshot is not a cross-platform, hash-verified lock.

## Documentation

- [Operating guide](docs/operating_guide.md): collection, configuration and replay.
- [Experiment plan](research/soccer_1x2_experiment_plan.md): training and held-out evaluation.
- [Model-risk register](docs/model_risk_register.md): assumptions and remaining gaps.
- [Research journal](docs/research_journal.md): findings and design decisions.

Provider credentials are optional for offline work. Live collection uses cached,
rate-limited adapters; Odds-API.io is capped locally at 90 requests/hour.
The retained [SharpEdge predecessor](docs/legacy_compatibility.md) is deprecated.

MIT-licensed code; provider data retains its own terms. Research and education
only, not financial advice.
