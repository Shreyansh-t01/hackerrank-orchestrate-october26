# Usage Report — Ghost Data Still Exists

Generated: 2026-10-25T13:18:40.340890+00:00

## Run summary

- Provider: Google Gemini API
- Cases processed: 518
- Model calls: 0
- Failed model calls: 0
- Input tokens: 0
- Output tokens: 0
- Total tokens: 0
- Average tokens per case: 0.0

## Per-model totals

- **gemini-3.8-flash:** 0 calls, 0 failures, 0 input tokens, 0 output tokens, estimated cost not calculated (model rates are not configured)

## Estimated cost

Not calculated for every used model. Set model-specific `GEMINI_PRICE_<MODEL>_INPUT_USD_PER_MILLION` and `GEMINI_PRICE_<MODEL>_OUTPUT_USD_PER_MILLION` values (or shared fallback rates); no price was assumed.

## Notes

- PDFs and images use local OCR followed by the configured text model during index construction. Cached extraction call and token totals are restored into later full-run reports, including when the SQLite index is rebuilt separately.
- Token counts come from Gemini `usageMetadata` when the API returns it. Calls without usage metadata cannot be assigned token totals.
- Intent classification may use deterministic fallback rules when no model call is available.
- Intent classification used local deterministic rules in offline mode; no intent model calls were made.
- The existing SQLite index had incomplete evidence coverage; no PDF or image indexing was performed during this run.
