"""High-level deterministic subtitle preparation, application, review, and QA."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any

from .artifacts import (
    TranslationArtifact,
    load_translation_artifact,
    translation_template,
    validate_translation_binding,
)
from .constants import (
    COMMAND_REPORT_SCHEMA,
    PREPARED_SCHEMA,
    TOOL_NAME,
    TOOL_VERSION,
    VALIDATION_SCHEMA,
)
from .errors import ExitCode, SubtitleError
from .io_utils import (
    atomic_write_bytes,
    atomic_write_json,
    read_json,
    sha256_bytes,
    utc_now_iso,
)
from .retrieval import (
    load_glossary,
    load_memory_records,
    match_glossary,
    retrieve_examples,
)
from .review import (
    audit_review_table,
    load_review_table,
    review_export_rows,
    write_review_table,
)
from .srt import SrtDocument, encode_rendered_srt, parse_srt, parse_srt_bytes, render_srt
from .text import clean_subtitle_text, load_style_profile, validate_final_text


def _resource_descriptor(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "sha256": None}
    try:
        data = path.read_bytes()
    except OSError:
        return {"path": str(path), "exists": True, "sha256": None}
    return {"path": str(path), "exists": True, "sha256": sha256_bytes(data)}


def _preferred_phrase_matches(
    *,
    current: str,
    previous: str,
    following: str,
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return advisory phrase candidates without treating scope as proven.

    Lexical matching is deterministic. Semantic scope fields such as domain,
    speaker perspective, and polarity are surfaced for the translator to
    confirm; they are never silently treated as satisfied.
    """

    matches: list[dict[str, Any]] = []
    for position, phrase in enumerate(profile.get("preferred_phrases", [])):
        if not isinstance(phrase, dict):
            continue
        signals = phrase.get("zh_contains", [])
        if isinstance(signals, str):
            signals = [signals]
        signals = [str(value) for value in signals if str(value)]
        if not signals:
            continue
        scope = phrase.get("context_scope")
        scope = scope if isinstance(scope, dict) else {}
        try:
            cue_window = max(1, int(scope.get("cue_window", 3)))
        except (TypeError, ValueError):
            cue_window = 3
        window = current if cue_window == 1 else " ".join(
            value for value in (previous, current, following) if value
        )
        mode = str(phrase.get("match_mode") or "any_in_window")
        matched = [signal for signal in signals if signal in window]
        if mode == "all_in_window":
            lexical_match = len(matched) == len(signals)
        elif mode == "exact_or_contained":
            lexical_match = any(
                current.strip() == signal.strip()
                or signal.strip() in current
                or current.strip() in signal
                for signal in signals
                if signal.strip() and current.strip()
            )
            matched = [
                signal
                for signal in signals
                if signal.strip()
                and current.strip()
                and (
                    current.strip() == signal.strip()
                    or signal.strip() in current
                    or current.strip() in signal
                )
            ]
        else:
            lexical_match = bool(matched)
        if not lexical_match:
            continue
        phrase_id = (
            phrase.get("rule_id")
            or phrase.get("id")
            or f"profile-phrase-{position + 1}"
        )
        semantic_constraints = {
            key: value for key, value in scope.items() if key != "cue_window"
        }
        matches.append(
            {
                "id": str(phrase_id),
                "priority": int(phrase.get("priority") or 0),
                "match_mode": mode,
                "matched_signals": matched,
                "signals": signals,
                "preferred_en": str(phrase.get("preferred_en") or ""),
                "note": str(phrase.get("note") or ""),
                "context_scope": scope,
                "applicability": (
                    "lexical_match_requires_scope_confirmation"
                    if semantic_constraints
                    else "lexical_match"
                ),
            }
        )
    matches.sort(key=lambda item: (-item["priority"], item["id"]))
    return matches


def prepare_document(
    *,
    input_path: Path,
    output_path: Path,
    preferences_path: Path,
    glossary_path: Path,
    style_profile_path: Path,
    top_k: int,
    min_score: float,
) -> dict[str, Any]:
    source = parse_srt(input_path)
    profile = load_style_profile(style_profile_path)
    records = load_memory_records(preferences_path)
    glossary = load_glossary(glossary_path)

    cues: list[dict[str, Any]] = []
    matched_count = 0
    abstained_count = 0
    for position, cue in enumerate(source.cues):
        previous = source.cues[position - 1].text if position > 0 else ""
        following = source.cues[position + 1].text if position + 1 < len(source.cues) else ""
        context = " ".join(value for value in (previous, cue.text, following) if value)
        retrieval = retrieve_examples(
            query=cue.text,
            context=context,
            records=records,
            profile=profile,
            top_k=top_k,
            min_score=min_score,
        )
        if retrieval["status"] == "matched":
            matched_count += 1
        else:
            abstained_count += 1
        glossary_matches = match_glossary(context, glossary)
        phrase_matches = _preferred_phrase_matches(
            current=cue.text,
            previous=previous,
            following=following,
            profile=profile,
        )
        candidates = retrieval.get("candidates", [])
        cues.append(
            {
                "index": cue.index,
                "raw_index": cue.raw_index,
                "timing": cue.raw_timing,
                "start_ms": cue.start_ms,
                "end_ms": cue.end_ms,
                "duration_ms": cue.end_ms - cue.start_ms,
                "source_fingerprint": cue.fingerprint,
                "text": cue.text,
                "previous_text": previous,
                "next_text": following,
                "context_window": context,
                "retrieval": retrieval,
                "similar_approved_examples": candidates,
                "glossary_matches": glossary_matches,
                "preferred_phrase_matches": phrase_matches,
            }
        )

    payload = {
        "schema_version": PREPARED_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "source": source.source_manifest(),
        "cue_count": len(source.cues),
        "resources": {
            "style_profile": _resource_descriptor(style_profile_path),
            "translation_memory": _resource_descriptor(preferences_path),
            "glossary": _resource_descriptor(glossary_path),
        },
        "profile_name": profile.get("profile_name", "default"),
        "translation_principles": profile.get("translation_principles", []),
        "mandatory_process": [
            "Read the whole cue sequence before translating",
            "Reconstruct each complete thought across adjacent cues",
            "Translate each complete thought naturally",
            "Distribute the result back across the original cue indexes",
            "Use approved examples and glossary terms only when their meaning and perspective match",
            "Treat abstention or conflicts as a request for independent translation",
        ],
        "translation_contract": {
            "schema_version": translation_template(source)["schema_version"],
            "source": source.source_manifest(),
            "entry_fields": ["index", "source_fingerprint", "text"],
        },
        "retrieval_summary": {
            "memory_record_count": len(records),
            "glossary_term_count": len(glossary),
            "matched_cues": matched_count,
            "abstained_cues": abstained_count,
            "top_k": top_k,
            "min_score": min_score,
        },
        "warnings": list(source.warnings),
        "cues": cues,
    }
    atomic_write_json(output_path, payload)
    return payload


def _validated_clean_translations(
    *,
    source: SrtDocument,
    translations: dict[int, str],
    profile: dict[str, Any],
    english_only: bool = True,
) -> tuple[dict[int, str], list[dict[str, Any]]]:
    expected = [cue.index for cue in source.cues]
    expected_set = set(expected)
    actual_set = set(translations)
    missing = [index for index in expected if index not in actual_set]
    extra = sorted(actual_set - expected_set)
    if missing or extra:
        raise SubtitleError(
            "Translation indexes do not match the source",
            code="translation_index_mismatch",
            exit_code=ExitCode.INVALID_ARTIFACT,
            details={"missing": missing, "extra": extra},
        )

    clean: dict[int, str] = {}
    issues: list[dict[str, Any]] = []
    for cue in source.cues:
        text = clean_subtitle_text(translations[cue.index], profile)
        text_issues = validate_final_text(text, profile, english_only=english_only)
        for issue in text_issues:
            issues.append(
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "index": cue.index,
                }
            )
        clean[cue.index] = text
    if issues:
        raise SubtitleError(
            f"Final English failed validation with {len(issues)} problem(s)",
            code="final_text_validation_failed",
            exit_code=ExitCode.VALIDATION_FAILED,
            details={"issues": issues},
        )
    return clean, issues


def _compare_generated_structure(
    source: SrtDocument, generated: SrtDocument
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if len(source.cues) != len(generated.cues):
        issues.append(
            {
                "severity": "error",
                "code": "cue_count_changed",
                "expected": len(source.cues),
                "actual": len(generated.cues),
            }
        )
        return issues
    for original, translated in zip(source.cues, generated.cues):
        if original.index != translated.index or original.raw_index != translated.raw_index:
            issues.append(
                {
                    "severity": "error",
                    "code": "cue_number_changed",
                    "index": original.index,
                    "actual": translated.raw_index,
                }
            )
        if original.raw_timing != translated.raw_timing:
            issues.append(
                {
                    "severity": "error",
                    "code": "timing_changed",
                    "index": original.index,
                    "expected": original.raw_timing,
                    "actual": translated.raw_timing,
                }
            )
    return issues


def _publish_translations(
    *,
    source: SrtDocument,
    translations: dict[int, str],
    output_path: Path,
    profile: dict[str, Any],
    warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    clean, _ = _validated_clean_translations(
        source=source,
        translations=translations,
        profile=profile,
    )
    rendered = render_srt(source, clean)
    payload = encode_rendered_srt(source, rendered)
    generated = parse_srt_bytes(payload, source=f"<generated:{output_path}>")
    structure_issues = _compare_generated_structure(source, generated)
    if structure_issues:
        raise SubtitleError(
            "Generated SRT failed structural validation",
            code="generated_structure_invalid",
            exit_code=ExitCode.VALIDATION_FAILED,
            details={"issues": structure_issues},
        )
    # No output exists until every deterministic gate above has passed.
    atomic_write_bytes(output_path, payload)
    return {
        "schema_version": COMMAND_REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "ok": True,
        "passed": True,
        "output": str(output_path),
        "output_sha256": sha256_bytes(payload),
        "source_sha256": source.source_sha256,
        "cue_count": len(source.cues),
        "numbering_preserved": True,
        "timings_preserved": True,
        "encoding": source.encoding,
        "had_bom": source.had_bom,
        "newline": source.newline,
        "warnings": list(warnings or []) + list(source.warnings),
    }


def apply_translation_artifact(
    *,
    input_path: Path,
    translation_path: Path,
    output_path: Path,
    style_profile_path: Path,
    require_source_binding: bool,
    report_path: Path | None,
) -> dict[str, Any]:
    source = parse_srt(input_path)
    artifact = load_translation_artifact(translation_path)
    warnings = validate_translation_binding(
        artifact,
        source,
        require_binding=require_source_binding,
    )
    profile = load_style_profile(style_profile_path)
    report = _publish_translations(
        source=source,
        translations=artifact.translations,
        output_path=output_path,
        profile=profile,
        warnings=warnings,
    )
    report["translation_artifact"] = {
        "path": str(translation_path),
        "schema_version": artifact.schema_version,
        "source_bound": artifact.source is not None,
    }
    if report_path:
        atomic_write_json(report_path, report)
    return report


def export_review(
    *,
    input_path: Path,
    translation_path: Path,
    output_path: Path,
    style_profile_path: Path,
    require_source_binding: bool,
    report_path: Path | None,
) -> dict[str, Any]:
    source = parse_srt(input_path)
    artifact = load_translation_artifact(translation_path)
    warnings = validate_translation_binding(
        artifact,
        source,
        require_binding=require_source_binding,
    )
    profile = load_style_profile(style_profile_path)
    clean, _ = _validated_clean_translations(
        source=source,
        translations=artifact.translations,
        profile=profile,
    )
    rows = review_export_rows(source, clean)
    write_review_table(output_path, rows, source=source)
    report = {
        "schema_version": COMMAND_REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "ok": True,
        "passed": True,
        "output": str(output_path),
        "source_sha256": source.source_sha256,
        "cue_count": len(source.cues),
        "source_bound": artifact.source is not None,
        "warnings": warnings + list(source.warnings),
    }
    if report_path:
        atomic_write_json(report_path, report)
    return report


def audit_review(
    *,
    review_path: Path,
    report_path: Path,
    source_path: Path | None,
    clean_output_path: Path | None,
    style_profile_path: Path,
    strict: bool,
    sheet_name: str | None,
    header_row: int | None,
) -> dict[str, Any]:
    table = load_review_table(
        review_path,
        sheet_name=sheet_name,
        header_row=header_row,
    )
    source = parse_srt(source_path) if source_path else None
    profile = load_style_profile(style_profile_path)
    report, clean_rows, _ = audit_review_table(
        table,
        source=source,
        profile=profile,
        strict=strict,
    )
    atomic_write_json(report_path, report)
    if clean_output_path:
        write_review_table(clean_output_path, clean_rows, source=source)
    if not report["passed"]:
        raise SubtitleError(
            "Review audit found blocking issues",
            code="review_audit_blocked",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={
                "report": str(report_path),
                "errors": report["summary"]["errors"],
                "warnings": report["summary"]["warnings"],
            },
        )
    return report


def apply_review(
    *,
    input_path: Path,
    review_path: Path,
    output_path: Path,
    style_profile_path: Path,
    strict: bool,
    report_path: Path | None,
    sheet_name: str | None,
    header_row: int | None,
) -> dict[str, Any]:
    source = parse_srt(input_path)
    table = load_review_table(
        review_path,
        sheet_name=sheet_name,
        header_row=header_row,
    )
    profile = load_style_profile(style_profile_path)
    audit, _, translations = audit_review_table(
        table,
        source=source,
        profile=profile,
        strict=strict,
    )
    if not audit["passed"]:
        if report_path:
            atomic_write_json(
                report_path,
                {
                    "schema_version": COMMAND_REPORT_SCHEMA,
                    "ok": False,
                    "audit": audit,
                },
            )
        raise SubtitleError(
            "Reviewed table cannot be finalized because its audit failed",
            code="review_finalize_blocked",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={
                "issues": audit["issues"],
                "report": str(report_path) if report_path else None,
            },
        )
    report = _publish_translations(
        source=source,
        translations=translations,
        output_path=output_path,
        profile=profile,
        warnings=[
            issue
            for issue in audit["issues"]
            if issue.get("severity") == "warning"
        ],
    )
    report["review"] = {
        "path": str(review_path),
        "strict": strict,
        "audit_summary": audit["summary"],
    }
    if report_path:
        atomic_write_json(report_path, report)
    return report


def validate_srt_pair(
    *,
    original_path: Path,
    translated_path: Path,
    style_profile_path: Path,
    english_only: bool,
    report_path: Path | None,
) -> dict[str, Any]:
    original = parse_srt(original_path)
    translated = parse_srt(translated_path)
    profile = load_style_profile(style_profile_path)
    issues = _compare_generated_structure(original, translated)
    warnings = list(original.warnings) + list(translated.warnings)
    reading_speed_target = 20
    for cue in translated.cues:
        duration_ms = cue.end_ms - cue.start_ms
        character_count = len(" ".join(cue.text.split()))
        if english_only and duration_ms > 0:
            cps = character_count * 1000 / duration_ms
            if cps > reading_speed_target:
                warnings.append({
                    "code": "reading_speed_high",
                    "index": cue.index,
                    "duration_ms": duration_ms,
                    "character_count": character_count,
                    "characters_per_second": round(cps, 2),
                    "target_characters_per_second": reading_speed_target,
                })
        for issue in validate_final_text(cue.text, profile, english_only=english_only):
            issues.append(
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "message": issue.message,
                    "index": cue.index,
                }
            )
    report = {
        "schema_version": VALIDATION_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "passed": not issues,
        "original": str(original_path),
        "translated": str(translated_path),
        "source_sha256": original.source_sha256,
        "cue_count": len(original.cues),
        "issues": issues,
        "warnings": warnings,
    }
    if report_path:
        atomic_write_json(report_path, report)
    if issues:
        raise SubtitleError(
            f"Validation failed with {len(issues)} problem(s)",
            code="srt_validation_failed",
            exit_code=ExitCode.VALIDATION_FAILED,
            details={
                "issues": issues,
                "report": str(report_path) if report_path else None,
            },
        )
    return report


def create_simple_delivery(
    *,
    input_path: Path,
    translation_path: Path,
    output_dir: Path,
    style_profile_path: Path,
    report_path: Path | None,
) -> dict[str, Any]:
    """Create and verify the standard handoff for a simple translation task."""

    output_dir.mkdir(parents=True, exist_ok=True)
    final_srt = output_dir / "final.en.srt"
    bilingual_review = output_dir / "bilingual-review.xlsx"
    apply_report = output_dir / "apply-report.json"
    export_report = output_dir / "review-export.json"
    audit_report = output_dir / "review-audit.json"
    validation_report = output_dir / "final-validation.json"
    delivery_report = report_path or output_dir / "delivery-report.json"

    applied = apply_translation_artifact(
        input_path=input_path,
        translation_path=translation_path,
        output_path=final_srt,
        style_profile_path=style_profile_path,
        require_source_binding=True,
        report_path=apply_report,
    )
    exported = export_review(
        input_path=input_path,
        translation_path=translation_path,
        output_path=bilingual_review,
        style_profile_path=style_profile_path,
        require_source_binding=True,
        report_path=export_report,
    )
    audited = audit_review(
        review_path=bilingual_review,
        report_path=audit_report,
        source_path=input_path,
        clean_output_path=None,
        style_profile_path=style_profile_path,
        strict=True,
        sheet_name=None,
        header_row=None,
    )
    validated = validate_srt_pair(
        original_path=input_path,
        translated_path=final_srt,
        style_profile_path=style_profile_path,
        english_only=True,
        report_path=validation_report,
    )

    report = {
        "schema_version": COMMAND_REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "ok": True,
        "passed": True,
        "mode": "simple_delivery",
        "source": str(input_path),
        "translations": str(translation_path),
        "cue_count": validated["cue_count"],
        "source_bound": applied["translation_artifact"]["source_bound"],
        "review_columns": ["序号", "中文", "英文", "英文修改版", "时间线"],
        "deliverables": {
            "english_srt": str(final_srt),
            "bilingual_review_xlsx": str(bilingual_review),
            "apply_report": str(apply_report),
            "review_export_report": str(export_report),
            "review_audit_report": str(audit_report),
            "validation_report": str(validation_report),
        },
        "checks": {
            "translation_applied": bool(applied.get("passed")),
            "bilingual_table_exported": bool(exported.get("passed")),
            "review_table_audited": bool(audited.get("passed")),
            "srt_validated": bool(validated.get("passed")),
        },
    }
    atomic_write_json(delivery_report, report)
    report["report"] = str(delivery_report)
    return report


def extract_legacy(*, input_path: Path, output_path: Path) -> dict[str, Any]:
    source = parse_srt(input_path)
    items = [{"index": cue.index, "text": cue.text} for cue in source.cues]
    atomic_write_json(output_path, items)
    return {
        "schema_version": COMMAND_REPORT_SCHEMA,
        "ok": True,
        "passed": True,
        "output": str(output_path),
        "source_sha256": source.source_sha256,
        "cue_count": len(source.cues),
    }


def doctor(
    *,
    style_profile_path: Path,
    glossary_path: Path,
    preferences_path: Path,
    work_dir: Path,
    require_xlsx: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, message: str, **details: Any) -> None:
        checks.append(
            {
                "name": name,
                "status": status,
                "message": message,
                **details,
            }
        )

    version_ok = sys.version_info >= (3, 10)
    add(
        "python",
        "pass" if version_ok else "fail",
        platform.python_version(),
        executable=sys.executable,
        minimum="3.10",
    )

    try:
        profile = load_style_profile(style_profile_path)
        add("style_profile", "pass", str(profile.get("profile_name", "default")))
    except SubtitleError as exc:
        add("style_profile", "fail", exc.message, code=exc.code)

    try:
        glossary = load_glossary(glossary_path)
        add("glossary", "pass", f"{len(glossary)} term(s)")
    except SubtitleError as exc:
        add("glossary", "fail", exc.message, code=exc.code)

    try:
        records = load_memory_records(preferences_path)
        add("translation_memory", "pass", f"{len(records)} active record(s)")
    except SubtitleError as exc:
        add("translation_memory", "fail", exc.message, code=exc.code)

    xlsx_available = importlib.util.find_spec("openpyxl") is not None
    add(
        "openpyxl",
        "pass" if xlsx_available else ("fail" if require_xlsx else "warning"),
        "available" if xlsx_available else "not installed",
    )

    work_exists = work_dir.exists()
    writable = work_exists and os.access(work_dir, os.W_OK)
    add(
        "work_directory",
        "pass" if writable else "fail",
        str(work_dir),
        exists=work_exists,
        writable=writable,
    )

    failures = [check for check in checks if check["status"] == "fail"]
    report = {
        "schema_version": COMMAND_REPORT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "passed": not failures,
        "checks": checks,
    }
    if failures:
        raise SubtitleError(
            "Doctor found blocking environment or resource problems",
            code="doctor_failed",
            exit_code=ExitCode.CONFIG_INVALID,
            details={"report": report},
        )
    return report
