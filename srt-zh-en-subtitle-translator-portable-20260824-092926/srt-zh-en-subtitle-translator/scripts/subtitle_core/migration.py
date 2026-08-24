"""Non-destructive legacy translation-memory migration.

The migration reads the 108-row legacy seed and optionally enriches spans from
the corpus ``dev`` partition.  Holdout entries are never opened or imported.
Every migrated row is tagged ``legacy_unverified`` and retrieval-ineligible.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .memory import (
    DEFAULT_LEGACY_MEMORY_PATH,
    DEFAULT_REFERENCES_DIR,
    DEFAULT_STATE_ROOT,
    MemoryConflictError,
    MemoryGovernanceError,
    canonicalize_english,
    check_record_conflicts,
    create_release,
    file_sha256,
    lint_memory_records,
    normalize_source_text,
    normalize_space,
    read_json,
    render_cue_text,
    sha256_value,
    utc_now,
    write_run_artifact,
)


_LOWERCASE_START_RE = re.compile(r"^[a-z]")
_CONNECTOR_START_RE = re.compile(
    r"^(?:and|but|or|that|when|while|to|for|by|on|our|was|will|right|"
    r"into|even|considering|including|continuously|truth be told)\b",
    re.IGNORECASE,
)
_FULLWIDTH_ENGLISH_PUNCTUATION_RE = re.compile(r"[，。！？；：]")
_TERMINAL_ENGLISH_RE = re.compile(r"[.!?](?:[\"'”’])?$")


# These are audit observations, not silently corrected translations.  They keep
# suspect legacy evidence quarantined for explicit re-review.
LEGACY_AUDIT_FLAGS: dict[int, list[tuple[str, str]]] = {
    11: [("awkward_call_to_action", "Conflicts with the governed PUREFAITH CTA")],
    12: [("english_fluency_suspected", "Awkward phrase: joined ... in the beginning")],
    13: [("english_grammar_suspected", "Tense and even/no construction need re-review")],
    14: [("fragment_dependency", "At that time/context is not self-contained")],
    18: [("english_grammar_suspected", "Demand/supply construction is ungrammatical")],
    20: [("english_grammar_suspected", "Missing article before production side")],
    37: [("english_collocation_suspected", "make jokes on me is not the intended collocation")],
    38: [("speaker_wording_suspected", "lady boss is speaker-sensitive and unnatural")],
    46: [("english_grammar_suspected", "me and my family needs re-review")],
    49: [("english_fluency_suspected", "chosen to be stable is unnatural")],
    57: [("english_typography_error", "Full-width comma appears in English")],
    58: [
        ("english_style_suspected", "Brochure-like seating-manufacturing wording"),
        ("unicode_hyphen_inconsistent", "Non-breaking hyphen appears in English"),
    ],
    62: [("english_grammar_suspected", "line-up is incorrectly hyphenated as a verb")],
    71: [("semantic_alignment_suspected", "One-year steel supply is absent from target")],
    72: [("english_fluency_suspected", "within my ability is unnatural in context")],
    75: [("english_grammar_suspected", "on one hand needs re-review")],
    79: [("english_fluency_suspected", "Redundant remember/very clearly wording")],
    80: [("cross_cue_redistribution", "Location cue contains workshop-rental target content")],
    81: [("cross_cue_redistribution", "Workshop-rental cue contains location target content")],
    83: [("english_typography_error", "Full-width comma appears in English")],
    85: [("english_typography_error", "Full-width comma appears in English")],
    86: [("english_fluency_suspected", "every time when is redundant")],
    99: [("context_polarity_dependency", "No-odor target depends on omitted preceding cue")],
    108: [("english_spacing_error", "Missing space after possessive apostrophe")],
}


def _split_srt_blocks(text: str) -> list[list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not normalized:
        return []
    return [block.split("\n") for block in re.split(r"\n{2,}", normalized)]


def _read_srt_evidence(path: Path, *, document_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for lines in _split_srt_blocks(path.read_text(encoding="utf-8-sig")):
        if len(lines) < 3 or not lines[0].strip().isdigit():
            continue
        rows.append(
            {
                "partition": "dev",
                "document_id": document_id,
                "cue_index": int(lines[0].strip()),
                "timing": lines[1].strip(),
                "text": normalize_space(" ".join(lines[2:])),
                "artifact": str(path),
            }
        )
    return rows


def _read_review_evidence(path: Path, *, document_id: str) -> list[dict[str, Any]]:
    try:
        import openpyxl
    except ImportError:
        return []
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = workbook.active
    raw_rows = list(sheet.iter_rows(values_only=True))
    header_index = None
    for position, row in enumerate(raw_rows[:15]):
        normalized = {normalize_space(value).casefold() for value in row if value is not None}
        if normalized & {"序号", "index", "number", "no"} and normalized & {
            "中文",
            "chinese",
            "source",
        }:
            header_index = position
            break
    if header_index is None:
        return []
    headers = [normalize_space(value).casefold() for value in raw_rows[header_index]]

    def column(*names: str) -> int | None:
        for name in names:
            try:
                return headers.index(name.casefold())
            except ValueError:
                continue
        return None

    index_column = column("序号", "index", "number", "no")
    source_column = column("中文", "chinese", "source")
    timing_column = column("时间线", "timeline", "timestamp", "timing")
    if index_column is None or source_column is None:
        return []
    rows: list[dict[str, Any]] = []
    for row in raw_rows[header_index + 1 :]:
        index_value = row[index_column] if index_column < len(row) else None
        source_value = row[source_column] if source_column < len(row) else None
        if index_value is None or not normalize_space(source_value):
            continue
        try:
            cue_index = int(float(index_value))
        except (TypeError, ValueError):
            continue
        timing = ""
        if timing_column is not None and timing_column < len(row):
            timing = normalize_space(row[timing_column])
        rows.append(
            {
                "partition": "dev",
                "document_id": document_id,
                "cue_index": cue_index,
                "timing": timing,
                "text": normalize_space(source_value),
                "artifact": str(path),
            }
        )
    return rows


def load_dev_corpus_evidence(
    corpus_root: str | Path,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Index only the manifest's dev partition; holdout is never traversed."""

    root = Path(corpus_root)
    manifest_path = root / "manifest.json"
    manifest = read_json(manifest_path)
    partitions = manifest.get("partitions", {})
    dev_items = partitions.get("dev", [])
    if not isinstance(dev_items, list):
        raise MemoryGovernanceError("Corpus manifest partitions.dev must be a list")

    evidence: dict[str, list[dict[str, Any]]] = {}
    documents_scanned = 0
    rows_scanned = 0
    skipped_documents: list[str] = []
    for item in dev_items:
        if not isinstance(item, Mapping):
            continue
        document_id = normalize_space(item.get("id", ""))
        relative_directory = Path(str(item.get("directory", "")))
        # This guard is deliberately strict: migration must never ingest holdout.
        if not relative_directory.parts or relative_directory.parts[0].casefold() != "dev":
            raise MemoryGovernanceError(
                f"Non-dev corpus path rejected during migration: {relative_directory}"
            )
        document_dir = (root / relative_directory).resolve()
        try:
            document_dir.relative_to((root / "dev").resolve())
        except ValueError as exc:
            raise MemoryGovernanceError(
                f"Corpus path escapes dev partition: {relative_directory}"
            ) from exc

        source_srt = document_dir / "source.zh.srt"
        if source_srt.exists():
            rows = _read_srt_evidence(source_srt, document_id=document_id)
        else:
            approved_review = document_dir / "review.approved.xlsx"
            regular_review = document_dir / "review.xlsx"
            review_path = approved_review if approved_review.exists() else regular_review
            rows = (
                _read_review_evidence(review_path, document_id=document_id)
                if review_path.exists()
                else []
            )
        if not rows:
            skipped_documents.append(document_id)
            continue
        documents_scanned += 1
        rows_scanned += len(rows)
        for row in rows:
            key = normalize_source_text(row["text"])
            if key:
                evidence.setdefault(key, []).append(row)

    return evidence, {
        "manifest": str(manifest_path),
        "partition": "dev",
        "documents_scanned": documents_scanned,
        "rows_scanned": rows_scanned,
        "skipped_documents": skipped_documents,
        "holdout_documents_opened": 0,
    }


def _looks_like_fragment(final_en: str, example: Mapping[str, Any]) -> bool:
    text = normalize_space(final_en)
    return bool(
        example.get("context_before")
        or example.get("context_after")
        or _LOWERCASE_START_RE.search(text)
        or _CONNECTOR_START_RE.search(text)
    )


def _automatic_quality_observations(
    final_en: str,
    example: Mapping[str, Any],
) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []

    def add(code: str, detail: str) -> None:
        if not any(item["code"] == code for item in observations):
            observations.append({"code": code, "detail": detail})

    if not normalize_space(example.get("draft_en", "")):
        add("legacy_draft_missing", "Legacy example has no draft English")
    if not example.get("context_before") or not example.get("context_after"):
        add("legacy_context_incomplete", "One or both adjacent contexts are unavailable")
    if _looks_like_fragment(final_en, example):
        add("canonical_fragment_unresolved", "Legacy evidence is not a verified complete thought")
    if _FULLWIDTH_ENGLISH_PUNCTUATION_RE.search(final_en):
        add("fullwidth_punctuation_in_english", "Full-width punctuation appears in English")
    if "\u2011" in final_en:
        add("nonbreaking_hyphen_in_english", "Non-breaking hyphen appears in English")
    if "employees'health" in final_en:
        add("apostrophe_spacing_error", "Missing space after possessive apostrophe")
    return observations


def _legacy_record_id(
    source_sha256: str,
    legacy_index: int,
    example: Mapping[str, Any],
) -> str:
    identity = {
        "source_sha256": source_sha256,
        "legacy_index": legacy_index,
        "zh": normalize_space(example.get("zh", "")),
        "draft_en": normalize_space(example.get("draft_en", "")),
        "final_en": normalize_space(example.get("final_en", "")),
        "context_before": normalize_space(example.get("context_before", "")),
        "context_after": normalize_space(example.get("context_after", "")),
    }
    return f"legacy-{sha256_value(identity)[:24]}"


def migrate_legacy_records(
    legacy_data: Mapping[str, Any],
    *,
    source_path: str | Path,
    source_sha256: str,
    corpus_evidence: Mapping[str, list[dict[str, Any]]] | None = None,
    policy_versions: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Transform legacy examples in memory without writing any files."""

    examples = legacy_data.get("examples")
    if not isinstance(examples, list):
        raise MemoryGovernanceError("Legacy translation memory examples must be a list")
    evidence_index = corpus_evidence or {}
    records: list[dict[str, Any]] = []
    source_to_ids: dict[str, list[str]] = {}

    for legacy_index, raw_example in enumerate(examples, 1):
        if not isinstance(raw_example, Mapping):
            raise MemoryGovernanceError(f"Legacy example {legacy_index} is not an object")
        example = dict(raw_example)
        zh = normalize_space(example.get("zh", ""))
        final_en = normalize_space(example.get("final_en", ""))
        if not zh or not final_en:
            raise MemoryGovernanceError(
                f"Legacy example {legacy_index} lacks zh or final_en"
            )
        is_fragment = _looks_like_fragment(final_en, example)
        observations = _automatic_quality_observations(final_en, example)
        for code, detail in LEGACY_AUDIT_FLAGS.get(legacy_index, []):
            if not any(item["code"] == code for item in observations):
                observations.append({"code": code, "detail": detail})

        source_key = normalize_source_text(zh)
        spans = [deepcopy(span) for span in evidence_index.get(source_key, [])]
        unique_locations = {
            (span["document_id"], span["cue_index"], span.get("timing", ""))
            for span in spans
        }
        if len(unique_locations) > 1:
            observations.append(
                {
                    "code": "corpus_span_ambiguous",
                    "detail": "Exact Chinese text appears in multiple dev corpus locations",
                }
            )
        document_id = None
        cue_indexes: list[int] = []
        if len(unique_locations) == 1:
            document_id, cue_index, _ = next(iter(unique_locations))
            cue_indexes = [int(cue_index)]

        canonical = canonicalize_english(
            final_en,
            complete_thought=not is_fragment,
        )
        record_id = _legacy_record_id(source_sha256, legacy_index, example)
        flags = sorted({item["code"] for item in observations})
        before = normalize_space(example.get("context_before", ""))
        after = normalize_space(example.get("context_after", ""))
        record = {
            "schema_version": "2.0",
            "record_id": record_id,
            "status": "legacy_unverified",
            "source": {
                "language": "zh-CN",
                "text": zh,
                "normalized_text": source_key,
                "span": {
                    "kind": "cue_fragment" if is_fragment else "legacy_unknown",
                    "document_id": document_id,
                    "sentence_group_id": None,
                    "cue_indexes": cue_indexes,
                    "legacy_example_index": legacy_index,
                },
                "context_before": [before] if before else [],
                "context_after": [after] if after else [],
                "context_links": {
                    "before_record_ids": [],
                    "after_record_ids": [],
                },
                "evidence_spans": spans,
            },
            "target": {
                "language": "en",
                "locale": "en-US",
                "canonical_text": canonical,
                "rendered_cues": [
                    {
                        "cue_index": cue_indexes[0] if len(cue_indexes) == 1 else None,
                        "text": render_cue_text(final_en),
                    }
                ],
                "canonicalization_status": (
                    "legacy_fragment_unresolved"
                    if is_fragment
                    else "punctuation_normalized_unverified"
                ),
            },
            "classification": {
                "brand": "PUREFAITH",
                "domains": [],
                "speaker_role": "unknown",
                "perspective": "unknown",
            },
            "provenance": {
                "origin": "legacy_translation_memory",
                "source_path": str(Path(source_path)),
                "source_sha256": source_sha256,
                "legacy_example_index": legacy_index,
                "legacy_draft_en": normalize_space(example.get("draft_en", "")),
                "corpus_partition": "dev" if spans else None,
            },
            "approval": {
                "status": "legacy_unverified",
            },
            "quality": {
                "flags": flags,
                "observations": sorted(observations, key=lambda item: item["code"]),
                "semantic_alignment": "unknown",
            },
            "policy_versions": dict(policy_versions or {}),
            "lifecycle": {
                "created_at": "legacy-unknown",
                "supersedes": [],
            },
            "retrieval": {
                "eligible": False,
                "reasons": ["legacy_unverified", *flags],
            },
        }
        records.append(record)
        source_to_ids.setdefault(source_key, []).append(record_id)

    # Resolve context strings to IDs where exact evidence exists.  Multiple IDs
    # are retained rather than guessed away.
    for record in records:
        source = record["source"]
        before_ids: list[str] = []
        after_ids: list[str] = []
        for text in source["context_before"]:
            before_ids.extend(source_to_ids.get(normalize_source_text(text), []))
        for text in source["context_after"]:
            after_ids.extend(source_to_ids.get(normalize_source_text(text), []))
        source["context_links"] = {
            "before_record_ids": sorted(set(before_ids)),
            "after_record_ids": sorted(set(after_ids)),
        }

    # Mark all normalized-source conflicts on the records themselves.
    conflicts = check_record_conflicts(records)
    conflict_ids: set[str] = set()
    for issue in conflicts:
        if issue["code"] == "source_target_conflict":
            conflict_ids.update(issue.get("record_ids", []))
    for record in records:
        if record["record_id"] in conflict_ids:
            if "source_target_conflict" not in record["quality"]["flags"]:
                record["quality"]["flags"].append("source_target_conflict")
                record["quality"]["flags"].sort()
            if "source_target_conflict" not in record["retrieval"]["reasons"]:
                record["retrieval"]["reasons"].append("source_target_conflict")

    flag_counts = Counter(
        flag for record in records for flag in record["quality"]["flags"]
    )
    corpus_matches = sum(bool(record["source"]["evidence_spans"]) for record in records)
    summary = {
        "legacy_example_count": len(examples),
        "migrated_record_count": len(records),
        "legacy_unverified_count": sum(
            record["status"] == "legacy_unverified" for record in records
        ),
        "retrieval_eligible_count": sum(
            bool(record["retrieval"]["eligible"]) for record in records
        ),
        "corpus_matched_record_count": corpus_matches,
        "context_linked_record_count": sum(
            bool(record["source"]["context_links"]["before_record_ids"])
            or bool(record["source"]["context_links"]["after_record_ids"])
            for record in records
        ),
        "conflict_group_count": sum(
            issue["code"] == "source_target_conflict" for issue in conflicts
        ),
        "quality_flag_counts": dict(sorted(flag_counts.items())),
    }
    return records, summary


def migrate_legacy_memory(
    legacy_path: str | Path = DEFAULT_LEGACY_MEMORY_PATH,
    state_root: str | Path = DEFAULT_STATE_ROOT,
    *,
    corpus_root: str | Path | None = None,
    style_profile_path: str | Path | None = None,
    termbase_path: str | Path | None = None,
    activate: bool = False,
    actor_id: str = "legacy-migration",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan or apply a non-destructive governed migration.

    ``dry_run`` defaults to ``True``.  The legacy source checksum is verified
    again after release creation to prove that migration did not mutate it.
    """

    started_at = utc_now()
    legacy = Path(legacy_path)
    root = Path(state_root)
    source_hash_before = file_sha256(legacy)
    legacy_data = read_json(legacy)
    style_path = (
        Path(style_profile_path)
        if style_profile_path is not None
        else DEFAULT_REFERENCES_DIR / "style-profile.json"
    )
    terms_path = (
        Path(termbase_path)
        if termbase_path is not None
        else DEFAULT_REFERENCES_DIR / "glossary.json"
    )
    style = read_json(style_path)
    termbase = read_json(terms_path)
    policy_versions = {
        "style_profile": style.get("version"),
        "termbase": termbase.get("version"),
    }

    corpus_evidence: dict[str, list[dict[str, Any]]] = {}
    corpus_summary: dict[str, Any] = {
        "partition": None,
        "documents_scanned": 0,
        "rows_scanned": 0,
        "holdout_documents_opened": 0,
    }
    if corpus_root is not None:
        corpus_evidence, corpus_summary = load_dev_corpus_evidence(corpus_root)

    records, migration_summary = migrate_legacy_records(
        legacy_data,
        source_path=legacy,
        source_sha256=source_hash_before,
        corpus_evidence=corpus_evidence,
        policy_versions=policy_versions,
    )
    if migration_summary["legacy_example_count"] != 108:
        raise MemoryGovernanceError(
            "Expected the governed seed to contain exactly 108 legacy examples; "
            f"found {migration_summary['legacy_example_count']}"
        )
    if migration_summary["retrieval_eligible_count"] != 0:
        raise MemoryGovernanceError(
            "Legacy migration attempted to enable unverified retrieval"
        )
    lint_issues = lint_memory_records(records)
    blocking = [issue for issue in lint_issues if issue["severity"] == "error"]
    if blocking:
        raise MemoryConflictError(
            f"Migrated records failed lint: {blocking[0]['code']}: {blocking[0]['message']}"
        )

    release = create_release(
        root,
        records,
        parent_release_id=None,
        source={
            "origin": "legacy_migration",
            "legacy_path": str(legacy),
            "legacy_sha256": source_hash_before,
            "corpus_partition": "dev" if corpus_root is not None else None,
            "holdout_ingested": False,
        },
        policy_versions=policy_versions,
        activate=activate,
        actor_id=actor_id,
        dry_run=dry_run,
        created_at=started_at,
    )
    source_hash_after = file_sha256(legacy)
    if source_hash_before != source_hash_after:
        raise MemoryGovernanceError(
            "Legacy translation memory changed during migration; release is unsafe"
        )

    completed_at = utc_now()
    result = {
        **migration_summary,
        "release_id": release["release_id"],
        "release_path": release["release_path"],
        "activated": release["activated"],
        "legacy_sha256_before": source_hash_before,
        "legacy_sha256_after": source_hash_after,
        "legacy_unchanged": source_hash_before == source_hash_after,
        "corpus": corpus_summary,
        "lint_error_count": 0,
        "lint_warning_count": sum(
            issue["severity"] == "warning" for issue in lint_issues
        ),
    }
    run_artifact = {
        "schema_version": "2.0",
        "run_id": "",
        "run_type": "legacy_migration",
        "started_at": started_at,
        "completed_at": completed_at,
        "dry_run": dry_run,
        "status": "planned" if dry_run else "succeeded",
        "inputs": {
            "legacy_path": str(legacy),
            "legacy_sha256": source_hash_before,
            "state_root": str(root),
            "corpus_root": str(Path(corpus_root)) if corpus_root is not None else None,
            "corpus_partition": "dev" if corpus_root is not None else None,
        },
        "result": result,
        "issues": lint_issues,
    }
    run_artifact["run_id"] = f"run-{sha256_value(run_artifact)[:24]}"
    if not dry_run:
        write_run_artifact(root, run_artifact)
    return {
        "dry_run": dry_run,
        "run_artifact": run_artifact,
        "result": result,
        "release": release,
    }


__all__ = [
    "LEGACY_AUDIT_FLAGS",
    "load_dev_corpus_evidence",
    "migrate_legacy_memory",
    "migrate_legacy_records",
]
