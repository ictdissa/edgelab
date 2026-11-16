# Legacy Compatibility

`sharpedge/` and several older scripts are retained to make historical formats
and regression tests inspectable. They are deprecated, not a second supported
research engine. Their previous paper results are not validated performance.

Use the root README and `docs/operating_guide.md` for supported EdgeLab commands.
In particular, use `run_live_research_analysis.py` for exploratory collection and
`run_prospective_study.py` for bounded feasibility and registered studies.

The older `run_live_odds_first_scan.py`, `run_polymarket_event_edge_scan.py`,
`find_odds_for_polymarket_event.py` and `build_matched_odds.py` retain predecessor
schemas. Do not mix their outputs into a current EdgeLab study or paper ledger.
Their caches, matching labels and legacy pricing do not certify economic
settlement equivalence, source sharpness or point-in-time execution eligibility.

The legacy ID rewriter no longer constructs No odds from one team's implied
probability. Outright rewriting and legacy outright valuation are disabled:
the old schema cannot establish full-field completeness. This is intentional
abstention, not evidence that an outright market is efficiently priced.

Historical source archives are unchanged and excluded from public releases.
Reproduce an older archive only with its recorded source version; it must not
be described as an evaluation of the hardened EdgeLab version.
