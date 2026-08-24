"""Governed PUREFAITH terminology loading, linting, and matching.

This module is intentionally standard-library only.  It treats the termbase as
versioned reference data and never learns or mutates terminology implicitly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping


SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_TERMBASE_PATH = SKILL_DIR / "references" / "glossary.json"


class TerminologyError(ValueError):
    """Raised when governed terminology cannot be loaded safely."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise TerminologyError(f"Termbase does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise TerminologyError(f"Invalid termbase JSON at {path}: {exc}") from exc


def load_termbase(path: str | Path | None = None, *, strict: bool = True) -> dict[str, Any]:
    """Load a governed termbase.

    Args:
        path: JSON path. Defaults to ``references/glossary.json``.
        strict: Raise ``TerminologyError`` when lint finds an error.

    Returns:
        A dictionary with an ``entries`` list.  The returned value is detached
        from the source file and may be safely inspected by callers.
    """

    termbase_path = Path(path) if path is not None else DEFAULT_TERMBASE_PATH
    data = _read_json(termbase_path)
    if not isinstance(data, dict):
        raise TerminologyError(f"Termbase root must be an object: {termbase_path}")
    issues = lint_termbase(data)
    errors = [issue for issue in issues if issue["severity"] == "error"]
    if strict and errors:
        summary = "; ".join(issue["message"] for issue in errors[:5])
        raise TerminologyError(f"Invalid termbase {termbase_path}: {summary}")
    return data


def lint_termbase(termbase: Mapping[str, Any] | str | Path) -> list[dict[str, Any]]:
    """Return deterministic lint findings without modifying the termbase."""

    if isinstance(termbase, (str, Path)):
        data = _read_json(Path(termbase))
    else:
        data = dict(termbase)

    issues: list[dict[str, Any]] = []

    def add(
        severity: str,
        code: str,
        message: str,
        *,
        term_id: str | None = None,
    ) -> None:
        issue: dict[str, Any] = {
            "severity": severity,
            "code": code,
            "message": message,
        }
        if term_id:
            issue["term_id"] = term_id
        issues.append(issue)

    if data.get("schema_version") != "1.0":
        add("error", "unsupported_schema", "schema_version must be 1.0")
    if not str(data.get("termbase_id", "")).strip():
        add("error", "missing_termbase_id", "termbase_id is required")
    if not str(data.get("version", "")).strip():
        add("error", "missing_version", "version is required")

    entries = data.get("entries")
    if not isinstance(entries, list):
        add("error", "entries_not_list", "entries must be a list")
        return issues

    seen_ids: set[str] = set()
    source_to_term: dict[str, str] = {}
    for position, raw_entry in enumerate(entries, 1):
        if not isinstance(raw_entry, dict):
            add("error", "entry_not_object", f"entry {position} must be an object")
            continue
        term_id = str(raw_entry.get("term_id", "")).strip()
        if not term_id:
            add("error", "missing_term_id", f"entry {position} has no term_id")
            continue
        if term_id in seen_ids:
            add("error", "duplicate_term_id", f"duplicate term_id {term_id}", term_id=term_id)
        seen_ids.add(term_id)

        source = raw_entry.get("source")
        target = raw_entry.get("target")
        if not isinstance(source, dict):
            add("error", "invalid_source", "source must be an object", term_id=term_id)
            continue
        if not isinstance(target, dict):
            add("error", "invalid_target", "target must be an object", term_id=term_id)
            continue

        canonical = str(source.get("canonical", "")).strip()
        preferred = str(target.get("preferred", "")).strip()
        if not canonical:
            add("error", "missing_source", "source.canonical is required", term_id=term_id)
        if not preferred:
            add("error", "missing_target", "target.preferred is required", term_id=term_id)

        aliases = source.get("aliases", [])
        if not isinstance(aliases, list) or any(not str(value).strip() for value in aliases):
            add("error", "invalid_aliases", "source.aliases must contain non-empty strings", term_id=term_id)
            aliases = []

        forms = [canonical, *(str(value).strip() for value in aliases)]
        if len(set(forms)) != len(forms):
            add("warning", "duplicate_source_form", "canonical source and aliases overlap", term_id=term_id)
        for form in forms:
            if not form:
                continue
            prior = source_to_term.get(form)
            if prior and prior != term_id:
                add(
                    "error",
                    "source_form_conflict",
                    f"source form {form!r} is owned by both {prior} and {term_id}",
                    term_id=term_id,
                )
            source_to_term[form] = term_id

        allowed = target.get("allowed", [])
        forbidden = target.get("forbidden", [])
        if not isinstance(allowed, list) or not isinstance(forbidden, list):
            add("error", "invalid_target_variants", "allowed and forbidden must be lists", term_id=term_id)
            continue
        normalized_allowed = {str(value).strip().casefold() for value in allowed}
        normalized_forbidden = {str(value).strip().casefold() for value in forbidden}
        preferred_key = preferred.casefold()
        if preferred_key in normalized_forbidden:
            add("error", "preferred_forbidden", "preferred target is also forbidden", term_id=term_id)
        overlap = normalized_allowed & normalized_forbidden
        if overlap:
            add(
                "error",
                "allowed_forbidden_overlap",
                f"allowed and forbidden targets overlap: {sorted(overlap)}",
                term_id=term_id,
            )

        status = raw_entry.get("status")
        if status not in {"candidate", "confirmed", "deprecated"}:
            add("error", "invalid_status", f"unsupported status {status!r}", term_id=term_id)
        evidence = raw_entry.get("evidence")
        if status == "confirmed" and (not isinstance(evidence, list) or not evidence):
            add("error", "confirmed_without_evidence", "confirmed terms require evidence", term_id=term_id)

    return sorted(
        issues,
        key=lambda item: (
            0 if item["severity"] == "error" else 1,
            item["code"],
            item.get("term_id", ""),
        ),
    )


def _domain_matches(entry_domains: Iterable[str], requested: set[str]) -> bool:
    if not requested:
        return True
    return bool(set(entry_domains) & requested)


def match_terms(
    text: str,
    termbase: Mapping[str, Any] | str | Path | None = None,
    *,
    domains: Iterable[str] | None = None,
    include_candidates: bool = False,
    longest_only: bool = True,
) -> list[dict[str, Any]]:
    """Match governed Chinese terms with deterministic longest-first handling.

    The function returns offsets into the original Python string.  Overlapping
    shorter matches are suppressed by default, which prevents ``扶手`` from
    competing with a longer confirmed compound when both are present.
    """

    if termbase is None:
        data = load_termbase()
    elif isinstance(termbase, (str, Path)):
        data = load_termbase(termbase)
    else:
        data = dict(termbase)
        errors = [
            issue for issue in lint_termbase(data) if issue["severity"] == "error"
        ]
        if errors:
            raise TerminologyError(errors[0]["message"])

    requested_domains = {str(value) for value in (domains or []) if str(value)}
    candidates: list[dict[str, Any]] = []
    for entry in data.get("entries", []):
        status = entry.get("status")
        if status == "deprecated":
            continue
        if status == "candidate" and not include_candidates:
            continue
        if not _domain_matches(entry.get("domain", []), requested_domains):
            continue
        source = entry["source"]
        forms = [source["canonical"], *source.get("aliases", [])]
        for form in sorted(set(forms), key=lambda value: (-len(value), value)):
            start = 0
            while True:
                position = text.find(form, start)
                if position < 0:
                    break
                candidates.append(
                    {
                        "term_id": entry["term_id"],
                        "source_text": form,
                        "canonical_source": source["canonical"],
                        "preferred_en": entry["target"]["preferred"],
                        "allowed_en": list(entry["target"].get("allowed", [])),
                        "forbidden_en": list(entry["target"].get("forbidden", [])),
                        "start": position,
                        "end": position + len(form),
                        "status": status,
                        "domains": list(entry.get("domain", [])),
                        "termbase_id": data.get("termbase_id"),
                        "termbase_version": data.get("version"),
                    }
                )
                start = position + max(len(form), 1)

    candidates.sort(
        key=lambda item: (
            item["start"],
            -(item["end"] - item["start"]),
            item["term_id"],
        )
    )
    if not longest_only:
        return candidates

    selected: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for item in candidates:
        span = (item["start"], item["end"])
        if any(span[0] < used[1] and used[0] < span[1] for used in occupied):
            continue
        selected.append(item)
        occupied.append(span)
    return selected


def termbase_stats(termbase: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    """Return simple governed termbase coverage statistics."""

    if isinstance(termbase, (str, Path)):
        data = load_termbase(termbase, strict=False)
    else:
        data = dict(termbase)
    entries = [entry for entry in data.get("entries", []) if isinstance(entry, dict)]
    status_counts: dict[str, int] = {}
    domain_counts: dict[str, int] = {}
    for entry in entries:
        status = str(entry.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1
        for domain in entry.get("domain", []):
            key = str(domain)
            domain_counts[key] = domain_counts.get(key, 0) + 1
    issues = lint_termbase(data)
    return {
        "termbase_id": data.get("termbase_id"),
        "version": data.get("version"),
        "entry_count": len(entries),
        "status_counts": dict(sorted(status_counts.items())),
        "domain_counts": dict(sorted(domain_counts.items())),
        "error_count": sum(issue["severity"] == "error" for issue in issues),
        "warning_count": sum(issue["severity"] == "warning" for issue in issues),
    }


__all__ = [
    "DEFAULT_TERMBASE_PATH",
    "TerminologyError",
    "lint_termbase",
    "load_termbase",
    "match_terms",
    "termbase_stats",
]
