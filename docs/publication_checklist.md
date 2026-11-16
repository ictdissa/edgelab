# Publication Checklist

1. Rotate any credential previously disclosed in chats/logs. Rotation requires
   the provider account and is not performed by this repository.
2. Run the offline tests, Ruff, mypy and numerical replay. Read the evidence and
   model-risk register; do not use superseded counts as trading performance.
   Fresh-interpreter import tests must pass independently of suite import order.
3. Export with `scripts/export_public_release.py` and verify its manifest. The
   exporter refuses configured secrets, selected credential patterns, symlinks
   and non-allowlisted members. It does not certify every possible secret format.
4. Inspect the ZIP inventory. No `.env`, raw API responses, source-run archives,
   portfolio databases, CVs, personal contact documents or Git history belongs
   in this public package. Synthetic fixtures and aggregate evidence are included.
5. Extract into a new directory; perform a fresh installation and run the
   acceptance commands there. A passing existing virtualenv is not a fresh install.
6. Create a repository only when ready and review visibility before pushing.
   Upload the extracted public package, not this entire working directory.
7. Confirm hosted CI is green after publication. Until then describe it as a
   workflow supplied, not hosted CI verified. Recheck staged files and history
   for secrets; `.gitignore` cannot remove previously committed credentials.

Use the latest dated verified export. The 2026-10-13 archives predate the
publication-hardening fixes and are retained only for historical reference.

Code is MIT licensed. Provider data is excluded and not relicensed. No release
step enables trading, buys a subscription, makes market-data requests or uploads
anything. A successful export is not approval for live money.
