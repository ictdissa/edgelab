# EdgeLab Mock Research Analysis

## Executive Summary

- Markets scanned: 5
- Fair values generated: 4
- Signals generated: 4
- Paper trades entered: 0
- Skipped trades: 4
- Generated at: 2026-11-16T10:38:00.353202+00:00

No live trading was attempted. This run is research/paper only.
Mock data is deterministic plumbing data, not evidence of profitability.
Historical synthetic sample replay as of 2026-05-18T00:00:00+00:00. These are not today's tradable events.
No actionable edge found.
Synthetic weather values use a demonstration distribution, not a calibrated rainfall forecast.

## Data Coverage

| category | count |
| --- | --- |
| sports_1x2 | 2 |
| crypto_threshold | 1 |
| weather_threshold | 1 |
| sports_outright | 1 |

## Matching Diagnostics

| market | decision | score | candidate_count | alt_score | rejection_reason | matched_outcomes | reasons |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Will Arsenal win against Chelsea on 2026-06-01? | MATCH | 1.000 | 3 | 0.000 |  | {'Arsenal': 'Yes'} | event_id_match=True; score=1.000 |
| Will Arsenal vs Chelsea end in a draw? | MATCH | 1.000 | 3 | 0.000 |  | {'Draw': 'Yes'} | event_id_match=True; score=1.000 |
| Will BTC be above $100,000 on June 30, 2026? | MATCH | 1.000 | 3 | 0.000 |  | {'Yes': 'Yes'} | event_id_match=True; score=1.000 |
| Will NYC rainfall exceed 1 inch on July 4, 2026? | MATCH | 1.000 | 3 | 0.000 |  | {'Yes': 'Yes'} | event_id_match=True; score=1.000 |
| Will Spain win the 2026 FIFA World Cup? | REJECT | 0.000 | 3 | 0.000 | fixture_or_contract_type_mismatch | {} | event_id_match=False; score=0.000; partial outright field cannot anchor a yes/no outright; no outcome mapping |

A machine-readable audit is written to `mock_event_match_audit.csv`.

## Edge Table

| market | category | fair | bid | ask | raw_edge | cost_drag | adjusted | liquidity | freshness | data_age_s | fair_source | action | risk/reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pm-ars-che-ars | sports_1x2 | 0.469 | 0.430 | 0.450 | 0.019 | 0.034 | -0.016 | 70.0 | fresh | 0 | sports_no_vig:2books | HOLD | adjusted edge below threshold |
| pm-ars-che-draw | sports_1x2 | 0.272 | 0.280 | 0.310 | 0.008 | 0.031 | -0.023 | 35.0 | fresh | 0 | sports_no_vig:2books | HOLD | adjusted edge below threshold |
| pm-btc-100k | crypto_threshold | 0.358 | 0.380 | 0.400 | 0.022 | 0.079 | -0.057 | 50.0 | fresh | 0 | crypto_lognormal_threshold | HOLD | adjusted edge below threshold |
| pm-nyc-rain | weather_threshold | 0.212 | 0.240 | 0.270 | 0.028 | 0.098 | -0.070 | 30.0 | fresh | 0 | synthetic_weather_normal_example | HOLD | adjusted edge below threshold |

## Risk Rejections

| reason | count |
| --- | --- |
| signal action is HOLD; adjusted edge below threshold | 4 |

## Paper Trades

| market | side | price | size | approved | exec_source | fair | adjusted_edge | reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pm-ars-che-ars | SKIP |  | 0.0 | False | none | 0.469 | -0.016 | signal action is HOLD; adjusted edge below threshold |
| pm-ars-che-draw | SKIP |  | 0.0 | False | none | 0.272 | -0.023 | signal action is HOLD; adjusted edge below threshold |
| pm-btc-100k | SKIP |  | 0.0 | False | none | 0.358 | -0.057 | signal action is HOLD; adjusted edge below threshold |
| pm-nyc-rain | SKIP |  | 0.0 | False | none | 0.212 | -0.070 | signal action is HOLD; adjusted edge below threshold |

## CLV Results

No realized CLV is claimed in the mock research report. CLV requires approved paper trades plus later fair-value/market snapshots.

## Cost Decomposition

Cost drag equals slippage + fees + model uncertainty + resolution risk + safety margin. A signal is tradable only if raw edge remains positive after this drag and risk checks.

## Failure Modes

- Mock anchors are simplified and not evidence of profitability.
- Partial outright fields are rejected rather than treated as exhaustive.
- Apparent edges must survive bid/ask, fees, uncertainty, liquidity, and risk checks.

## Evidence For Or Against Profitability

This run is a plumbing and data-quality test. No live-money conclusion should be drawn.

## Next Experiments

- Review settlement-equivalent pre-match soccer 1X2 contracts and record coherent odds/books.
- Calibrate uncertainty and execution costs using training events only.
- Register a future evaluation window before held-out collection; distinguish net quote markouts from settled PnL.

## Evidence Status

SYNTHETIC_INTEGRATION_ONLY. This retrospective smoke replay is not preregistered research.

No validated live CLV, calibration, capacity or net-profitability result exists. A future registration requires explicit dates before evaluation starts.
