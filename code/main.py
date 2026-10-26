"""Command-line entry point for the deletion-audit workflow."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATASET_DIR = REPO_ROOT / "dataset"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "resource_index.db"


def _load_dotenv() -> None:
    """Read simple KEY=VALUE entries without requiring a dotenv dependency."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as source:
        return [{key.strip(): (value or "").strip() for key, value in row.items()} for row in csv.DictReader(source)]


def load_valid_evidence_ids() -> set[str]:
    """Collect citation IDs only from participant-facing manifest and audit files."""
    result: set[str] = set()
    for filename, field in (
        ("evidence_files.csv", "file_id"),
        ("system_activity.csv", "activity_id"),
        ("user_audit_log.csv", "audit_id"),
    ):
        path = DATASET_DIR / filename
        if path.exists():
            result.update(row[field] for row in load_csv(path) if row.get(field))
    return result


def write_output_csv(
    rows: list[dict],
    case_ids: list[str],
    output_path: Path,
    *,
    allow_unresolved_incomplete: bool = False,
) -> None:
    """Validate the complete output before writing it to the requested path."""
    from code.output_validation import validate_output_rows
    from code.run_logging import record_event

    ordered_rows = sorted(rows, key=lambda item: item["case_id"])
    errors = validate_output_rows(
        ordered_rows,
        sorted(case_ids),
        load_valid_evidence_ids(),
        allow_unresolved_incomplete=allow_unresolved_incomplete,
    )
    if errors:
        record_event("output_validation_failed", validation_error_count=len(errors), status="failed")
        preview = "; ".join(errors[:10])
        raise ValueError(f"Output validation failed ({len(errors)} errors): {preview}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary_path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.writer(target)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        for row in ordered_rows:
            writer.writerow([row["case_id"], row["deletion_status"], row["steps_json"]])
    temporary_path.replace(output_path)


def _write_reports(results: list[dict], results_meta: list[dict], reference_path: Path) -> None:
    from code.evaluation.reporting import write_evaluation_report, write_usage_report

    evaluation_dir = REPO_ROOT / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    if reference_path.exists():
        write_evaluation_report(
            results,
            load_csv(reference_path),
            evaluation_dir / "evaluation_report.md",
        )
    write_usage_report(results_meta, evaluation_dir / "usage_report.md")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit deletion requests using the local SQLite evidence index.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--sample", action="store_true", help="Process public sample requests only.")
    mode.add_argument("--case", metavar="CASE_ID", help="Process one case for debugging.")
    parser.add_argument("--rebuild-index", action="store_true", help="Rebuild the index from source files.")
    parser.add_argument("--index-only", action="store_true", help="Build/check the evidence index and exit without auditing cases.")
    parser.add_argument("--skip-index", action="store_true", help="Use an existing SQLite index without parsing evidence sources.")
    parser.add_argument("--offline-intent", action="store_true", help="Use local intent rules and make no Gemini intent calls.")
    parser.add_argument("--batch-size", type=int, default=50, help="Cases per processing batch (default: 50).")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="SQLite index path.")
    parser.add_argument("--output", type=Path, help="Override the output CSV path.")
    return parser.parse_args()


def main() -> None:
    _load_dotenv()
    args = _parse_args()
    if args.batch_size < 1:
        raise ValueError("--batch-size must be at least 1")
    if args.offline_intent:
        os.environ["GEMINI_OFFLINE_INTENT"] = "1"

    # Include PDF/image text-interpretation calls in a full run's usage report.
    from code.llm.usage import USAGE_TRACKER
    USAGE_TRACKER.reset()
    if args.skip_index:
        if not args.db.exists():
            raise FileNotFoundError(f"SQLite index does not exist: {args.db}")
        from code.resource_discovery import get_source_coverage
        coverage = get_source_coverage(args.db)
        os.environ["AUDIT_INDEX_COVERAGE_COMPLETE"] = "1" if coverage.get("is_complete") else "0"
        print(
            "Using existing index without ingestion: "
            f"complete={coverage.get('is_complete')}, "
            f"sources={coverage.get('source_files')}, "
            f"errors={coverage.get('ingestion_errors')}"
        )
    else:
        from code.ingestion import build_index

        # Apply schema updates and verify source hashes on every run. Unchanged
        # evidence and OCR/text extraction results are reused from their caches.
        build_index(db_path=args.db, force_rebuild=args.rebuild_index)

    if args.index_only:
        from code.resource_discovery import get_source_coverage
        coverage = get_source_coverage(args.db)
        print(
            "Index-only coverage: "
            f"complete={coverage.get('is_complete')}, "
            f"sources={coverage.get('source_files')}, "
            f"errors={coverage.get('ingestion_errors')}, "
            f"unclassified={len(coverage.get('unclassified_categories', []))}"
        )
        if not coverage.get("is_complete"):
            raise RuntimeError("Index-only build is incomplete; no cases were processed.")
        from code.llm.usage import USAGE_TRACKER
        print(f"Document/index model usage: {USAGE_TRACKER.get_summary()}")
        return

    if args.sample:
        request_path = DATASET_DIR / "sample_deletion_requests.csv"
        output_path = args.output or (REPO_ROOT / "output.sample.csv")
    elif args.case:
        request_path = DATASET_DIR / "deletion_requests.csv"
        output_path = args.output or (REPO_ROOT / f"output.{args.case}.csv")
    else:
        request_path = DATASET_DIR / "deletion_requests.csv"
        output_path = args.output or (REPO_ROOT / "output.csv")

    cases = load_csv(request_path)
    if args.case:
        cases = [case for case in cases if case.get("case_id") == args.case]
        if not cases:
            raise ValueError(f"Case {args.case!r} was not found in {request_path}")

    from code.graph import run_audit
    from code.run_logging import record_event

    rows: list[dict] = []
    results_meta: list[dict] = []
    failures: list[str] = []
    started = time.monotonic()

    for batch_start in range(0, len(cases), args.batch_size):
        batch = cases[batch_start:batch_start + args.batch_size]
        batch_number = batch_start // args.batch_size + 1
        batch_count = (len(cases) + args.batch_size - 1) // args.batch_size
        print(f"Starting batch {batch_number}/{batch_count} ({len(batch)} cases)")
        for offset, case in enumerate(batch):
            index = batch_start + offset + 1
            case_id = case.get("case_id", f"row_{index}")
            record_event("case_started", case_id=case_id, stage="audit")
            try:
                result = run_audit(case, db_path=args.db)
                rows.append(result)
                results_meta.append({"case_id": case_id, "status": "ok"})
                try:
                    step_count = len(json.loads(result.get("steps_json", "[]")))
                except (TypeError, json.JSONDecodeError):
                    step_count = 0
                record_event(
                    "case_finished", case_id=case_id, status=result.get("deletion_status", ""),
                    step_count=step_count,
                )
            except Exception as exc:
                failures.append(f"{case_id}:{type(exc).__name__}")
                results_meta.append({"case_id": case_id, "status": "error", "error_type": type(exc).__name__})
                record_event("case_failed", case_id=case_id, status="ERROR", error_type=type(exc).__name__)
        elapsed = time.monotonic() - started
        processed = min(batch_start + args.batch_size, len(cases))
        print(f"Processed {processed}/{len(cases)} cases in {elapsed:.1f}s")

    if failures:
        record_event("run_failed", status="error", failed_case_count=len(failures))
        preview = ", ".join(failures[:10])
        raise RuntimeError(
            f"Audit run failed for {len(failures)} case(s); no output was written. "
            f"First failures: {preview}"
        )

    expected_case_ids = [case["case_id"] for case in cases]
    write_output_csv(
        rows,
        expected_case_ids,
        output_path,
        allow_unresolved_incomplete=args.offline_intent and args.skip_index,
    )
    # Keep the submission reports tied to the full-dataset run. Debug modes
    # must not replace those reports with partial-run metrics or token totals.
    if not args.sample and not args.case:
        _write_reports(rows, results_meta, DATASET_DIR / "sample_output.csv")
    record_event(
        "run_finished", status="complete", record_count=len(rows),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    print(f"Completed {len(rows)} cases in {time.monotonic() - started:.1f}s")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
