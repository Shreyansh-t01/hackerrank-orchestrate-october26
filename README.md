# HackerRank Orchestrate - October 2026

## Ghost Data Still Exists

Build an AI agent that audits whether a user's personal data was actually deleted.

A deletion workflow may report **Complete** while data still exists in databases, logs, support attachments, exports, search indexes, archives, or backups. Sync and restore processes may also bring deleted data back.

For each request, the agent must first determine whether deletion was actually requested. It then returns `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`, reports any remaining data or recreation risk, and produces the ordered remediation and verification steps required to finish an authorized deletion safely.

Read [`problem_statement.md`](./problem_statement.md) for the complete challenge contract.

---

## Quick Start

Clone the repository and move into the project directory:

```bash
git clone https://github.com/interviewstreet/hackerrank-orchestrate-october26.git
cd hackerrank-orchestrate-october26
```

Start your AI coding tool from the **repository root** so it automatically reads [`AGENTS.md`](./AGENTS.md) and logs your session turns to `log.txt` in the root directory.

The implementation is organized into small modules under `code/`. Read [`IMPLEMENTATION_GUIDE.md`](./IMPLEMENTATION_GUIDE.md) for a plain-language walkthrough of how the parts connect.

Install the Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Create a local environment file from the example and add your API key:

```powershell
Copy-Item .env.example .env
```

Install the Tesseract OCR executable separately and place it on `PATH`; set optional `TESSERACT_CMD` in `.env` if it is installed elsewhere. PyMuPDF reads selectable PDF text directly. Tesseract reads scanned PDF pages and images locally. Gemini is used only afterward to interpret extracted text into evidence records; it never receives image bytes or runs just to extract text. Set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) for intent classification and semantic document interpretation. `GEMINI_DOCUMENT_TEXT_TIMEOUT_SECONDS` defaults to 60 and `GEMINI_DOCUMENT_MAX_RETRIES` defaults to 2 additional retries. `GEMINI_INTENT_MAX_RETRIES` controls intent classification. `.env` is ignored by Git; keep credentials out of `.env.example`.

LLM transport and settings live under `code/llm/`. Feature prompts live with the feature that uses them: intent classification owns its prompt in `code/intent_classifier.py`, and document evidence interpretation owns its prompt in `code/evidence_parsers/document_text.py`. Deterministic extraction is a separate function in that parser; text-model calls are usage-tracked by the shared LLM transport.

Evidence collection names and service-export tables are normalized in `code/data_catalog.py` to the categories in `deletion_rules.csv`. Unmapped categories count as an evidence-coverage gap and cannot silently be treated as a completed parse.

Your solution must:

- Read the input files from `dataset/`
- Generate one prediction row for every request in `dataset/deletion_requests.csv`
- Write the final predictions to `output.csv` in the repository root

Run the full audit from the repository root with:

```bash
python3 code/main.py
```

The first run builds the local SQLite index. Unchanged PDFs and images reuse cached text interpretations in `data/document_text_cache/`. Each interpreted record retains its evidence ID, source filename link, capture timestamp, and page number. `--rebuild-index --index-only --db data/resource_index_candidate.db` rebuilds and checks the index without auditing cases or writing `output.csv`. `--sample` and `--case case_07` write separate debug CSV files and do not overwrite the final `output.csv`.

Operational run events are appended to `data/audit_runtime.jsonl`; the logger keeps only allow-listed metadata and does not store request contents or personal identifiers.

The blank template at `dataset/output.csv` is provided as a reference. Your final generated file for submission must be the root-level `output.csv`.

## Tests

Unit tests use Python's standard `unittest` runner:

```bash
python -m unittest discover -s tests
```

The unit tests are in `tests/` and can be run with the command above.


## Dataset

All participant-facing files are in `dataset/`:

| File | Contents |
| --- | --- |
| `deletion_requests.csv` | 518 requests |
| `sample_deletion_requests.csv` | 32 sample requests |
| `sample_output.csv` | Correct sample answers |
| `user_identities.csv` | Current user identities |
| `user_audit_log.csv` | Historical identity changes |
| `system_activity.csv` | Activity across services and data processes |
| `deletion_rules.csv` | Required treatment for each data category |
| `evidence_files.csv` | Manifest of supplied evidence files |
| `output.csv` | Blank output template |

Evidence is stored under `dataset/evidence/` and may include PDFs, images, logs, JSON, plain text, or SQL snapshot exports. SQL captures may contain a primary database, regional replicas, a reporting replica, or a failover replica so replication drift can be investigated directly. The `.sql` files are readable as text and executable with SQLite.

Only `deletion_requests.csv` requires answers. The other files provide evidence and context.

---

## Output

Create `output.csv` with these columns in this exact order:

```text
case_id,deletion_status,steps_json
```

Produce exactly one row per request. `deletion_status` is `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`. `steps_json` is an ordered JSON array containing findings, remediation actions, and verification checks. Use an empty array (`[]`) for `NOT_APPLICABLE`. Evidence references may be a `file_id`, `activity_id`, or `audit_id`. Determine the resource state at `audit_at` from the available evidence; reported workflow statuses and operation results are claims. Use `dataset/sample_output.csv` as the format reference.

The final file must contain 518 data rows sorted by `case_id` (plus the header row). The grader parses `steps_json` as JSON, so object-key order and whitespace do not matter; array order does. Order findings, remediation, and verification in that phase order. Within a phase, follow the operation order in the problem statement, then sort by `system_id` and `resource_id`.

---

## Evaluation

`output.csv` will be compared with hidden canonical answers. Scoring considers:

- deletion assessment accuracy;
- current and historical identity resolution;
- discovery of remaining data and recreation risks;
- correct policy application and evidence references; and
- completeness and ordering of remediation and verification steps.

Your `code.zip` must include:

```text
evaluation/evaluation_report.md
evaluation/usage_report.md
```

- `evaluation_report.md` must cover case, row, field, and sequence accuracy, at least three additional evaluation cases created by you, failures found, and improvements made.
- `usage_report.md` must cover the final full-dataset run, including model providers and names, model calls, input and output tokens, total and average tokens per request, and estimated total and per-request cost. Include per-model and overall totals when using multiple models.

**Important:** Place `evaluation_report.md` and `usage_report.md` in the repository-root `evaluation/` folder. The runner writes them after a successful full-dataset run. Keep prompts in source code or Markdown files. Automated code evaluation ignores raw data files (`.csv`, `.tsv`, `.json`, `.jsonl`, `.sql`, `.log`), so content stored only in those formats will not be visible to the code judge or AI interviewer.

---

## Chat Transcript

This repository includes [`AGENTS.md`](./AGENTS.md), which instructs compatible AI coding tools to append conversation summaries to `log.txt` in the repository root:

| Platform | Path |
| --- | --- |
| macOS / Linux | `<repo root>/log.txt` |
| Windows | `<repo root>\log.txt` |

- Always launch your AI coding agent from the repository root (or explicitly ask it to read `./AGENTS.md` first) so every turn is logged to `log.txt` with the required `User Prompt`, `Agent Response Summary`, `Actions`, and `Context` sections.
- Upload `log.txt` directly as your `chat_transcript` submission on HackerRank.
- Do not include API keys, tokens, credentials, or secrets in prompts, code, configuration, or `log.txt`.

---

## Packaging `code.zip` and Submission

| Item | Description |
| --- | --- |
| `code.zip` | Runnable solution, prompts, configuration, `README.md`, and `evaluation/` reports |
| `output.csv` | One answer row for each of the 518 requests in `dataset/deletion_requests.csv` |
| `chat_transcript` | The root `log.txt` file generated by your coding agent |
| AI interview | Live explanation of your architecture, approach, and evaluation |

You may use any programming language, model, framework, or retrieval approach. Include clear setup and execution instructions in `README.md` inside `code.zip`.

To package `code.zip` from the repository root without bundling the dataset:

**macOS / Linux:**

```bash
zip -r code.zip README.md IMPLEMENTATION_GUIDE.md .env.example requirements.txt code tests evaluation -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

**Windows (PowerShell):**

```powershell
Compress-Archive -Path README.md, IMPLEMENTATION_GUIDE.md, .env.example, requirements.txt, code, tests, evaluation -DestinationPath code.zip -Force
```

When packaging `code.zip`:

- Keep your evaluation reports in the repository-root `evaluation/` folder so they are included in `code.zip`.
- Do **not** include `dataset/`, virtual environments (`.venv`, `venv`, `node_modules`), or build caches (`__pycache__`). On Windows, keep `.venv` at the repository root (outside `code/`) and remove any `code/__pycache__` folders before running `Compress-Archive`.
- Do **not** include nested archives (`.zip`, `.tar`, `.gz`, `.tgz`, `.7z`, `.rar`) inside `code.zip` — submissions containing nested archive files will be rejected.

Before submitting, confirm that:

- `output.csv` has the exact header `case_id,deletion_status,steps_json` and contains all 518 data rows sorted by `case_id`;
- every `steps_json` value is a valid JSON array with consecutive step numbers starting at `1`;
- findings cite valid evidence IDs, and `COMPLETE` verification steps cite evidence proving the current state;
- proposed remediation and future verification steps use an empty `evidence_id` (`""`);
- `code.zip` includes your runnable code, `README.md`, `evaluation/evaluation_report.md`, and `evaluation/usage_report.md`;
- `log.txt` follows the `AGENTS.md` turn format and is uploaded to the `chat_transcript` slot; and
- no API keys, credentials, real personal data, or sensitive configuration values are included.
