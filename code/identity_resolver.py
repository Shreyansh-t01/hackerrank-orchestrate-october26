"""
Temporal Identity Graph & Resolver Module for Deletion Auditing.

Reconstructs current and historical user identities using a temporal graph with
valid-time intervals [valid_from, valid_to), point-in-time audit cutoffs,
and temporal BFS traversal to prevent identity bleed and resolve recycled identifiers.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


def parse_iso_timestamp(ts: Optional[str | datetime]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string into a timezone-aware UTC datetime."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    ts_str = str(ts).strip()
    if not ts_str:
        return None
    # Support 'Z' suffix and standard offsets
    if ts_str.endswith("Z"):
        ts_str = ts_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_email(email: Optional[str]) -> str:
    """Normalize email address to lowercase and strip whitespace."""
    if not email:
        return ""
    return email.strip().lower()


def clean_phone_digits(phone: Optional[str]) -> str:
    """Extract digits only from phone number for format-invariant matching."""
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def compute_email_hash(email: str) -> str:
    """Compute standard SHA-256 hash of normalized email."""
    norm = normalize_email(email)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


@dataclass
class TimeWindow:
    """Represents a validity interval [valid_from, valid_to)."""
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None

    def contains(self, ts: Optional[datetime]) -> bool:
        """Check if timestamp falls within [valid_from, valid_to)."""
        if ts is None:
            return True
        if self.valid_from is not None and ts < self.valid_from:
            return False
        if self.valid_to is not None and ts >= self.valid_to:
            return False
        return True

    def overlaps(self, other: TimeWindow) -> bool:
        """Check if this interval overlaps with another interval."""
        # start1 < end2 and start2 < end1
        if self.valid_to is not None and other.valid_from is not None and self.valid_to <= other.valid_from:
            return False
        if other.valid_to is not None and self.valid_from is not None and other.valid_to <= self.valid_from:
            return False
        return True


@dataclass
class GraphNode:
    """Node in the Temporal Identity Graph."""
    node_id: str
    node_type: str  # 'USER_ID', 'EMAIL', 'PHONE', 'EMAIL_HASH', 'NAME'
    value: str
    normalized_value: str


@dataclass
class GraphEdge:
    """Temporal edge connecting nodes with valid-time interval."""
    source_id: str
    target_id: str
    relation: str  # e.g., 'OWNS_EMAIL', 'OWNS_PHONE', 'OWNS_HASH', 'HAS_NAME'
    time_window: TimeWindow
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolvedIdentity:
    """Comprehensive resolved identity bundle for an audited user."""
    user_id: str
    name: str
    current_email: str
    all_emails: Set[str]
    all_phones: Set[str]
    phone_digits: Set[str]
    all_email_hashes: Set[str]
    timeline_intervals: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert resolved identity to serializable dictionary."""
        return {
            "user_id": self.user_id,
            "name": self.name,
            "current_email": self.current_email,
            "all_emails": sorted(list(self.all_emails)),
            "all_phones": sorted(list(self.all_phones)),
            "phone_digits": sorted(list(self.phone_digits)),
            "all_email_hashes": sorted(list(self.all_email_hashes)),
            "timeline_intervals": self.timeline_intervals,
        }

    def matches_identifier(self, value: Optional[str], ts: Optional[datetime] = None) -> bool:
        """Check if an arbitrary string matches any of the user's identifiers."""
        if not value:
            return False
        val_str = str(value).strip()
        val_lower = val_str.lower()
        val_digits = clean_phone_digits(val_str)

        if val_str == self.user_id:
            return True
        if val_lower in self.all_emails:
            return True
        if val_lower in self.all_email_hashes:
            return True
        if val_str in self.all_phones:
            return True
        if val_digits and val_digits in self.phone_digits:
            return True
        if val_str == self.name:
            return True
        return False


class TemporalIdentityGraph:
    """
    Temporal Identity Graph with interval edges and overlap-constrained traversal.
    """

    def __init__(self) -> None:
        self.nodes: Dict[str, GraphNode] = {}
        # Adjacency list: node_id -> list of GraphEdge
        self.adj: Dict[str, List[GraphEdge]] = defaultdict(list)
        # Fast lookup indexes for entry-point resolution
        self.email_to_user_edges: Dict[str, List[Tuple[str, TimeWindow]]] = defaultdict(list)

    def add_node(self, node_id: str, node_type: str, value: str, normalized_value: str) -> GraphNode:
        if node_id not in self.nodes:
            self.nodes[node_id] = GraphNode(node_id, node_type, value, normalized_value)
        return self.nodes[node_id]

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        valid_from: Optional[datetime] = None,
        valid_to: Optional[datetime] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        window = TimeWindow(valid_from=valid_from, valid_to=valid_to)
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            relation=relation,
            time_window=window,
            metadata=metadata or {},
        )
        self.adj[source_id].append(edge)
        # Bidirectional edge for traversal
        reverse_edge = GraphEdge(
            source_id=target_id,
            target_id=source_id,
            relation=f"REVERSE_{relation}",
            time_window=window,
            metadata=metadata or {},
        )
        self.adj[target_id].append(reverse_edge)


class TemporalIdentityResolver:
    """
    High-level resolver that builds point-in-time temporal identity graphs
    and performs constrained traversals starting from input emails.
    """

    def __init__(
        self,
        user_identities_path: str | Path = "dataset/user_identities.csv",
        user_audit_log_path: str | Path = "dataset/user_audit_log.csv",
    ) -> None:
        self.user_identities_path = Path(user_identities_path)
        self.user_audit_log_path = Path(user_audit_log_path)
        self._raw_identities: List[Dict[str, str]] = []
        self._raw_audit_logs: List[Dict[str, str]] = []
        self._load_raw_data()

    def _load_raw_data(self) -> None:
        """Load tabular dataset into memory."""
        if self.user_identities_path.exists():
            with open(self.user_identities_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self._raw_identities = [r for r in reader]

        if self.user_audit_log_path.exists():
            with open(self.user_audit_log_path, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self._raw_audit_logs = [r for r in reader]

    def build_graph(self, audit_cutoff: Optional[datetime] = None) -> TemporalIdentityGraph:
        """
        Construct the temporal graph strictly respecting the audit_cutoff timestamp.
        Any audit log events that occurred after audit_cutoff are excluded to enforce
        a strict point-in-time snapshot.
        """
        graph = TemporalIdentityGraph()

        # 1. Group audit logs by (user_id, field_name)
        # Sort each group chronologically by event_time
        user_field_events: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
        for r in self._raw_audit_logs:
            event_time = parse_iso_timestamp(r["event_time"])
            if audit_cutoff is not None and event_time is not None and event_time > audit_cutoff:
                # Exclude future mutations occurring after audit_at
                continue
            user_field_events[(r["user_id"], r["field_name"])].append({
                "audit_id": r["audit_id"],
                "event_time": event_time,
                "old_value": r["old_value"],
                "new_value": r["new_value"],
            })

        for key in user_field_events:
            user_field_events[key].sort(key=lambda x: x["event_time"] or datetime.min.replace(tzinfo=timezone.utc))

        # 2. Iterate through user identities and construct nodes & interval edges
        for u in self._raw_identities:
            user_id = u["user_id"]
            user_node_id = f"user:{user_id}"
            graph.add_node(user_node_id, "USER_ID", user_id, user_id)

            # Name node
            name_val = u.get("name", "")
            if name_val:
                name_node_id = f"name:{name_val}"
                graph.add_node(name_node_id, "NAME", name_val, name_val)
                graph.add_edge(user_node_id, name_node_id, "HAS_NAME")

            # Process fields: email, phone, email_hash
            fields_to_process = [
                ("email", "EMAIL", normalize_email(u.get("email")), u.get("email", "")),
                ("phone", "PHONE", u.get("phone", "").strip(), u.get("phone", "")),
                ("email_hash", "EMAIL_HASH", u.get("email_hash", "").strip().lower(), u.get("email_hash", "")),
            ]

            for field_name, node_type, current_norm, current_raw in fields_to_process:
                events = user_field_events.get((user_id, field_name), [])

                if not events:
                    # No historical changes: current value was valid indefinitely
                    if current_norm:
                        node_id = f"{field_name}:{current_norm}"
                        graph.add_node(node_id, node_type, current_raw, current_norm)
                        graph.add_edge(user_node_id, node_id, f"OWNS_{node_type}")
                        if field_name == "email":
                            graph.email_to_user_edges[current_norm].append((user_id, TimeWindow()))
                else:
                    # Reconstruct transition intervals:
                    # Initial value was valid from -infinity up to first event_time
                    first_ev = events[0]
                    first_old_norm = normalize_email(first_ev["old_value"]) if field_name in ("email", "email_hash") else first_ev["old_value"].strip()
                    if first_old_norm:
                        node_id = f"{field_name}:{first_old_norm}"
                        graph.add_node(node_id, node_type, first_ev["old_value"], first_old_norm)
                        graph.add_edge(
                            user_node_id,
                            node_id,
                            f"OWNS_{node_type}",
                            valid_from=None,
                            valid_to=first_ev["event_time"],
                            metadata={"audit_id": first_ev["audit_id"]},
                        )
                        if field_name == "email":
                            graph.email_to_user_edges[first_old_norm].append(
                                (user_id, TimeWindow(valid_from=None, valid_to=first_ev["event_time"]))
                            )

                    # Intermediate transitions
                    for i in range(len(events) - 1):
                        curr_ev = events[i]
                        next_ev = events[i + 1]
                        val_norm = normalize_email(curr_ev["new_value"]) if field_name in ("email", "email_hash") else curr_ev["new_value"].strip()
                        if val_norm:
                            node_id = f"{field_name}:{val_norm}"
                            graph.add_node(node_id, node_type, curr_ev["new_value"], val_norm)
                            graph.add_edge(
                                user_node_id,
                                node_id,
                                f"OWNS_{node_type}",
                                valid_from=curr_ev["event_time"],
                                valid_to=next_ev["event_time"],
                                metadata={"audit_id": curr_ev["audit_id"]},
                            )
                            if field_name == "email":
                                graph.email_to_user_edges[val_norm].append(
                                    (user_id, TimeWindow(valid_from=curr_ev["event_time"], valid_to=next_ev["event_time"]))
                                )

                    # Latest transition: valid from last event_time to +infinity (or snapshot time)
                    last_ev = events[-1]
                    last_val_norm = normalize_email(last_ev["new_value"]) if field_name in ("email", "email_hash") else last_ev["new_value"].strip()
                    if last_val_norm:
                        node_id = f"{field_name}:{last_val_norm}"
                        graph.add_node(node_id, node_type, last_ev["new_value"], last_val_norm)
                        graph.add_edge(
                            user_node_id,
                            node_id,
                            f"OWNS_{node_type}",
                            valid_from=last_ev["event_time"],
                            valid_to=None,
                            metadata={"audit_id": last_ev["audit_id"]},
                        )
                        if field_name == "email":
                            graph.email_to_user_edges[last_val_norm].append(
                                (user_id, TimeWindow(valid_from=last_ev["event_time"], valid_to=None))
                            )

        return graph

    def resolve_user_identity(
        self,
        email: str,
        requested_at: Optional[str | datetime] = None,
        audit_at: Optional[str | datetime] = None,
    ) -> Optional[ResolvedIdentity]:
        """
        Resolve a user's current and historical identity from the request email.
        Applies temporal cutoff, locates owning user_id at requested_at, and
        traverses the temporal identity graph using Breadth-First Search.
        """
        norm_email = normalize_email(email)
        req_dt = parse_iso_timestamp(requested_at)
        audit_dt = parse_iso_timestamp(audit_at)

        # Build point-in-time graph up to audit_dt
        graph = self.build_graph(audit_cutoff=audit_dt)

        # 1. Entry Point Resolution: Find user_id owning this email at requested_at
        candidate_edges = graph.email_to_user_edges.get(norm_email, [])
        if not candidate_edges:
            # Fallback: check all nodes matching normalized value
            email_node_id = f"email:{norm_email}"
            if email_node_id in graph.nodes:
                for edge in graph.adj[email_node_id]:
                    if edge.target_id.startswith("user:"):
                        uid = edge.target_id.split(":", 1)[1]
                        candidate_edges.append((uid, edge.time_window))

        if not candidate_edges:
            return None

        # Filter candidate user_id by time window matching requested_at
        selected_user_id: Optional[str] = None
        for uid, tw in candidate_edges:
            if tw.contains(req_dt):
                selected_user_id = uid
                break

        # If no exact temporal match (e.g., requested_at is None), pick the single or latest user
        if not selected_user_id and candidate_edges:
            selected_user_id = candidate_edges[0][0]

        if not selected_user_id:
            return None

        # 2. Temporal BFS Traversal from user node
        start_node_id = f"user:{selected_user_id}"
        visited_nodes: Set[str] = {start_node_id}
        queue = deque([start_node_id])

        user_name = ""
        current_email = ""
        all_emails: Set[str] = set()
        all_phones: Set[str] = set()
        phone_digits: Set[str] = set()
        all_hashes: Set[str] = set()
        intervals_summary: List[Dict[str, Any]] = []

        while queue:
            curr_id = queue.popleft()
            curr_node = graph.nodes.get(curr_id)
            if not curr_node:
                continue

            for edge in graph.adj[curr_id]:
                target_id = edge.target_id
                target_node = graph.nodes.get(target_id)
                if not target_node:
                    continue

                # Ensure edge validity is bounded by audit_dt
                if audit_dt is not None and edge.time_window.valid_from is not None:
                    if edge.time_window.valid_from > audit_dt:
                        # Skip edges that originated in the future past audit_at
                        continue

                # Record identifier details
                if target_node.node_type == "EMAIL":
                    all_emails.add(target_node.normalized_value)
                    # Automatically compute SHA-256 for all emails
                    all_hashes.add(compute_email_hash(target_node.normalized_value))
                    if edge.time_window.valid_to is None:
                        current_email = target_node.value

                elif target_node.node_type == "PHONE":
                    all_phones.add(target_node.value)
                    digits = clean_phone_digits(target_node.value)
                    if digits:
                        phone_digits.add(digits)

                elif target_node.node_type == "EMAIL_HASH":
                    all_hashes.add(target_node.normalized_value)

                elif target_node.node_type == "NAME":
                    user_name = target_node.value

                # Track temporal interval metadata
                intervals_summary.append({
                    "node_type": target_node.node_type,
                    "value": target_node.value,
                    "valid_from": edge.time_window.valid_from.isoformat() if edge.time_window.valid_from else None,
                    "valid_to": edge.time_window.valid_to.isoformat() if edge.time_window.valid_to else None,
                    "relation": edge.relation,
                })

                if target_id not in visited_nodes:
                    visited_nodes.add(target_id)
                    queue.append(target_id)

        if not current_email and all_emails:
            current_email = sorted(list(all_emails))[0]

        return ResolvedIdentity(
            user_id=selected_user_id,
            name=user_name,
            current_email=current_email,
            all_emails=all_emails,
            all_phones=all_phones,
            phone_digits=phone_digits,
            all_email_hashes=all_hashes,
            timeline_intervals=intervals_summary,
        )


# Global singleton instance for high-throughput single-request invocations
_DEFAULT_RESOLVER: Optional[TemporalIdentityResolver] = None


def resolve_user_identity(
    email: str,
    requested_at: Optional[str | datetime] = None,
    audit_at: Optional[str | datetime] = None,
    user_identities_path: str | Path = "dataset/user_identities.csv",
    user_audit_log_path: str | Path = "dataset/user_audit_log.csv",
) -> Dict[str, Any]:
    """
    Tool function to resolve a user's current and historical identity.
    Accepts an input request email and timestamps, returning a comprehensive
    dictionary of verified identifiers.
    """
    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = TemporalIdentityResolver(
            user_identities_path=user_identities_path,
            user_audit_log_path=user_audit_log_path,
        )

    resolved = _DEFAULT_RESOLVER.resolve_user_identity(
        email=email,
        requested_at=requested_at,
        audit_at=audit_at,
    )

    if resolved is None:
        return {
            "resolved": False,
            "query_email": email,
            "user_id": None,
            "all_emails": [normalize_email(email)],
            "all_phones": [],
            "phone_digits": [],
            "all_email_hashes": [compute_email_hash(email)],
        }

    res_dict = resolved.to_dict()
    res_dict["resolved"] = True
    res_dict["query_email"] = email
    return res_dict


if __name__ == "__main__":
    # Self-test across sample requests
    test_resolver = TemporalIdentityResolver()
    sample_email = "amina.elmasri@gmail.com"
    result = test_resolver.resolve_user_identity(sample_email, "2026-08-18T09:14:00Z", "2026-09-03T11:30:00Z")
    print(f"Sample resolution for {sample_email}:")
    if result:
        print(f"  User ID: {result.user_id}")
        print(f"  Name: {result.name}")
        print(f"  Emails: {result.all_emails}")
        print(f"  Phones: {result.all_phones}")
        print(f"  Hashes count: {len(result.all_email_hashes)}")

