"""
Verifier claims drill-down and admin override (handoff §4.1).

Reads per-session verification.jsonl, merges admin overrides from
verification_overrides.jsonl, and appends feedback to a global
truthseeker_feedback.jsonl for Truthseeker tuning review.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VALID_OVERRIDE_STATUSES = frozenset({
    "VERIFIED",
    "PARTIALLY_VERIFIED",
    "UNSUPPORTED",
    "CONTRADICTED",
    "NEEDS_HUMAN_REVIEW",
    "NOT_WEB_VERIFIABLE",
    "DOCUMENT_GROUNDED_NOT_WEB_VERIFIED",
})

_OVERRIDES_FILENAME = "verification_overrides.jsonl"
_FEEDBACK_FILENAME = "truthseeker_feedback.jsonl"


def claim_id_for_record(record: Dict[str, Any]) -> str:
    """Stable id for a verification record (turn + agent + claim text)."""
    key = (
        f"{record.get('source_turn', '')}|"
        f"{record.get('source_agent', '')}|"
        f"{record.get('claim', '')}"
    )
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def load_overrides(session_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Latest override per claim_id (last write wins)."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in _read_jsonl(session_dir / _OVERRIDES_FILENAME):
        cid = row.get("claim_id")
        if cid:
            by_id[str(cid)] = row
    return by_id


def load_claims(session_dir: Path) -> List[Dict[str, Any]]:
    """Load verification records with claim_id and effective_status."""
    raw = _read_jsonl(session_dir / "verification.jsonl")
    overrides = load_overrides(session_dir)
    claims: List[Dict[str, Any]] = []
    for record in raw:
        cid = claim_id_for_record(record)
        original = record.get("status", "UNKNOWN")
        override = overrides.get(cid)
        effective = override["status"] if override else original
        claims.append({
            "claim_id":         cid,
            "claim":            record.get("claim", ""),
            "category":         record.get("category"),
            "original_status":  original,
            "effective_status": effective,
            "confidence":       record.get("confidence"),
            "source_count":     record.get("source_count", 0),
            "highest_source_tier":  record.get("highest_source_tier"),
            "highest_source_score": record.get("highest_source_score", 0),
            "sources":          record.get("sources") or [],
            "source_turn":      record.get("source_turn"),
            "source_agent":     record.get("source_agent"),
            "verified_at":      record.get("verified_at") or record.get("ts"),
            "note":             record.get("note"),
            "context_id":       record.get("context_id"),
            "source_file":      record.get("source_file"),
            "override":         _public_override(override) if override else None,
        })
    return claims


def _public_override(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status":   row.get("status"),
        "reason":   row.get("reason"),
        "by":       row.get("by"),
        "at":       row.get("at"),
        "feedback": row.get("feedback"),
    }


def summarize_claims(claims: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for c in claims:
        status = c.get("effective_status") or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1
    return {
        "total":        len(claims),
        "status_counts": counts,
        "overridden":   sum(1 for c in claims if c.get("override")),
    }


def find_claim_by_id(session_dir: Path, claim_id: str) -> Optional[Dict[str, Any]]:
    for record in _read_jsonl(session_dir / "verification.jsonl"):
        if claim_id_for_record(record) == claim_id:
            return record
    return None


def save_override(
    *,
    session_dir: Path,
    session_id: str,
    feedback_dir: Path,
    claim_id: str,
    admin_username: str,
    status: str,
    reason: str,
    feedback: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Record an admin override for a claim. Raises ValueError on bad input.
    Returns the public override dict.
    """
    status = (status or "").strip().upper()
    reason = (reason or "").strip()
    if status not in VALID_OVERRIDE_STATUSES:
        raise ValueError(f"invalid status: {status}")
    if not reason:
        raise ValueError("reason is required")

    original = find_claim_by_id(session_dir, claim_id)
    if original is None:
        raise ValueError("claim not found")

    now = datetime.now().isoformat(timespec="seconds")
    override_row = {
        "claim_id":        claim_id,
        "session_id":      session_id,
        "status":          status,
        "original_status": original.get("status"),
        "reason":          reason,
        "by":              admin_username,
        "at":              now,
        "claim":           original.get("claim", ""),
        "source_turn":     original.get("source_turn"),
        "source_agent":    original.get("source_agent"),
    }
    if feedback and feedback.strip():
        override_row["feedback"] = feedback.strip()

    _append_jsonl(session_dir / _OVERRIDES_FILENAME, override_row)

    if feedback and feedback.strip():
        _append_jsonl(feedback_dir / _FEEDBACK_FILENAME, {
            "ts":              now,
            "session_id":      session_id,
            "claim_id":        claim_id,
            "admin":           admin_username,
            "original_status": original.get("status"),
            "override_status": status,
            "reason":          reason,
            "feedback":        feedback.strip(),
            "claim":           original.get("claim", ""),
            "source_turn":     original.get("source_turn"),
            "source_agent":    original.get("source_agent"),
        })

    return _public_override(override_row)
