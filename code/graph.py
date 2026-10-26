"""
graph.py — LangGraph workflow for the Ghost Data Still Exists deletion-audit agent.

Nodes (in order):
  1. load_request       — parse case fields from input row
  2. classify_intent    — LLM intent classification (deletion vs other)
  3. resolve_identity   — build temporal identity graph, gather all identifiers
  4. discover_resources — query SQLite index for all user resources
  5. evaluate_evidence  -- reconstruct state, apply policy, decide status
  6. build_steps        -- build findings + remediation + verification steps
  7. validate_output    -- schema check on steps
  8. write_output       -- return the completed row
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from langgraph.graph import StateGraph, END

from typing_extensions import TypedDict

from code.identity_resolver import resolve_user_identity
from code.intent_classifier import classify_intent_node
from code.evaluation_engine import evaluate_case, build_portfolio, get_deletion_rules
from code.step_builder import build_steps
from code.resource_discovery import get_source_coverage


DB_PATH = Path(__file__).parent.parent / "data" / "resource_index.db"
DATASET_DIR = Path(__file__).parent.parent / "dataset"


# ---------------------------------------------------------------------------
# Shared state schema
# ---------------------------------------------------------------------------

class AuditState(TypedDict, total=False):
    # Input fields (from deletion_requests.csv)
    case_id:         str
    email:           str
    requested_at:    str
    reported_status: str
    reported_at:     str
    audit_at:        str
    request_text:    str
    db_path:         str

    # Intent
    is_deletion_request: bool
    intent_category:     str
    intent_confidence:   float
    intent_reasoning:    str
    intent_source:       str

    # Identity
    user_id:         str
    all_identifiers: list[str]   # [email, email_norm, email_hash, phone, …]
    identity_intervals: dict
    identity_error:  str
    identity_bundle: dict

    # Resource discovery
    portfolio: list[dict]
    source_coverage: dict
    unresolved_questions: list[str]

    # Evaluation
    evaluations: list[Any]   # list[ResourceEvaluation]
    deletion_status: str     # COMPLETE | INCOMPLETE | NOT_APPLICABLE

    # Output
    steps_json: list[dict]
    output_row: dict         # {case_id, deletion_status, steps_json}
    validation_errors: list[str]


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

def load_request_node(state: AuditState) -> AuditState:
    """No-op: fields already populated by the caller (main.py populates from CSV)."""
    return state


def resolve_identity_node(state: AuditState) -> AuditState:
    """Resolve user_id and full identifier bundle from the case email."""
    email = state.get("email", "")
    audit_at = state.get("audit_at", "")
    try:
        identity = resolve_user_identity(
            email=email,
            requested_at=state.get("requested_at", ""),
            audit_at=audit_at,
            user_identities_path=DATASET_DIR / "user_identities.csv",
            user_audit_log_path=DATASET_DIR / "user_audit_log.csv",
        )
        identifiers = identity.get("all_identifiers") or [
            identity.get("user_id", ""),
            *identity.get("all_emails", []),
            *identity.get("all_phones", []),
            *identity.get("phone_digits", []),
            *identity.get("all_email_hashes", []),
            email,
        ]
        identifiers = list(dict.fromkeys(value for value in identifiers if value))
        intervals: dict[str, list[dict]] = {}
        for item in identity.get("identifiers", []):
            normalized = str(item.get("normalized_value") or item.get("value") or "").strip().lower()
            if normalized:
                intervals.setdefault(normalized, []).append({
                    "valid_from": item.get("valid_from"),
                    "valid_to": item.get("valid_to"),
                })
        return {
            **state,
            "user_id":         identity.get("user_id", ""),
            "all_identifiers": identifiers,
            "identity_intervals": intervals,
            "identity_bundle": identity,
            "identity_error":  "" if identity.get("resolved") else "Identity could not be uniquely resolved.",
        }
    except Exception as e:
        return {
            **state,
            "user_id":         "",
            "all_identifiers": [email],
            "identity_intervals": {},
            "identity_bundle": {"resolved": False},
            "identity_error":  str(e),
        }


def discover_resources_node(state: AuditState) -> AuditState:
    """Discover all resources belonging to the user in the evidence index."""
    if not state.get("is_deletion_request"):
        return {**state, "portfolio": []}

    db_path = state.get("db_path", str(DB_PATH))
    deletion_rules = get_deletion_rules(db_path)
    coverage = get_source_coverage(db_path, state.get("audit_at", ""))
    portfolio = build_portfolio(
        db_path=db_path,
        user_id=state.get("user_id", ""),
        all_identifiers=state.get("all_identifiers", []),
        audit_at=state.get("audit_at", ""),
        deletion_rules=deletion_rules,
        identifier_intervals=state.get("identity_intervals", {}),
    )
    offline_intent = os.environ.get("GEMINI_OFFLINE_INTENT", "").strip().lower() in {"1", "true", "yes"}
    unresolved_policy = [item for item in portfolio if item.get("required_action") == "UNKNOWN"]
    if offline_intent and unresolved_policy:
        # Keep the audit running without guessing the treatment for conditional
        # policies such as log retention periods that are missing in the index.
        portfolio = [item for item in portfolio if item.get("required_action") != "UNKNOWN"]
        coverage = dict(coverage)
        gaps = list(coverage.get("coverage_gaps", []))
        gaps.append(f"{len(unresolved_policy)} relevant resources have an unresolved policy condition")
        coverage["coverage_gaps"] = gaps
        coverage["is_complete"] = False
    from code.run_logging import record_event
    record_event(
        "resource_discovery", case_id=state.get("case_id", ""), stage="discovery",
        resource_count=len(portfolio), coverage_complete=bool(coverage.get("is_complete")),
    )
    return {**state, "portfolio": portfolio, "source_coverage": coverage}


def evaluate_evidence_node(state: AuditState) -> AuditState:
    """Evaluate each resource against deletion policy at audit_at."""
    deletion_status, evaluations = evaluate_case(
        db_path=state.get("db_path", str(DB_PATH)),
        user_id=state.get("user_id", ""),
        all_identifiers=state.get("all_identifiers", []),
        audit_at=state.get("audit_at", ""),
        is_deletion_request=state.get("is_deletion_request", False),
        portfolio=state.get("portfolio", []),
        source_coverage=state.get("source_coverage", {}),
        identity_resolved=bool(state.get("identity_bundle", {}).get("resolved")),
    )
    unresolved = list(state.get("unresolved_questions", []))
    if state.get("identity_error"):
        unresolved.append(state["identity_error"])
    coverage = state.get("source_coverage", {})
    if not coverage.get("is_complete"):
        gaps = coverage.get("coverage_gaps", [])
        unresolved.append("Evidence-source coverage is incomplete" + (f": {', '.join(gaps)}" if gaps else "."))
    if not state.get("portfolio"):
        unresolved.append("No matching resource was found; an empty search does not prove deletion.")
    for evaluation in evaluations:
        if getattr(evaluation, "notes", ""):
            unresolved.append(f"{evaluation.resource_id}: {evaluation.notes}")
    from code.run_logging import record_event
    record_event(
        "case_evaluated", case_id=state.get("case_id", ""),
        status=deletion_status, resource_count=len(evaluations),
        unresolved_count=len(unresolved),
    )
    return {
        **state,
        "evaluations":    evaluations,
        "deletion_status": deletion_status,
        "unresolved_questions": list(dict.fromkeys(unresolved)),
    }


def build_steps_node(state: AuditState) -> AuditState:
    """Build ordered finding/remediation/verification steps."""
    steps = build_steps(
        evaluations=state.get("evaluations", []),
        deletion_status=state.get("deletion_status", "NOT_APPLICABLE"),
    )
    return {**state, "steps_json": steps}


def validate_output_node(state: AuditState) -> AuditState:
    """Validate that steps_json conforms to the required schema."""
    import os
    from code.output_validation import validate_steps
    steps = state.get("steps_json", [])
    allow_incomplete_coverage = (
        os.environ.get("GEMINI_OFFLINE_INTENT", "").strip().lower() in {"1", "true", "yes"}
    )
    errors = validate_steps(
        state.get("deletion_status", "NOT_APPLICABLE"),
        steps,
        allow_incomplete_coverage=allow_incomplete_coverage,
    )
    return {**state, "validation_errors": errors}


def write_output_node(state: AuditState) -> AuditState:
    """Assemble the output row."""
    if state.get("validation_errors"):
        raise ValueError("Output validation failed: " + "; ".join(state["validation_errors"]))
    steps = state.get("steps_json", [])
    output_row = {
        "case_id":          state.get("case_id", ""),
        "deletion_status":  state.get("deletion_status", "NOT_APPLICABLE"),
        "steps_json":       json.dumps(steps, separators=(",", ":"), ensure_ascii=False),
    }
    return {**state, "output_row": output_row}


# ---------------------------------------------------------------------------
# Routing logic
# ---------------------------------------------------------------------------

def _route_after_intent(state: AuditState) -> str:
    """If not a deletion request, skip directly to build_steps."""
    if not state.get("is_deletion_request"):
        # Set status before routing away
        return "build_steps"
    return "resolve_identity"


# ---------------------------------------------------------------------------
# Graph construction (LangGraph)
# ---------------------------------------------------------------------------

def build_graph():
    """Build and compile the LangGraph audit workflow."""
    g = StateGraph(AuditState)

    # Add nodes
    g.add_node("load_request",       load_request_node)
    g.add_node("classify_intent",    classify_intent_node)
    g.add_node("resolve_identity",   resolve_identity_node)
    g.add_node("discover_resources", discover_resources_node)
    g.add_node("evaluate_evidence",  evaluate_evidence_node)
    g.add_node("build_steps",        build_steps_node)
    g.add_node("validate_output",    validate_output_node)
    g.add_node("write_output",       write_output_node)

    # Set entry point
    g.set_entry_point("load_request")

    # Edges
    g.add_edge("load_request",       "classify_intent")
    g.add_conditional_edges(
        "classify_intent",
        _route_after_intent,
        {
            "resolve_identity": "resolve_identity",
            "build_steps":      "build_steps",
        },
    )
    g.add_edge("resolve_identity",   "discover_resources")
    g.add_edge("discover_resources", "evaluate_evidence")
    g.add_edge("evaluate_evidence",  "build_steps")
    g.add_edge("build_steps",        "validate_output")
    g.add_edge("validate_output",    "write_output")
    g.add_edge("write_output",       END)

    return g.compile()


def run_audit(case_row: dict, db_path: str | Path | None = None) -> dict:
    """Run one case through the required LangGraph workflow."""
    app = build_graph()
    initial_state = dict(case_row)
    initial_state["db_path"] = str(db_path or DB_PATH)
    final_state = app.invoke(initial_state)
    
     
    output_row = final_state.get("output_row")
    if output_row is None:
        raise RuntimeError(f"LangGraph completed without an output row for {case_row.get('case_id', '')}.")
    return output_row

# Add this at the very bottom of the file
if __name__ == "__main__":
    # 1. Compile the graph
    app = build_graph() 
    
    # 2. Generate and save the PNG
    print("Generating graph image...")
    with open("agent_graph.png", "wb") as f:
        f.write(app.get_graph().draw_mermaid_png())
        
    print("Success! Saved as agent_graph.png")
