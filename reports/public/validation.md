# Publication Validation

See `release_validation.json` for the machine-recorded local results of the
publication pass. The clean-export checks are run on an allowlisted source
package without `.env`, raw provider responses or historical run directories.

## Current Checks: 2026-11-16

A fresh Python 3.11.15 macOS arm64 virtual environment installed all 63 pinned
dependencies offline from compatible cached wheels, then installed the clean
source export. It passed 475 tests, Ruff, mypy (68 source files), all five offline
workflow commands, the synthetic prospective-study lifecycle, sealed artifact
verification and byte-identical replay of all three numerical CSVs. Dependency
consistency checks passed. No network requests were made for installation or data.
The private `.env` was absent and the imported source belonged to the clean export.

This includes 66 isolated, network-blocked module-import cases. The remaining
new cases cover unsupported payoff rejection, mislabeled CSV/risk bypass attempts,
mixed-category provider responses, target mapping and retired legacy pricing.
Publication checks also exclude application notes and prevent a legacy diagnostic
report from certifying design or software checks it has not performed.
The earlier 350-case suite had not exposed these defects; count alone was not
evidence of correctness. The updated counterexamples pass without provider calls.

The 2026-10-13 publication pass had 350 tests; its archived artifacts are unchanged.
New source versions require new sealed runs, not relabeling old evaluations.
A larger count is regression coverage, not evidence of alpha. Source truth,
matching accuracy, settlement, costs and actual fills remain separate assumptions.

Hosted GitHub Actions has not run merely because its workflow is included.
The repository has not been uploaded or made public by the local export command.
The exporter scans for configured secret values and selected credential patterns;
that is not a general security certification or proof that a key was rotated.

Use the dated 2026-11-16 export rather than the older ZIPs. Public evidence still
contains only synthetic reproducible examples and the explicitly limited
2026-10-13 aggregate feasibility disclosure; no new live result is claimed.
