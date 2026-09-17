# Open Finance Future

This directory is intentionally a placeholder for a future Open Finance or banking aggregator integration.

Current data sources are:

- `manual`: user-entered transactions.
- `csv_import`: transactions imported from user-provided CSV files.
- `open_finance_future`: reserved architecture value. No real bank connection is implemented yet.

Future integrations should implement `FinancialDataSource` (in
`app/integrations/base.py` — a design contract, not live code today) and
normalize fetched data into `ImportedTransaction`. From there, reuse the
same pipeline the CSV importer already uses: `app/integrations/normalizer.py`
(`build_duplicate_hash`, `parse_decimal_text`, `normalize_duplicate_text`)
for decimal parsing and duplicate-hash generation, then persist through
`app/imports/service.py`'s category-resolution and duplicate-detection logic
— never inserting rows directly, so every source shares the same
deduplication and user-scoped authorization guarantees.

See `docs/architecture/overview.md` for how `app/imports/` fits into the
rest of the backend, and `docs/architecture/adr-open-finance.md` for the
Pluggy vs. Belvo comparison, the `connections`/`sync_runs` domain model,
and the current decision (postponed, with a re-evaluation trigger) before
any real implementation starts here.
