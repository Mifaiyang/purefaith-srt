"""Stable command-line interface for legacy workflows and v2 safety gates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback
from typing import Any, Callable

from .constants import DEFAULT_RETRIEVAL_MIN_SCORE, DEFAULT_RETRIEVAL_TOP_K
from .errors import ExitCode, SubtitleError
from .io_utils import atomic_write_json
from .pipeline import (
    apply_review,
    apply_translation_artifact,
    audit_review,
    create_simple_delivery,
    doctor,
    export_review,
    extract_legacy,
    prepare_document,
    validate_srt_pair,
)
from .srt import parse_srt


SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY = SKILL_DIR / "references" / "glossary.json"
DEFAULT_PREFERENCES = SKILL_DIR / "work" / "memory-v2"
DEFAULT_STYLE_PROFILE = SKILL_DIR / "references" / "style-profile.json"


def _path(value: str | Path) -> Path:
    return Path(value)


def _emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _add_style_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--style-profile", default=str(DEFAULT_STYLE_PROFILE))


def _add_report_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report", help="Write a structured JSON command report.")


def _add_review_location_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sheet", help="Review workbook sheet name.")
    parser.add_argument(
        "--header-row",
        type=int,
        help="One-based review header row; otherwise scan the first 100 rows.",
    )


def command_extract(args: argparse.Namespace) -> dict[str, Any]:
    return extract_legacy(input_path=_path(args.input), output_path=_path(args.output))


def command_prepare(args: argparse.Namespace) -> dict[str, Any]:
    payload = prepare_document(
        input_path=_path(args.input),
        output_path=_path(args.output),
        preferences_path=_path(args.preferences),
        glossary_path=_path(args.glossary),
        style_profile_path=_path(args.style_profile),
        top_k=args.top_k,
        min_score=args.min_score,
    )
    return {
        "ok": True,
        "passed": True,
        "output": str(args.output),
        "source_sha256": payload["source"]["sha256"],
        "cue_count": payload["cue_count"],
        "matched_cues": payload["retrieval_summary"]["matched_cues"],
        "abstained_cues": payload["retrieval_summary"]["abstained_cues"],
    }


def command_text(args: argparse.Namespace) -> None:
    source = parse_srt(_path(args.input))
    for cue in source.cues:
        print(f"{cue.index}. {cue.text.replace(chr(10), ' / ')}")
    return None


def command_apply(args: argparse.Namespace) -> dict[str, Any]:
    return apply_translation_artifact(
        input_path=_path(args.input),
        translation_path=_path(args.translations),
        output_path=_path(args.output),
        style_profile_path=_path(args.style_profile),
        require_source_binding=args.require_source_binding,
        report_path=_path(args.report) if args.report else None,
    )


def command_export_review(args: argparse.Namespace) -> dict[str, Any]:
    return export_review(
        input_path=_path(args.input),
        translation_path=_path(args.translations),
        output_path=_path(args.output),
        style_profile_path=_path(args.style_profile),
        require_source_binding=args.require_source_binding,
        report_path=_path(args.report) if args.report else None,
    )


def command_deliver(args: argparse.Namespace) -> dict[str, Any]:
    return create_simple_delivery(
        input_path=_path(args.input),
        translation_path=_path(args.translations),
        output_dir=_path(args.output_dir),
        style_profile_path=_path(args.style_profile),
        report_path=_path(args.report) if args.report else None,
    )


def command_audit_review(args: argparse.Namespace) -> dict[str, Any]:
    return audit_review(
        review_path=_path(args.review_table),
        report_path=_path(args.report),
        source_path=_path(args.source) if args.source else None,
        clean_output_path=_path(args.clean_output) if args.clean_output else None,
        style_profile_path=_path(args.style_profile),
        strict=args.strict,
        sheet_name=args.sheet,
        header_row=args.header_row,
    )


def command_apply_review(args: argparse.Namespace) -> dict[str, Any]:
    return apply_review(
        input_path=_path(args.input),
        review_path=_path(args.review_table),
        output_path=_path(args.output),
        style_profile_path=_path(args.style_profile),
        strict=args.strict,
        report_path=_path(args.report) if args.report else None,
        sheet_name=args.sheet,
        header_row=args.header_row,
    )


def command_finalize(args: argparse.Namespace) -> dict[str, Any]:
    return apply_review(
        input_path=_path(args.input),
        review_path=_path(args.review_table),
        output_path=_path(args.output),
        style_profile_path=_path(args.style_profile),
        strict=True,
        report_path=_path(args.report) if args.report else None,
        sheet_name=args.sheet,
        header_row=args.header_row,
    )


def command_validate(args: argparse.Namespace) -> dict[str, Any]:
    return validate_srt_pair(
        original_path=_path(args.original),
        translated_path=_path(args.translated),
        style_profile_path=_path(args.style_profile),
        english_only=args.english_only,
        report_path=_path(args.report) if args.report else None,
    )


def command_doctor(args: argparse.Namespace) -> dict[str, Any]:
    report = doctor(
        style_profile_path=_path(args.style_profile),
        glossary_path=_path(args.glossary),
        preferences_path=_path(args.preferences),
        work_dir=_path(args.work_dir),
        require_xlsx=args.require_xlsx,
    )
    report["ok"] = True
    return report


def command_learn(args: argparse.Namespace) -> dict[str, Any]:
    """Safe compatibility hook: learning can create candidates, never mutate a release."""

    from . import memory
    from .review import audit_review_table, load_review_table
    from .text import load_style_profile

    table = load_review_table(
        _path(args.review_table),
        sheet_name=args.sheet,
        header_row=args.header_row,
    )
    source = parse_srt(_path(args.source)) if args.source else None
    profile = load_style_profile(_path(args.style_profile))
    audit, clean_rows, _ = audit_review_table(
        table,
        source=source,
        profile=profile,
        strict=bool(args.source),
    )
    if not audit["passed"]:
        raise SubtitleError(
            "Learning candidates cannot be created from a failed review audit",
            code="learning_audit_failed",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"issues": audit["issues"]},
        )

    source_by_index = {cue.index: cue for cue in source.cues} if source else {}
    candidates: list[dict[str, Any]] = []
    for row in clean_rows:
        revised = str(row.get("英文修改版") or "").strip()
        draft = str(row.get("英文") or "").strip()
        if not revised or revised == draft:
            continue
        index = int(row["序号"])
        cue = source_by_index.get(index)
        zh = cue.text if cue else str(row.get("中文") or "")
        previous = ""
        following = ""
        if source and cue:
            position = list(source.cues).index(cue)
            previous = source.cues[position - 1].text if position > 0 else ""
            following = (
                source.cues[position + 1].text
                if position + 1 < len(source.cues)
                else ""
            )
        candidates.append(
            memory.build_candidate_record(
                zh,
                revised,
                rendered_cues=[{"cue_index": index, "text": revised}],
                source_span={
                    "kind": "complete_thought",
                    "document_id": source.source_sha256 if source else None,
                    "sentence_group_id": f"cue-{index}",
                    "cue_indexes": [index],
                },
                context_before=previous,
                context_after=following,
                provenance={
                    "review_table": str(args.review_table),
                    "source_sha256": source.source_sha256 if source else None,
                    "cue_index": index,
                },
                raw={"draft_en": draft, "approved_en": revised},
                quality_flags=["single_cue_candidate_requires_context_review"],
            )
        )
    summary = memory.append_candidates(
        _path(args.state_root),
        candidates,
        dry_run=args.dry_run,
    )
    report = {
        "ok": True,
        "passed": True,
        "mode": "candidate_only",
        "audit_summary": audit["summary"],
        **summary,
    }
    if args.report:
        atomic_write_json(_path(args.report), report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare, apply, review, learn safely from, and validate "
            "Chinese-to-English SRT subtitles."
        )
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Include an unexpected-error traceback on stderr.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    command = sub.add_parser("extract", help="Extract source cue text to legacy JSON.")
    command.add_argument("input")
    command.add_argument("output")
    command.set_defaults(func=command_extract)

    command = sub.add_parser(
        "prepare",
        help="Build a source-bound v2 translation context pack.",
    )
    command.add_argument("input")
    command.add_argument("output")
    command.add_argument("--preferences", default=str(DEFAULT_PREFERENCES))
    command.add_argument("--glossary", default=str(DEFAULT_GLOSSARY))
    _add_style_option(command)
    command.add_argument("--top-k", type=int, default=DEFAULT_RETRIEVAL_TOP_K)
    command.add_argument("--min-score", type=float, default=DEFAULT_RETRIEVAL_MIN_SCORE)
    command.set_defaults(func=command_prepare)

    command = sub.add_parser("text", help="Print cue numbers and source text.")
    command.add_argument("input")
    command.set_defaults(func=command_text)

    command = sub.add_parser(
        "apply",
        help="Validate and atomically apply translation JSON to the source SRT.",
    )
    command.add_argument("input")
    command.add_argument("translations")
    command.add_argument("output")
    _add_style_option(command)
    command.add_argument("--require-source-binding", action="store_true")
    _add_report_option(command)
    command.set_defaults(func=command_apply)

    command = sub.add_parser(
        "export-review",
        help="Export a source-bound CSV/TSV/XLSX human review table.",
    )
    command.add_argument("input")
    command.add_argument("translations")
    command.add_argument("output")
    _add_style_option(command)
    command.add_argument("--require-source-binding", action="store_true")
    _add_report_option(command)
    command.set_defaults(func=command_export_review)

    command = sub.add_parser(
        "deliver",
        help="Create a verified English SRT and bilingual XLSX for a simple task.",
    )
    command.add_argument("input")
    command.add_argument("translations")
    command.add_argument("output_dir")
    _add_style_option(command)
    _add_report_option(command)
    command.set_defaults(func=command_deliver)

    command = sub.add_parser(
        "audit-review",
        help="Audit a review table and optionally emit a clean source-bound copy.",
    )
    command.add_argument("review_table")
    command.add_argument("report")
    command.add_argument("--source", help="Original SRT used to bind and verify the review.")
    command.add_argument("--clean-output")
    command.add_argument("--strict", action="store_true")
    _add_style_option(command)
    _add_review_location_options(command)
    command.set_defaults(func=command_audit_review)

    command = sub.add_parser(
        "apply-review",
        help="Audit and apply a legacy or v2 reviewed table.",
    )
    command.add_argument("input")
    command.add_argument("review_table")
    command.add_argument("output")
    command.add_argument("--strict", action="store_true")
    _add_style_option(command)
    _add_report_option(command)
    _add_review_location_options(command)
    command.set_defaults(func=command_apply_review)

    command = sub.add_parser(
        "finalize",
        help="Strictly audit a source-bound review and atomically publish final SRT.",
    )
    command.add_argument("input")
    command.add_argument("review_table")
    command.add_argument("output")
    _add_style_option(command)
    _add_report_option(command)
    _add_review_location_options(command)
    command.set_defaults(func=command_finalize)

    command = sub.add_parser(
        "learn",
        aliases=["propose-learn"],
        help="Append pending learning candidates; never mutate active memory.",
    )
    command.add_argument("review_table")
    command.add_argument("--source")
    command.add_argument("--state-root", default="work/subtitle-memory")
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--preferences", default=str(DEFAULT_PREFERENCES))
    command.add_argument("--glossary", default=str(DEFAULT_GLOSSARY))
    _add_style_option(command)
    _add_report_option(command)
    _add_review_location_options(command)
    command.set_defaults(func=command_learn)

    command = sub.add_parser("validate", help="Validate final SRT structure and English.")
    command.add_argument("original")
    command.add_argument("translated")
    _add_style_option(command)
    command.add_argument(
        "--english-only",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    _add_report_option(command)
    command.set_defaults(func=command_validate)

    command = sub.add_parser(
        "doctor",
        help="Check runtime, resources, XLSX support, and writable workspace.",
    )
    command.add_argument("--require-xlsx", action="store_true")
    command.add_argument("--work-dir", default=".")
    command.add_argument("--preferences", default=str(DEFAULT_PREFERENCES))
    command.add_argument("--glossary", default=str(DEFAULT_GLOSSARY))
    _add_style_option(command)
    command.set_defaults(func=command_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            _emit(result)
        return int(ExitCode.OK)
    except SubtitleError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(exc.exit_code)
    except Exception as exc:  # defensive CLI boundary
        payload: dict[str, Any] = {
            "ok": False,
            "error": {
                "code": "internal_error",
                "message": str(exc),
                "type": type(exc).__name__,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        if getattr(args, "debug", False):
            traceback.print_exc()
        return int(ExitCode.INTERNAL_ERROR)
