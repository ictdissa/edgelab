# Soccer 1X2 Research Plan

Status: feasibility observed; training and held-out evaluation have not started.
Research only. No trading account, live orders, background scheduler or paid-plan
upgrade is authorized by this document.

## Hypothesis And Contract

Test whether complete pre-match regulation-time soccer 1X2 bookmaker vintages
contain information that predicts positive five-minute executable quote markouts
after costs. Start with one league: England - Premier League. Fixture identity,
kickoff, outcome, token and settlement equivalence must all agree. Qualification,
overtime, outrights and in-play markets are outside scope.

Bookmaker no-vig probabilities are candidate anchors, not ground truth. Requested
sources, their entitlement and their economic interpretation must be explicit.
Bet365-only feasibility data is recreational coverage; it cannot silently replace
the proposed sharp/exchange-source hypothesis. No source weighting or threshold
will be chosen using held-out profitability.

## Stage 0: Feasibility

The 2026-10-13 observation used exactly three Odds-API.io requests in total and
public Polymarket reads. The shared 90/hour limit was retained.

| Observation | Result |
| --- | --- |
| League-scoped fixture feed | Ten upcoming fixtures returned |
| Bet365 + Betfair Exchange request | HTTP 403; the provider says free plans exclude sharp/exchange books |
| Separately labeled Bet365-only check | Thirty 1X2 rows returned for ten fixtures |
| Targeted Polymarket discovery | Thirty candidates; candidate cap reached |
| Algorithmic matches | Twenty-four markets across eight fixtures; six rejected |
| Raw CLOB snapshots | Forty-eight token books |
| Timestamp parser | Numeric millisecond strings exposed a bug; corrected and regression-tested |
| Offline reparse at original as-of | Twelve books pass individual freshness; zero outcomes pass joint quote/odds timing |
| Orders / paper fills / CLV | Zero / zero / unavailable |

These are algorithmic coverage diagnostics, not adjudicated matching accuracy or
returns. The candidate cap prevents a universe-coverage claim. Original responses
are retained separately from reparsed observations; no timestamps are relabeled
as current. The public aggregate is in `reports/public/evidence.md`; original
provider responses and detailed local run artifacts are excluded from the public
export. Missing diagnostic pricing is blank, not a zero price or an edge.

The approximately 75-second serial collection also demonstrates an operational
constraint: a broad scan cannot be treated as one synchronous executable book.
Before a continuous campaign, validate source-clock semantics and narrow the
collection universe or implement validated batch/stream collection. Do not
relax freshness/skew limits merely to make the funnel produce trades.

## Stage 1: Training-Only Data-Quality Pilot

Before collection, choose fixed UTC training dates and the exact accessible
sources. Keep this dataset disjoint from any future evaluation window.

1. Verify provider entitlement, source timestamps, receipt timestamps and actual
   update cadence. Retain failures and discovered-but-unqueried fixtures.
2. Review each venue/book pair's regulation time, cancellation, postponement and
   abandonment terms with dated evidence. Do not infer equivalence from titles.
3. Adjudicate event/outcome matches against an independent review. Report false
   matches and exclusions, not just accepted-score distributions.
4. Collect complete coherent 1X2 vintages, executable depth, collection latency,
   arrival-book changes and subsequent quotes. Include rejected opportunities.
5. Verify venue fees and exchange commission where relevant. Estimate latency,
   disappearing liquidity and partial-fill sensitivity. Displayed size alone
   is not an executable fill guarantee.
6. Evaluate no-vig anchors against closing anchors and observed resolutions
   separately. Closing odds are a reference, not truth. Measure book dependence
   and alternative no-vig sensitivity before selecting a frozen model.
7. Calibrate abstention buffers on training events. A book-disagreement penalty
   is not a confidence interval or a measured probability of profit.

The current code's costs and uncertainty remain placeholders. No pilot variance
or calibrated coefficients are fabricated from this feasibility observation.

## Stage 2: Freeze An Untouched Evaluation

Register only after the pilot and settlement review. Freeze code, sources,
league, UTC dates, universe-selection policy, cost/uncertainty assumptions, risk,
primary estimand and exclusions. The protocol template and study runner already
support future registration; a feasibility observation is not that registration.

- Primary unit: canonical external fixture. Repeated trades and Yes/No wrappers
  of one fixture do not increase the independent event count.
- Primary endpoint: equal-weight fixture mean five-minute net executable quote
  markout, in dollars per share, using the first eligible exit quote and adequate
  displayed exit depth. This is not portfolio return or settled PnL.
- Planning target: 0.005 dollars/share; two-sided alpha 0.05 and target power 0.8,
  as in the existing protocol. Estimate the event-mean standard deviation from
  the training pilot before calculating a sample requirement. The normal
  approximation does not establish independence or adequate power.
- Stops: fixed evaluation end, never a favorable interim result. Collection
  outages and unavailable follow-ups remain missing, not zero returns.
- Diagnostics: anchor CLV, quote markouts, settled accounting PnL, missingness
  bounds, additional cost stress and category/source coverage reported separately.
- Benchmarks: no-trade and a predeclared simple raw-edge selector, plus market
  and bookmaker calibration on resolved covered fixtures. Shadow-baseline
  collection/comparison must be validated before claiming it was implemented.

Funded paper buys are the initial entry route. Inventory-only sales cannot be
counted as an independently available naked-short strategy. No result authorizes
real money automatically, including a positive confidence interval.

## Decisions And Falsification

- No comparable coverage: fix discovery or revise the research universe in a
  new protocol; do not shop leagues until positive results appear.
- No accessible sharp anchor: resolve access or explicitly change the hypothesis
  to a recreational-odds baseline. Do not relabel recreational odds as sharp.
- Unreliable timestamps or execution: study is not ready; do not report alpha.
- No signals with valid data: report a valid zero-opportunity result, including
  the cost/uncertainty funnel. Do not tune on the evaluation window.
- Negative net markouts or uncertainty spanning zero: report the result; do not
  retrospectively filter losing events or replace the primary endpoint with PnL.
- Positive held-out evidence: replicate across another untouched period and
  validate capacity and operational/accounting controls before considering any
  separately authorized live-trading implementation.

A rigorous negative result is useful. Source access, matching coverage and test
counts are engineering evidence, not substitutes for this experiment.
