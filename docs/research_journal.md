# Research Journal

## Question

Can external probability anchors and logical payoff relationships identify
prediction-market mispricings after executable prices, costs and risk?
The project began as SharpEdge Sports. EdgeLab is the research successor; the
predecessor remains in source for compatibility and regression tests.

## Discovery Was The First Failure

An early generic scan found outright World Cup winners while the external feed
contained individual fixtures. One shared team name does not make these the
same contract. Discovery is now fixture-targeted. Matching checks both teams,
kickoff, outcome/token identity and ambiguous alternatives. A separate reviewed
settlement specification is required before non-synthetic soccer paper trading.

## A Quote Was Not A Trade

Audits reproduced incomplete 1X2 vintages, incorrect contract joins, unsupported
crypto payoffs, timestamp substitution and follow-ups outside their horizons.
The code now preserves source and receipt time, uses complete coherent book
vintages and rejects unsupported contracts. Cash-funded buys and inventory-only
sells replace assumptions of free collateral or naked shorts.

Legacy campaigns included repeated snapshot counts and pre-hardening paper
entries. They are not validated strategy returns. The public package does not
count the old 570 rows and 170 positive-edge rows as independent observations.

## Risk Controls Needed Durable Identity

A reproduced bug allowed two venue wrappers of one fixture to consume two event
limits. Canonical provider/fixture identity now groups both wrappers, persists
across restart and refuses conflicting reassignment. A synthetic regression
admits 100 shares total under a 100-share fixture cap, not 200.

Another defect missed trailing collection outages. A study collected once and
revisited after its deadline now reports 71 missing slots out of 72, including
nine missing entry slots. Calendar completion is not complete data collection.
These are engineering/control results, not measured reductions in trading loss.

## Bounded Real Observation On 2026-10-13

Three authorized Odds-API.io requests returned ten upcoming Premier League
fixtures. The Betfair Exchange odds request was denied with HTTP 403; the
provider reported that this account's free plan excludes sharp/exchange books.
A separately labeled Bet365-only diagnostic reused the recorded fixture IDs.
It is recreational source coverage, not a substituted sharp-source experiment.

Targeted discovery returned 30 candidate markets, with 24 algorithmic matches
across eight fixtures and six rejects. The candidate cap prevents a census or
matching-accuracy claim. Forty-eight CLOB token books were captured.

Real responses exposed a parser bug: source timestamps were millisecond strings,
not JSON numbers. The parser is fixed. Offline reprocessing retained the original
as-of and made zero additional requests. Twelve token books passed individual
freshness; zero priced outcomes passed the joint timing checks. The broad serial
collection took roughly 75 seconds. No real-data paper trades or CLV resulted.

Raw provider payloads remain private. The public aggregate account is not an
independently replayable real-data experiment; synthetic examples are reproducible.

## Current Decision

No actionable edge or profitability is established. The next experiment needs
accessible suitable anchors, adjudicated matching/settlement terms, synchronized
training collection, verified costs and calibrated abstention buffers, followed
by a preregistered untouched evaluation. More modules or a lower threshold would
not supply that evidence. A rigorous negative result would still be useful.
