# Market And Model Risk Control Case Study

Scope: controls in a research paper portfolio, not institutional market-risk
capital, broker reconciliation, VaR or expected shortfall.

## Shared Fixture Exposure

Two Polymarket venue events can reference one external soccer fixture. Grouping
exposure by venue event alone allowed a configured 100-share event limit to admit
200 shares. The fix preserves venue identity but adds a canonical provider/fixture
key. Holdings and new orders are checked against that shared key. Durable aliases
survive restart; missing/conflicting established identities fail closed.

The regression accepts one 100-share fill across two wrappers, rejects further
fixture exposure after restart and reconciles persisted exposure to 100. This is
a synthetic control test, not an empirical claim that portfolio loss fell 50%.

```bash
python -m pytest -q tests/test_prospective_study.py::test_canonical_fixture_cap_across_venue_wrappers_and_restart
python -m pytest -q tests/test_fixture_risk_identity.py
```

Share limits bound unresolved inventory; they are not a full correlated-tail-risk
model. Provider IDs do not automatically reconcile multiple external providers.

## Funding And Accounting

Buys require available cash and sells require existing token inventory. Fees and
slippage debit cash separately from the displayed-price fill. SQLite transactions,
stable decision IDs and invariant checks protect against duplicate fills, partial
accounting commits, conflicting settlements and state reset during resume.
Daily-loss stops use a persisted UTC first-observation equity baseline and remain
latched until the next day. Missing/stale marks cannot silently enable new risk.

```bash
python -m pytest -q tests/test_portfolio_invariants.py tests/test_quant_hardening.py
```

These checks do not model exchange margin, liquidation, hedging capacity, wallet
reconciliation or every gap-risk scenario. Risk limits are research assumptions.

## Time And Availability

A decision cannot use an observation received in the future even when its source
timestamp is earlier. Book/odds age and cross-source skew are separate checks.
Exact-token follow-ups use bounded lateness and sufficient displayed exit depth;
missing observations remain missing rather than being replaced by a midpoint.

```bash
python -m pytest -q tests/test_backtest_no_lookahead.py tests/test_research_integrity.py tests/test_timestamp_parsing.py
```

During the real feasibility observation, timing checks blocked all entries.
Abstention is a control outcome, not proof that the underlying market had no edge.

## What A Risk Reviewer Should Challenge

The controls are code-tested, not independently certified. Fee/slippage inputs
are placeholders; uncertainty is heuristic; sources may be correlated; matching
has no adjudicated precision/recall benchmark; actual queue/arrival fills are
unvalidated. Operational monitoring, broker reconciliation, stress calibration
and a prospective empirical sample remain unfinished.
