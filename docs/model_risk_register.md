# Model Risk Register

Status reflects implemented safeguards, not regulatory or independent validation.

| Assumption or risk | Control | Residual limitation |
| --- | --- | --- |
| Bookmaker odds are a fair-value anchor | Complete coherent no-vig vintages; exact outcome mapping | Proportional de-vig removes overround, not bias; source sharpness/independence unvalidated |
| Fixture identity implies economic equivalence | Both-team, kickoff, type and token checks; separate reviewed settlement gate | Human evidence required for cancellation, postponement and regulation-time terms |
| A supplied winner category proves the payoff | Shared classifier/payoff checks at resolver, valuation, signal and risk boundaries; compound/margin/period props rejected | Rule-based exclusions are not a complete semantic parser; no adjudicated precision/recall benchmark |
| A recent download means recent data | Preserve source and receipt time; age/skew gates | Source-clock semantics and actual update cadence need operational validation |
| Displayed depth is fillable | Enter at ask/bid with displayed-size and funding caps | Arrival delay, cancellation races, partial fills, impact and queue position unvalidated |
| Configured costs reflect venue economics | Freeze costs at entry and label net markouts as estimates | Fee/slippage placeholders are not a verified fee schedule or calibrated fill model |
| Uncertainty buffer is calibrated confidence | Explicit buffer and conservative abstention | Hand-set penalties, not confidence intervals or probability of profit |
| Many trades mean many independent samples | Equal-weight canonical fixture aggregation; missingness diagnostics | Event independence and power must be assessed on real training data |
| Each venue wrapper is separate exposure | Durable canonical fixture keys; invariant checks | No cross-provider reconciliation or comprehensive correlated-risk model |
| Local hash sealing proves truth | Input/source/output hashes and deterministic replay | Unsigned local integrity is not external timestamping or provider truth |
| Missing follow-ups can be ignored | Report missingness and adverse bounds; no midpoint substitution | Nonrandom missingness can bias complete-case estimates |
| Positive synthetic markouts prove alpha | Evidence types are explicit; no live-order implementation | No completed validated held-out real-data result or settled strategy return |

The intended initial estimand is equal-weight fixture five-minute net executable
quote markout in dollars per share. External-anchor CLV, accounting PnL, trade
counts and portfolio returns are separate quantities. A positive interval would
not automatically authorize scaling or real money.
