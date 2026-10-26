# Evaluation Report — Ghost Data Still Exists

Generated: 2026-10-25T13:19:33.258247+00:00

## Public sample comparison

The reference is `dataset/sample_output.csv` (public cases only). Hidden evaluation labels are not available to this report.

- Cases compared: 32
- Case-level deletion-status accuracy: 15/32 (46.9%)
- Step-level exact-position accuracy: 23/193 (11.9%)
- Field-level accuracy across the seven required fields: 233/1351 (17.2%)
- Exact ordered-sequence accuracy: 1/32 (3.1%)

## Additional designed cases

1. **Historical identifier with reassignment:** Resolve an old email using its ownership interval at `requested_at`; do not connect the new owner to the prior user's data. Expected: audit only the original user's evidence and mark unresolved identity as incomplete.
2. **Deletion requested while the account stays active:** “Delete my personal data, but leave my account active.” Expected: deletion is authorized; preserve an account tombstone only when policy requires it and redact direct identifiers.
3. **Restore claim newer than a snapshot:** A workflow reports a successful restore after the last source snapshot. Expected: the old snapshot is stale, the current state is unverified, and the case cannot be marked complete until a later source snapshot resolves it.

These cases are design checks; this report does not claim they were executed unless they are included in a separately recorded test run.

## Failures found and improvements made

- Activity `SUCCESS` results no longer overwrite a source snapshot as proof of absence or presence. A newer state-changing claim makes the snapshot stale and the resource state unknown.
- An empty resource search no longer produces `COMPLETE`; source coverage and identity resolution are checked separately.
- Historical identifiers are gathered only from the resolved user's own temporal edges, preventing traversal through a recycled identifier to another user.
- PDF and image evidence is read with local OCR and interpreted by the configured text model; extracted records are cached by source hash, prompt, OCR engine version, and model.
- Output rows are validated independently for schema, allowed operations, phase order, step numbering, and evidence references before writing.

## Run conditions

- Intent classification used local deterministic rules; no Gemini intent calls were made.
- The supplied SQLite index had incomplete evidence-source coverage during this run.

## Limitations

- Case-level and step-level metrics against hidden canonical answers require the organizer's reference labels and are not inferred from workflow status claims.
- Costs are reported only when token usage and model rates are available from the run configuration.
