"""Keep legacy diagnostics visibly distinct from the canonical evidence record."""

LEGACY_WARNING = (
    "> UNVALIDATED LEGACY DIAGNOSTIC. These results are historical or exploratory, "
    "not verified current performance. Pre-hardening fills and markouts are not "
    "validated trading returns. Snapshot counts do not establish independence, "
    "profitability or permission to scale. See reports/current_evidence.md."
)


def label_legacy_report(report: str) -> str:
    if LEGACY_WARNING in report:
        return report
    title, separator, rest = report.partition("\n")
    return title + "\n\n" + LEGACY_WARNING + "\n" + separator + rest
