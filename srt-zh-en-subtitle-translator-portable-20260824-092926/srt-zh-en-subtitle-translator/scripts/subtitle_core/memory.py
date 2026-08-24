"""Governed, append-only translation-memory storage for PUREFAITH subtitles.

The legacy ``references/translation-memory.json`` file is seed evidence only.
This module never rewrites it.  Learning writes immutable candidate events;
approved candidates are promoted into content-addressed immutable releases.
An active release is selected through one atomically replaced pointer.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Iterable, Mapping, Sequence
import uuid


SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_REFERENCES_DIR = SKILL_DIR / "references"
DEFAULT_LEGACY_MEMORY_PATH = DEFAULT_REFERENCES_DIR / "translation-memory.json"
DEFAULT_STYLE_PROFILE_PATH = DEFAULT_REFERENCES_DIR / "style-profile.json"
DEFAULT_TERMBASE_PATH = DEFAULT_REFERENCES_DIR / "glossary.json"
DEFAULT_STATE_ROOT = SKILL_DIR / "work" / "memory-v2"

CANDIDATE_LOG = Path("candidates") / "candidates.jsonl"
REVIEW_LOG = Path("candidates") / "review-events.jsonl"
RELEASES_DIR = Path("releases")
ACTIVE_POINTER = Path("active-release.json")
POINTER_HISTORY = Path("release-history.jsonl")
RUNS_DIR = Path("runs")

_SPACE_RE = re.compile(r"[ \t]+")
_SOURCE_PUNCTUATION_RE = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()\-—_]+")
_TERMINAL_PUNCTUATION_RE = re.compile(r"[.,!?;:，。！？；：]+$")
_TERMINAL_BEFORE_QUOTE_RE = re.compile(r"[.,!?;:，。！？；：]+([\"'”’])$")
_CANONICAL_END_RE = re.compile(r"[.!?](?:[\"'”’])?$")


class MemoryGovernanceError(RuntimeError):
    """Base error for an unsafe or invalid governed-memory operation."""


class MemoryConflictError(MemoryGovernanceError):
    """Raised before a write when records conflict."""


class MemoryApprovalError(MemoryGovernanceError):
    """Raised when an unapproved candidate is selected for promotion."""


def utc_now() -> str:
    """Return a compact, timezone-explicit UTC timestamp."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_space(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def normalize_source_text(value: Any) -> str:
    """Normalize Chinese source text for duplicate/conflict comparisons."""

    return _SOURCE_PUNCTUATION_RE.sub("", str(value or "")).casefold()


def render_cue_text(value: Any) -> str:
    """Strip terminal punctuation while retaining useful internal punctuation."""

    text = normalize_space(value)
    previous = None
    while text and text != previous:
        previous = text
        text = _TERMINAL_PUNCTUATION_RE.sub("", text).rstrip()
        text = _TERMINAL_BEFORE_QUOTE_RE.sub(r"\1", text).rstrip()
    return text


def canonicalize_english(value: Any, *, complete_thought: bool = True) -> str:
    """Normalize English for storage, preserving normal sentence punctuation."""

    text = normalize_space(value)
    if not text:
        return ""
    # Canonical storage uses English punctuation, not full-width Chinese marks.
    text = text.replace("，", ",").replace("。", ".").replace("！", "!").replace("？", "?")
    text = text.replace("；", ";").replace("：", ":")
    if complete_thought and not _CANONICAL_END_RE.search(text):
        text += "."
    return text


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: str | Path) -> Any:
    json_path = Path(path)
    try:
        return json.loads(json_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise MemoryGovernanceError(f"JSON file does not exist: {json_path}") from exc
    except json.JSONDecodeError as exc:
        raise MemoryGovernanceError(f"Invalid JSON at {json_path}: {exc}") from exc


def _write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _atomic_write_json(path: Path, value: Any) -> None:
    """Write JSON beside the destination and atomically replace its pointer."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        _write_json_file(temp_path, value)
        # Windows rejects fsync on a read-only descriptor.
        with temp_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _append_jsonl(path: Path, events: Sequence[Mapping[str, Any]]) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(
        json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for event in events
    ).encode("utf-8")
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    jsonl_path = Path(path)
    if not jsonl_path.exists():
        return []
    events: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MemoryGovernanceError(
                    f"Invalid JSONL at {jsonl_path}:{line_number}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise MemoryGovernanceError(
                    f"JSONL event must be an object at {jsonl_path}:{line_number}"
                )
            events.append(value)
    return events


def _record_identity_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    source = record.get("source", {})
    target = record.get("target", {})
    return {
        "source": {
            "language": source.get("language", "zh-CN"),
            "normalized_text": source.get("normalized_text")
            or normalize_source_text(source.get("text", "")),
            "span": source.get("span", {}),
        },
        "target": {
            "language": target.get("language", "en"),
            "locale": target.get("locale", "en-US"),
            "canonical_text": target.get("canonical_text", ""),
            "rendered_cues": target.get("rendered_cues", []),
        },
        "classification": record.get("classification", {}),
    }


def stable_record_id(record: Mapping[str, Any], *, prefix: str = "tm") -> str:
    return f"{prefix}-{sha256_value(_record_identity_payload(record))[:24]}"


def _candidate_identity_payload(
    source_text: str,
    canonical_english: str,
    rendered_cues: Sequence[Mapping[str, Any]],
    classification: Mapping[str, Any],
    source_span: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "source_text": source_text,
        "canonical_english": canonical_english,
        "rendered_cues": list(rendered_cues),
        "classification": dict(classification),
        "source_span": dict(source_span),
    }


def build_candidate_record(
    source_text: str,
    canonical_english: str,
    *,
    rendered_cues: Sequence[Mapping[str, Any] | str] | None = None,
    source_span: Mapping[str, Any] | None = None,
    context_before: Sequence[str] | str | None = None,
    context_after: Sequence[str] | str | None = None,
    classification: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    raw: Mapping[str, Any] | None = None,
    policy_versions: Mapping[str, Any] | None = None,
    quality_flags: Iterable[str] | None = None,
    batch_id: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create an immutable, pending candidate event.

    This function never writes.  ``append_candidates`` is the only write step.
    The canonical English receives normal sentence punctuation; rendered cue
    strings are stored separately without terminal punctuation.
    """

    source_text = normalize_space(source_text)
    if not source_text:
        raise MemoryGovernanceError("Candidate source text must not be empty")
    canonical = canonicalize_english(canonical_english, complete_thought=True)
    if not canonical:
        raise MemoryGovernanceError("Candidate canonical English must not be empty")

    span = dict(source_span or {})
    span.setdefault("kind", "complete_thought")
    span.setdefault("document_id", None)
    span.setdefault("sentence_group_id", None)
    span.setdefault("cue_indexes", [])
    if span["kind"] != "complete_thought":
        raise MemoryGovernanceError(
            "New candidates must represent a complete thought; legacy fragments use migration"
        )

    rendered: list[dict[str, Any]] = []
    if rendered_cues is None:
        rendered.append({"cue_index": None, "text": render_cue_text(canonical)})
    else:
        for position, cue in enumerate(rendered_cues):
            if isinstance(cue, str):
                cue_index = None
                cue_text = cue
            elif isinstance(cue, Mapping):
                cue_index = cue.get("cue_index")
                cue_text = cue.get("text", "")
            else:
                raise MemoryGovernanceError(
                    f"Rendered cue {position + 1} must be a string or object"
                )
            clean = render_cue_text(cue_text)
            if not clean:
                raise MemoryGovernanceError(
                    f"Rendered cue {position + 1} must not be empty"
                )
            rendered.append({"cue_index": cue_index, "text": clean})

    def context_list(value: Sequence[str] | str | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = normalize_space(value)
            return [cleaned] if cleaned else []
        return [normalize_space(item) for item in value if normalize_space(item)]

    classification_data = dict(classification or {})
    classification_data.setdefault("brand", "PUREFAITH")
    classification_data.setdefault("domains", [])
    identity = _candidate_identity_payload(
        source_text,
        canonical,
        rendered,
        classification_data,
        span,
    )
    candidate_id = f"cand-{sha256_value(identity)[:24]}"
    timestamp = created_at or utc_now()

    record: dict[str, Any] = {
        "schema_version": "2.0",
        "record_id": f"candidate-{candidate_id.removeprefix('cand-')}",
        "status": "candidate",
        "source": {
            "language": "zh-CN",
            "text": source_text,
            "normalized_text": normalize_source_text(source_text),
            "span": span,
            "context_before": context_list(context_before),
            "context_after": context_list(context_after),
            "context_links": {
                "before_record_ids": [],
                "after_record_ids": [],
            },
        },
        "target": {
            "language": "en",
            "locale": "en-US",
            "canonical_text": canonical,
            "rendered_cues": rendered,
        },
        "classification": classification_data,
        "provenance": {
            "origin": "review_candidate",
            **dict(provenance or {}),
        },
        "approval": {
            "status": "pending",
        },
        "quality": {
            "flags": sorted(set(str(flag) for flag in (quality_flags or []))),
        },
        "policy_versions": dict(policy_versions or {}),
        "lifecycle": {
            "created_at": timestamp,
            "supersedes": [],
        },
        "retrieval": {
            "eligible": False,
            "reasons": ["candidate_not_approved"],
        },
    }
    event_identity = {
        "candidate_id": candidate_id,
        "created_at": timestamp,
        "raw": dict(raw or {}),
        "record": record,
    }
    return {
        "schema_version": "2.0",
        "candidate_id": candidate_id,
        "event_id": f"evt-{sha256_value(event_identity)[:24]}",
        "event_type": "candidate_created",
        "created_at": timestamp,
        "status": "candidate",
        "batch_id": batch_id,
        "raw": dict(raw or {}),
        "record": record,
    }


def _coerce_candidate(
    candidate: Mapping[str, Any],
    *,
    batch_id: str | None,
) -> dict[str, Any]:
    if candidate.get("event_type") == "candidate_created":
        event = deepcopy(dict(candidate))
        if batch_id is not None:
            event["batch_id"] = batch_id
        return event
    source_text = (
        candidate.get("source_text")
        or candidate.get("zh")
        or candidate.get("source")
        or ""
    )
    canonical = (
        candidate.get("canonical_english")
        or candidate.get("canonical_en")
        or candidate.get("final_en")
        or candidate.get("revised_en")
        or ""
    )
    return build_candidate_record(
        str(source_text),
        str(canonical),
        rendered_cues=candidate.get("rendered_cues"),
        source_span=candidate.get("source_span"),
        context_before=candidate.get("context_before"),
        context_after=candidate.get("context_after"),
        classification=candidate.get("classification"),
        provenance=candidate.get("provenance"),
        raw=candidate.get("raw", candidate),
        policy_versions=candidate.get("policy_versions"),
        quality_flags=candidate.get("quality_flags"),
        batch_id=batch_id,
        created_at=candidate.get("created_at"),
    )


def append_candidates(
    state_root: str | Path,
    candidates: Iterable[Mapping[str, Any]],
    *,
    batch_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Append raw candidate events; never update a release."""

    root = Path(state_root)
    candidate_path = root / CANDIDATE_LOG
    existing = {
        event.get("candidate_id"): event
        for event in read_jsonl(candidate_path)
        if event.get("event_type") == "candidate_created"
    }
    prepared: list[dict[str, Any]] = []
    duplicate_ids: list[str] = []
    for raw_candidate in candidates:
        event = _coerce_candidate(raw_candidate, batch_id=batch_id)
        issues = lint_memory_records([event.get("record", {})])
        errors = [issue for issue in issues if issue["severity"] == "error"]
        if errors:
            raise MemoryGovernanceError(errors[0]["message"])
        candidate_id = event["candidate_id"]
        prior = existing.get(candidate_id)
        if prior:
            if sha256_value(
                _record_identity_payload(prior.get("record", {}))
            ) != sha256_value(_record_identity_payload(event.get("record", {}))):
                raise MemoryConflictError(
                    f"Candidate ID collision with different content: {candidate_id}"
                )
            duplicate_ids.append(candidate_id)
            continue
        existing[candidate_id] = event
        prepared.append(event)

    if not dry_run:
        _append_jsonl(candidate_path, prepared)
    return {
        "dry_run": dry_run,
        "candidate_log": str(candidate_path),
        "candidate_count": len(prepared),
        "candidate_ids": [event["candidate_id"] for event in prepared],
        "duplicates_skipped": sorted(duplicate_ids),
        "release_mutated": False,
    }


def load_candidate_events(state_root: str | Path) -> list[dict[str, Any]]:
    return read_jsonl(Path(state_root) / CANDIDATE_LOG)


def append_review_decision(
    state_root: str | Path,
    candidate_id: str,
    *,
    decision: str,
    reviewer_id: str,
    rationale: str,
    dry_run: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Append an approval/rejection event without mutating the candidate."""

    if decision not in {"approved", "rejected"}:
        raise MemoryApprovalError("Review decision must be approved or rejected")
    candidates = {
        event.get("candidate_id"): event
        for event in load_candidate_events(state_root)
        if event.get("event_type") == "candidate_created"
    }
    if candidate_id not in candidates:
        raise MemoryApprovalError(f"Unknown candidate: {candidate_id}")
    if not normalize_space(reviewer_id):
        raise MemoryApprovalError("reviewer_id is required")
    if not normalize_space(rationale):
        raise MemoryApprovalError("A non-empty review rationale is required")
    timestamp = created_at or utc_now()
    identity = {
        "candidate_id": candidate_id,
        "decision": decision,
        "reviewer_id": reviewer_id,
        "rationale": rationale,
        "created_at": timestamp,
    }
    event = {
        "schema_version": "2.0",
        "event_id": f"review-{sha256_value(identity)[:24]}",
        "event_type": "candidate_reviewed",
        "candidate_id": candidate_id,
        "decision": decision,
        "reviewer_id": normalize_space(reviewer_id),
        "rationale": normalize_space(rationale),
        "created_at": timestamp,
    }
    if not dry_run:
        _append_jsonl(Path(state_root) / REVIEW_LOG, [event])
    return {"dry_run": dry_run, "event": event}


def latest_review_decisions(state_root: str | Path) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    for event in read_jsonl(Path(state_root) / REVIEW_LOG):
        if event.get("event_type") == "candidate_reviewed" and event.get("candidate_id"):
            decisions[str(event["candidate_id"])] = event
    return decisions


def _record_source_key(record: Mapping[str, Any]) -> str:
    source = record.get("source", {})
    return str(source.get("normalized_text") or normalize_source_text(source.get("text", "")))


def _record_target_key(record: Mapping[str, Any]) -> str:
    target = record.get("target", {})
    return normalize_space(target.get("canonical_text", "")).casefold()


def check_record_conflicts(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Find duplicate IDs and source/target conflicts without changing records."""

    issues: list[dict[str, Any]] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    by_source: dict[str, list[Mapping[str, Any]]] = {}
    for position, record in enumerate(records, 1):
        record_id = str(record.get("record_id", ""))
        if record_id in by_id:
            prior = by_id[record_id]
            issues.append(
                {
                    "severity": "error",
                    "code": "duplicate_record_id",
                    "record_id": record_id,
                    "positions": [position],
                    "message": f"Duplicate record_id {record_id}",
                    "identical": sha256_value(prior) == sha256_value(record),
                }
            )
        else:
            by_id[record_id] = record
        source_key = _record_source_key(record)
        if source_key:
            by_source.setdefault(source_key, []).append(record)

    for source_key, group in sorted(by_source.items()):
        if len(group) < 2:
            continue
        target_groups: dict[str, list[str]] = {}
        for record in group:
            target_groups.setdefault(_record_target_key(record), []).append(
                str(record.get("record_id", ""))
            )
        if len(target_groups) > 1:
            approved_or_eligible = any(
                record.get("status") == "approved"
                or bool(record.get("retrieval", {}).get("eligible"))
                for record in group
            )
            issues.append(
                {
                    "severity": "error" if approved_or_eligible else "warning",
                    "code": "source_target_conflict",
                    "source_key": source_key,
                    "record_ids": sorted(
                        str(record.get("record_id", "")) for record in group
                    ),
                    "targets": sorted(target_groups),
                    "message": (
                        "A normalized Chinese source has multiple target translations"
                    ),
                }
            )
        else:
            issues.append(
                {
                    "severity": "warning",
                    "code": "duplicate_source_target",
                    "source_key": source_key,
                    "record_ids": sorted(
                        str(record.get("record_id", "")) for record in group
                    ),
                    "message": "Duplicate normalized source/target evidence",
                }
            )
    return issues


def lint_memory_records(
    records: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Perform dependency-free structural and governance linting."""

    issues: list[dict[str, Any]] = []

    def add(
        severity: str,
        code: str,
        message: str,
        *,
        record_id: str | None = None,
    ) -> None:
        issue: dict[str, Any] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if record_id:
            issue["record_id"] = record_id
        issues.append(issue)

    for position, raw_record in enumerate(records, 1):
        if not isinstance(raw_record, Mapping):
            add("error", "record_not_object", f"Record {position} must be an object")
            continue
        record = raw_record
        record_id = str(record.get("record_id", "")).strip()
        if not record_id:
            add("error", "missing_record_id", f"Record {position} has no record_id")
        if record.get("schema_version") != "2.0":
            add("error", "unsupported_schema", "schema_version must be 2.0", record_id=record_id)
        status = record.get("status")
        if status not in {
            "candidate",
            "legacy_unverified",
            "approved",
            "deprecated",
            "quarantined",
        }:
            add("error", "invalid_status", f"Unsupported status {status!r}", record_id=record_id)

        source = record.get("source")
        target = record.get("target")
        if not isinstance(source, Mapping):
            add("error", "invalid_source", "source must be an object", record_id=record_id)
            continue
        if not isinstance(target, Mapping):
            add("error", "invalid_target", "target must be an object", record_id=record_id)
            continue

        source_text = normalize_space(source.get("text", ""))
        normalized = str(source.get("normalized_text", ""))
        if not source_text:
            add("error", "empty_source", "source.text must not be empty", record_id=record_id)
        if not normalized:
            add(
                "error",
                "missing_normalized_source",
                "source.normalized_text is required",
                record_id=record_id,
            )
        elif normalized != normalize_source_text(source_text):
            add(
                "warning",
                "normalized_source_drift",
                "source.normalized_text differs from the current normalizer",
                record_id=record_id,
            )

        span = source.get("span")
        if not isinstance(span, Mapping) or span.get("kind") not in {
            "complete_thought",
            "cue_fragment",
            "legacy_unknown",
        }:
            add("error", "invalid_span", "source.span.kind is invalid", record_id=record_id)

        canonical = normalize_space(target.get("canonical_text", ""))
        if not canonical:
            add(
                "error",
                "empty_canonical_target",
                "target.canonical_text must not be empty",
                record_id=record_id,
            )
        if (
            isinstance(span, Mapping)
            and span.get("kind") == "complete_thought"
            and canonical
            and not _CANONICAL_END_RE.search(canonical)
        ):
            add(
                "error",
                "canonical_punctuation_missing",
                "Complete-thought canonical English needs terminal punctuation",
                record_id=record_id,
            )

        rendered = target.get("rendered_cues")
        if not isinstance(rendered, list) or not rendered:
            add(
                "error",
                "missing_rendered_cues",
                "target.rendered_cues must be a non-empty list",
                record_id=record_id,
            )
        else:
            for cue in rendered:
                cue_text = normalize_space(cue.get("text", "")) if isinstance(cue, Mapping) else ""
                if not cue_text:
                    add(
                        "error",
                        "empty_rendered_cue",
                        "Rendered cue text must not be empty",
                        record_id=record_id,
                    )
                elif cue_text != render_cue_text(cue_text):
                    add(
                        "error",
                        "rendered_terminal_punctuation",
                        "Rendered PUREFAITH cue retains terminal punctuation",
                        record_id=record_id,
                    )

        approval = record.get("approval")
        retrieval = record.get("retrieval")
        if not isinstance(approval, Mapping):
            add("error", "missing_approval", "approval object is required", record_id=record_id)
            approval = {}
        if not isinstance(retrieval, Mapping):
            add("error", "missing_retrieval", "retrieval object is required", record_id=record_id)
            retrieval = {}
        eligible = bool(retrieval.get("eligible"))
        if eligible and (status != "approved" or approval.get("status") != "approved"):
            add(
                "error",
                "unsafe_retrieval_eligibility",
                "Only explicitly approved records may be retrieval eligible",
                record_id=record_id,
            )
        if status == "legacy_unverified" and eligible:
            add(
                "error",
                "legacy_retrieval_enabled",
                "Legacy-unverified records must remain retrieval ineligible",
                record_id=record_id,
            )

        for required in (
            "classification",
            "provenance",
            "quality",
            "policy_versions",
            "lifecycle",
        ):
            if not isinstance(record.get(required), Mapping):
                add(
                    "error",
                    f"missing_{required}",
                    f"{required} object is required",
                    record_id=record_id,
                )

    issues.extend(check_record_conflicts(records))
    return sorted(
        issues,
        key=lambda item: (
            0 if item["severity"] == "error" else 1,
            item["code"],
            item.get("record_id", ""),
        ),
    )


def _release_payload(
    records: Sequence[Mapping[str, Any]],
    *,
    parent_release_id: str | None,
    source: Mapping[str, Any],
    policy_versions: Mapping[str, Any],
    created_at: str,
) -> tuple[str, str, dict[str, Any]]:
    ordered_records = sorted(
        (deepcopy(dict(record)) for record in records),
        key=lambda item: str(item.get("record_id", "")),
    )
    records_sha = sha256_value(ordered_records)
    release_identity = {
        "records_sha256": records_sha,
        "parent_release_id": parent_release_id,
        "source": dict(source),
        "policy_versions": dict(policy_versions),
    }
    release_id = f"rel-{sha256_value(release_identity)[:24]}"
    payload = {
        "schema_version": "2.0",
        "release_id": release_id,
        "created_at": created_at,
        "parent_release_id": parent_release_id,
        "source": dict(source),
        "policy_versions": dict(policy_versions),
        "record_count": len(ordered_records),
        "records_sha256": records_sha,
        "records": ordered_records,
    }
    return release_id, records_sha, payload


def create_release(
    state_root: str | Path,
    records: Sequence[Mapping[str, Any]],
    *,
    parent_release_id: str | None = None,
    source: Mapping[str, Any] | None = None,
    policy_versions: Mapping[str, Any] | None = None,
    activate: bool = False,
    actor_id: str = "system",
    dry_run: bool = False,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a content-addressed immutable release and optionally activate it."""

    root = Path(state_root)
    timestamp = created_at or utc_now()
    issues = lint_memory_records(records)
    blocking = [issue for issue in issues if issue["severity"] == "error"]
    if blocking:
        raise MemoryConflictError(
            f"Release blocked by {blocking[0]['code']}: {blocking[0]['message']}"
        )
    release_id, records_sha, payload = _release_payload(
        records,
        parent_release_id=parent_release_id,
        source=source or {"origin": "governed_release"},
        policy_versions=policy_versions or {},
        created_at=timestamp,
    )
    release_dir = root / RELEASES_DIR / release_id
    memory_path = release_dir / "memory.json"
    manifest_path = release_dir / "manifest.json"
    already_exists = release_dir.exists()

    if already_exists:
        if not memory_path.exists() or not manifest_path.exists():
            raise MemoryGovernanceError(
                f"Release directory is incomplete and cannot be overwritten: {release_dir}"
            )
        existing = read_json(memory_path)
        if (
            existing.get("records_sha256") != records_sha
            or existing.get("parent_release_id") != parent_release_id
            or existing.get("policy_versions", {}) != dict(policy_versions or {})
        ):
            raise MemoryConflictError(
                f"Immutable release ID collision at {release_dir}"
            )
    elif not dry_run:
        releases_root = root / RELEASES_DIR
        releases_root.mkdir(parents=True, exist_ok=True)
        temp_dir = releases_root / f".tmp-{release_id}-{uuid.uuid4().hex}"
        try:
            temp_dir.mkdir(parents=False, exist_ok=False)
            temp_memory = temp_dir / "memory.json"
            _write_json_file(temp_memory, payload)
            memory_sha = file_sha256(temp_memory)
            manifest = {
                "schema_version": "2.0",
                "release_id": release_id,
                "immutable": True,
                "created_at": timestamp,
                "parent_release_id": parent_release_id,
                "memory_file": "memory.json",
                "memory_sha256": memory_sha,
                "records_sha256": records_sha,
                "record_count": len(records),
            }
            _write_json_file(temp_dir / "manifest.json", manifest)
            os.replace(temp_dir, release_dir)
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    activation: dict[str, Any] | None = None
    if activate:
        if dry_run and not (root / RELEASES_DIR / release_id / "memory.json").exists():
            current = load_active_pointer(root)
            activation = {
                "dry_run": True,
                "previous_release_id": current.get("release_id"),
                "release_id": release_id,
                "planned": True,
            }
        else:
            activation = activate_release(
                root,
                release_id,
                reason="release_created",
                actor_id=actor_id,
                dry_run=dry_run,
            )
    return {
        "dry_run": dry_run,
        "release_id": release_id,
        "release_path": str(release_dir),
        "record_count": len(records),
        "records_sha256": records_sha,
        "already_exists": already_exists,
        "activated": bool(activate),
        "activation": activation,
        "warning_count": sum(issue["severity"] == "warning" for issue in issues),
        "issues": issues,
    }


def list_releases(state_root: str | Path) -> list[dict[str, Any]]:
    root = Path(state_root)
    releases_root = root / RELEASES_DIR
    if not releases_root.exists():
        return []
    releases: list[dict[str, Any]] = []
    active = load_active_pointer(root)
    for release_dir in sorted(releases_root.iterdir()):
        manifest_path = release_dir / "manifest.json"
        if not release_dir.is_dir() or release_dir.name.startswith(".") or not manifest_path.exists():
            continue
        manifest = read_json(manifest_path)
        manifest["path"] = str(release_dir)
        manifest["active"] = manifest.get("release_id") == active.get("release_id")
        releases.append(manifest)
    return releases


def load_release(state_root: str | Path, release_id: str) -> dict[str, Any]:
    release_path = Path(state_root) / RELEASES_DIR / release_id / "memory.json"
    payload = read_json(release_path)
    if payload.get("release_id") != release_id:
        raise MemoryGovernanceError(
            f"Release ID mismatch inside {release_path}: {payload.get('release_id')!r}"
        )
    manifest_path = release_path.with_name("manifest.json")
    manifest = read_json(manifest_path)
    actual_memory_sha = file_sha256(release_path)
    if manifest.get("memory_sha256") != actual_memory_sha:
        raise MemoryGovernanceError(f"Immutable release checksum failed: {release_id}")
    if payload.get("records_sha256") != sha256_value(payload.get("records", [])):
        raise MemoryGovernanceError(f"Record checksum failed: {release_id}")
    return payload


def load_active_pointer(state_root: str | Path) -> dict[str, Any]:
    pointer_path = Path(state_root) / ACTIVE_POINTER
    if not pointer_path.exists():
        return {}
    pointer = read_json(pointer_path)
    if not isinstance(pointer, dict):
        raise MemoryGovernanceError(f"Active pointer must be an object: {pointer_path}")
    return pointer


def load_active_release(state_root: str | Path) -> dict[str, Any] | None:
    pointer = load_active_pointer(state_root)
    release_id = pointer.get("release_id")
    if not release_id:
        return None
    return load_release(state_root, str(release_id))


def activate_release(
    state_root: str | Path,
    release_id: str,
    *,
    reason: str,
    actor_id: str,
    dry_run: bool = False,
    activated_at: str | None = None,
) -> dict[str, Any]:
    """Atomically point at an existing immutable release."""

    root = Path(state_root)
    release_path = root / RELEASES_DIR / release_id / "memory.json"
    if not release_path.exists():
        raise MemoryGovernanceError(f"Cannot activate unknown release: {release_id}")
    # Verify immutability before changing the pointer.
    load_release(root, release_id)
    current = load_active_pointer(root)
    previous = current.get("release_id")
    timestamp = activated_at or utc_now()
    identity = {
        "release_id": release_id,
        "previous_release_id": previous,
        "activated_at": timestamp,
        "reason": reason,
        "actor_id": actor_id,
    }
    event_id = f"pointer-{sha256_value(identity)[:24]}"
    pointer = {
        "schema_version": "2.0",
        "release_id": release_id,
        "previous_release_id": previous,
        "activated_at": timestamp,
        "reason": normalize_space(reason),
        "actor_id": normalize_space(actor_id),
        "pointer_event_id": event_id,
    }
    history_event = {
        **pointer,
        "event_type": "active_release_changed",
    }
    if not dry_run:
        _atomic_write_json(root / ACTIVE_POINTER, pointer)
        _append_jsonl(root / POINTER_HISTORY, [history_event])
    return {
        "dry_run": dry_run,
        "previous_release_id": previous,
        "release_id": release_id,
        "pointer": pointer,
    }


def rollback_release(
    state_root: str | Path,
    release_id: str,
    *,
    actor_id: str,
    reason: str = "manual_rollback",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Rollback by repointing only; no release or candidate data is deleted."""

    current = load_active_pointer(state_root).get("release_id")
    if current == release_id:
        return {
            "dry_run": dry_run,
            "previous_release_id": current,
            "release_id": release_id,
            "no_op": True,
        }
    result = activate_release(
        state_root,
        release_id,
        reason=f"rollback:{normalize_space(reason)}",
        actor_id=actor_id,
        dry_run=dry_run,
    )
    result["no_op"] = False
    return result


def load_memory_records(
    state_root: str | Path | None = None,
    *,
    release_id: str | None = None,
    include_unverified: bool = False,
) -> list[dict[str, Any]]:
    """Load records from an immutable release.

    No active release means an empty result; the function never silently falls
    back to the legacy memory.  By default only approved, retrieval-eligible
    records are returned.
    """

    root = Path(state_root) if state_root is not None else DEFAULT_STATE_ROOT
    if release_id:
        release = load_release(root, release_id)
    else:
        release = load_active_release(root)
        if release is None:
            return []
    records = [deepcopy(record) for record in release.get("records", [])]
    if include_unverified:
        return records
    return [
        record
        for record in records
        if record.get("status") == "approved"
        and record.get("approval", {}).get("status") == "approved"
        and bool(record.get("retrieval", {}).get("eligible"))
    ]


def _promote_candidate_record(
    candidate_event: Mapping[str, Any],
    review_event: Mapping[str, Any],
    *,
    promoted_at: str,
    policy_versions: Mapping[str, Any] | None,
) -> dict[str, Any]:
    record = deepcopy(dict(candidate_event["record"]))
    record["status"] = "approved"
    record["record_id"] = stable_record_id(record, prefix="tm")
    record["approval"] = {
        "status": "approved",
        "review_event_id": review_event["event_id"],
        "reviewer_id": review_event["reviewer_id"],
        "approved_at": review_event["created_at"],
        "rationale": review_event["rationale"],
    }
    record["provenance"] = {
        **dict(record.get("provenance", {})),
        "candidate_id": candidate_event["candidate_id"],
        "candidate_event_id": candidate_event["event_id"],
        "review_event_id": review_event["event_id"],
    }
    record["policy_versions"] = {
        **dict(record.get("policy_versions", {})),
        **dict(policy_versions or {}),
    }
    lifecycle = dict(record.get("lifecycle", {}))
    lifecycle["promoted_at"] = promoted_at
    lifecycle.setdefault("supersedes", [])
    record["lifecycle"] = lifecycle
    record["retrieval"] = {
        "eligible": True,
        "reasons": [],
    }
    return record


def promote_candidates(
    state_root: str | Path,
    candidate_ids: Sequence[str],
    *,
    actor_id: str,
    policy_versions: Mapping[str, Any] | None = None,
    activate: bool = True,
    dry_run: bool = False,
    promoted_at: str | None = None,
) -> dict[str, Any]:
    """Promote explicitly approved candidates into a new immutable release."""

    if not candidate_ids:
        raise MemoryApprovalError("At least one candidate_id is required")
    root = Path(state_root)
    candidate_map = {
        str(event.get("candidate_id")): event
        for event in load_candidate_events(root)
        if event.get("event_type") == "candidate_created"
    }
    reviews = latest_review_decisions(root)
    selected_events: list[dict[str, Any]] = []
    selected_reviews: list[dict[str, Any]] = []
    for candidate_id in candidate_ids:
        candidate = candidate_map.get(candidate_id)
        if candidate is None:
            raise MemoryApprovalError(f"Unknown candidate: {candidate_id}")
        review = reviews.get(candidate_id)
        if review is None or review.get("decision") != "approved":
            raise MemoryApprovalError(
                f"Candidate has no current approval event: {candidate_id}"
            )
        selected_events.append(candidate)
        selected_reviews.append(review)

    active = load_active_release(root)
    parent_release_id = active.get("release_id") if active else None
    base_records = [deepcopy(record) for record in (active or {}).get("records", [])]
    timestamp = promoted_at or utc_now()
    promoted = [
        _promote_candidate_record(
            candidate,
            review,
            promoted_at=timestamp,
            policy_versions=policy_versions,
        )
        for candidate, review in zip(selected_events, selected_reviews)
    ]

    promoted_source_keys: dict[str, list[dict[str, Any]]] = {}
    for record in promoted:
        promoted_source_keys.setdefault(_record_source_key(record), []).append(record)
    for source_key, group in promoted_source_keys.items():
        if len({_record_target_key(record) for record in group}) > 1:
            raise MemoryConflictError(
                f"Selected candidates conflict for normalized source {source_key!r}"
            )

    duplicates_skipped: list[str] = []
    replaced_record_ids: list[str] = []
    new_records: list[dict[str, Any]] = []
    for record in base_records:
        source_key = _record_source_key(record)
        incoming = promoted_source_keys.get(source_key)
        if not incoming:
            new_records.append(record)
            continue
        incoming_record = incoming[0]
        same_target = _record_target_key(record) == _record_target_key(incoming_record)
        if (
            record.get("status") == "approved"
            and record.get("approval", {}).get("status") == "approved"
            and same_target
        ):
            duplicates_skipped.extend(
                str(candidate["candidate_id"])
                for candidate in selected_events
                if _record_source_key(candidate["record"]) == source_key
            )
            new_records.append(record)
            promoted_source_keys.pop(source_key, None)
            continue
        if record.get("status") == "approved" and not same_target:
            allowed_supersedes = set(incoming_record.get("lifecycle", {}).get("supersedes", []))
            if record.get("record_id") not in allowed_supersedes:
                raise MemoryConflictError(
                    "Approved source conflict requires the candidate to explicitly "
                    f"supersede {record.get('record_id')}"
                )
        replaced_record_ids.append(str(record.get("record_id")))

    for source_key, group in promoted_source_keys.items():
        incoming = group[0]
        if replaced_record_ids:
            relevant_replacements = [
                record_id
                for record_id in replaced_record_ids
                if any(
                    _record_source_key(record) == source_key
                    and record.get("record_id") == record_id
                    for record in base_records
                )
            ]
            incoming["lifecycle"]["supersedes"] = sorted(
                set(incoming["lifecycle"].get("supersedes", []))
                | set(relevant_replacements)
            )
        new_records.append(incoming)

    release_source = {
        "origin": "candidate_promotion",
        "candidate_ids": sorted(set(candidate_ids) - set(duplicates_skipped)),
        "review_event_ids": sorted(
            review["event_id"]
            for candidate, review in zip(selected_events, selected_reviews)
            if candidate["candidate_id"] not in set(duplicates_skipped)
        ),
        "actor_id": normalize_space(actor_id),
    }
    release = create_release(
        root,
        new_records,
        parent_release_id=parent_release_id,
        source=release_source,
        policy_versions=policy_versions,
        activate=activate,
        actor_id=actor_id,
        dry_run=dry_run,
        created_at=timestamp,
    )
    return {
        **release,
        "candidate_ids_requested": list(candidate_ids),
        "candidate_ids_promoted": release_source["candidate_ids"],
        "duplicates_skipped": sorted(set(duplicates_skipped)),
        "replaced_record_ids": sorted(set(replaced_record_ids)),
    }


def lint_style_profile(profile: Mapping[str, Any] | str | Path) -> list[dict[str, Any]]:
    if isinstance(profile, (str, Path)):
        data = read_json(profile)
    else:
        data = dict(profile)
    issues: list[dict[str, Any]] = []

    def add(severity: str, code: str, message: str, *, rule_id: str | None = None) -> None:
        issue: dict[str, Any] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if rule_id:
            issue["rule_id"] = rule_id
        issues.append(issue)

    if data.get("schema_version") != "2.0":
        add("error", "unsupported_style_schema", "style schema_version must be 2.0")
    if data.get("rendering_rules", {}).get("cue_terminal_punctuation") != "strip":
        add(
            "error",
            "unsafe_render_policy",
            "PUREFAITH cue_terminal_punctuation must be strip",
        )
    rules = data.get("preferred_phrases")
    if not isinstance(rules, list):
        add("error", "style_rules_not_list", "preferred_phrases must be a list")
        return issues
    seen_ids: set[str] = set()
    for position, rule in enumerate(rules, 1):
        if not isinstance(rule, Mapping):
            add("error", "style_rule_not_object", f"Rule {position} must be an object")
            continue
        rule_id = str(rule.get("rule_id", "")).strip()
        if not rule_id:
            add("error", "missing_rule_id", f"Rule {position} has no rule_id")
            continue
        if rule_id in seen_ids:
            add("error", "duplicate_rule_id", f"Duplicate rule_id {rule_id}", rule_id=rule_id)
        seen_ids.add(rule_id)
        preferred = normalize_space(rule.get("preferred_en", ""))
        if not preferred:
            add("error", "empty_preferred_phrase", "preferred_en is empty", rule_id=rule_id)
        if re.search(r"\s/\s", preferred):
            add(
                "error",
                "ambiguous_phrase_alternatives",
                "preferred_en must be atomic and cannot contain slash alternatives",
                rule_id=rule_id,
            )
        canonical = normalize_space(rule.get("canonical_en", ""))
        if not canonical or not _CANONICAL_END_RE.search(canonical):
            add(
                "error",
                "canonical_style_punctuation",
                "canonical_en must have normal sentence punctuation",
                rule_id=rule_id,
            )
        if rule.get("match_mode") not in {
            "all_in_window",
            "any_in_window",
            "exact_or_contained",
        }:
            add("error", "invalid_match_mode", "Unsupported match_mode", rule_id=rule_id)
        signals = rule.get("zh_contains")
        if not isinstance(signals, list) or not signals or any(
            not normalize_space(signal) for signal in signals
        ):
            add("error", "invalid_style_signals", "zh_contains is invalid", rule_id=rule_id)
        if not isinstance(rule.get("context_scope"), Mapping):
            add("error", "missing_context_scope", "context_scope is required", rule_id=rule_id)
    return sorted(
        issues,
        key=lambda item: (
            0 if item["severity"] == "error" else 1,
            item["code"],
            item.get("rule_id", ""),
        ),
    )


def legacy_memory_stats(path: str | Path = DEFAULT_LEGACY_MEMORY_PATH) -> dict[str, Any]:
    data = read_json(path)
    if not isinstance(data, Mapping):
        raise MemoryGovernanceError("Legacy memory root must be an object")
    examples = data.get("examples", [])
    if not isinstance(examples, list):
        raise MemoryGovernanceError("Legacy memory examples must be a list")
    context_count = sum(
        isinstance(item, Mapping)
        and bool(item.get("context_before"))
        and bool(item.get("context_after"))
        for item in examples
    )
    blank_drafts = sum(
        isinstance(item, Mapping) and not normalize_space(item.get("draft_en", ""))
        for item in examples
    )
    source_groups: dict[str, set[str]] = {}
    for item in examples:
        if not isinstance(item, Mapping):
            continue
        source_groups.setdefault(
            normalize_source_text(item.get("zh", "")),
            set(),
        ).add(normalize_space(item.get("final_en", "")).casefold())
    conflicts = sum(len(targets) > 1 for targets in source_groups.values())
    return {
        "path": str(Path(path)),
        "sha256": file_sha256(path),
        "actual_count": len(examples),
        "declared_count": data.get("example_count"),
        "context_pair_count": context_count,
        "blank_draft_count": blank_drafts,
        "normalized_source_conflict_count": conflicts,
    }


def resource_stats(
    references_dir: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    references = Path(references_dir) if references_dir is not None else DEFAULT_REFERENCES_DIR
    from .terminology import termbase_stats

    style = read_json(references / "style-profile.json")
    termbase = termbase_stats(references / "glossary.json")
    active = load_active_release(state_root) if state_root is not None else None
    candidate_events = (
        load_candidate_events(state_root) if state_root is not None else []
    )
    review_events = (
        read_jsonl(Path(state_root) / REVIEW_LOG) if state_root is not None else []
    )
    return {
        "legacy": legacy_memory_stats(references / "translation-memory.json"),
        "style": {
            "profile_id": style.get("profile_id"),
            "version": style.get("version"),
            "rule_count": len(style.get("preferred_phrases", [])),
            "principle_count": len(style.get("translation_principles", [])),
        },
        "termbase": termbase,
        "state": {
            "active_release_id": active.get("release_id") if active else None,
            "active_record_count": active.get("record_count", 0) if active else 0,
            "retrieval_eligible_count": sum(
                bool(record.get("retrieval", {}).get("eligible"))
                for record in (active or {}).get("records", [])
            ),
            "candidate_event_count": len(candidate_events),
            "review_event_count": len(review_events),
            "release_count": len(list_releases(state_root)) if state_root is not None else 0,
        },
    }


def lint_resources(
    references_dir: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    """Lint references and, optionally, the active governed state."""

    references = Path(references_dir) if references_dir is not None else DEFAULT_REFERENCES_DIR
    from .terminology import lint_termbase

    issues: list[dict[str, Any]] = []
    try:
        issues.extend(lint_style_profile(references / "style-profile.json"))
    except MemoryGovernanceError as exc:
        issues.append({"severity": "error", "code": "style_load_failed", "message": str(exc)})
    try:
        issues.extend(lint_termbase(references / "glossary.json"))
    except Exception as exc:
        issues.append({"severity": "error", "code": "termbase_load_failed", "message": str(exc)})
    try:
        legacy = legacy_memory_stats(references / "translation-memory.json")
        if legacy["declared_count"] != legacy["actual_count"]:
            issues.append(
                {
                    "severity": "error",
                    "code": "legacy_count_drift",
                    "message": "Legacy example_count does not match actual examples",
                }
            )
    except MemoryGovernanceError as exc:
        issues.append({"severity": "error", "code": "legacy_load_failed", "message": str(exc)})

    if state_root is not None:
        try:
            active = load_active_release(state_root)
            if active:
                issues.extend(lint_memory_records(active.get("records", [])))
        except MemoryGovernanceError as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "active_release_invalid",
                    "message": str(exc),
                }
            )
        try:
            candidates = load_candidate_events(state_root)
            seen: set[str] = set()
            for event in candidates:
                candidate_id = str(event.get("candidate_id", ""))
                if candidate_id in seen:
                    issues.append(
                        {
                            "severity": "warning",
                            "code": "duplicate_candidate_event",
                            "candidate_id": candidate_id,
                            "message": f"Candidate event repeats {candidate_id}",
                        }
                    )
                seen.add(candidate_id)
                issues.extend(lint_memory_records([event.get("record", {})]))
        except MemoryGovernanceError as exc:
            issues.append(
                {
                    "severity": "error",
                    "code": "candidate_log_invalid",
                    "message": str(exc),
                }
            )

    issues = sorted(
        issues,
        key=lambda item: (
            0 if item["severity"] == "error" else 1,
            item["code"],
            item.get("record_id", ""),
        ),
    )
    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "error_count": sum(issue["severity"] == "error" for issue in issues),
        "warning_count": sum(issue["severity"] == "warning" for issue in issues),
        "issues": issues,
    }


def write_run_artifact(
    state_root: str | Path,
    artifact: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write one immutable run artifact; an existing run is never replaced."""

    value = deepcopy(dict(artifact))
    value.setdefault("schema_version", "2.0")
    if not value.get("run_id"):
        value["run_id"] = f"run-{sha256_value(value)[:24]}"
    run_path = Path(state_root) / RUNS_DIR / f"{value['run_id']}.json"
    if run_path.exists():
        existing = read_json(run_path)
        if sha256_value(existing) != sha256_value(value):
            raise MemoryConflictError(f"Run artifact is immutable: {run_path}")
        return {"dry_run": dry_run, "path": str(run_path), "already_exists": True}
    if not dry_run:
        run_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = run_path.with_name(f".{run_path.name}.tmp-{uuid.uuid4().hex}")
        try:
            _write_json_file(temp_path, value)
            os.replace(temp_path, run_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    return {"dry_run": dry_run, "path": str(run_path), "already_exists": False}


__all__ = [
    "ACTIVE_POINTER",
    "CANDIDATE_LOG",
    "DEFAULT_LEGACY_MEMORY_PATH",
    "DEFAULT_REFERENCES_DIR",
    "DEFAULT_STATE_ROOT",
    "MemoryApprovalError",
    "MemoryConflictError",
    "MemoryGovernanceError",
    "activate_release",
    "append_candidates",
    "append_review_decision",
    "build_candidate_record",
    "canonicalize_english",
    "check_record_conflicts",
    "create_release",
    "file_sha256",
    "legacy_memory_stats",
    "lint_memory_records",
    "lint_resources",
    "list_releases",
    "load_active_pointer",
    "load_active_release",
    "load_candidate_events",
    "load_memory_records",
    "load_release",
    "normalize_source_text",
    "promote_candidates",
    "read_json",
    "read_jsonl",
    "render_cue_text",
    "resource_stats",
    "rollback_release",
    "sha256_value",
    "stable_record_id",
    "utc_now",
    "write_run_artifact",
]
