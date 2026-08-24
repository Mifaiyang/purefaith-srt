"""Review-table I/O, strict source-aware auditing, and safe round-trips."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import csv
from io import BytesIO, StringIO
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable

from .constants import REVIEW_AUDIT_SCHEMA, REVIEW_SCHEMA, TOOL_NAME, TOOL_VERSION
from .errors import ExitCode, SubtitleError
from .io_utils import (
    atomic_write_bytes,
    atomic_write_text,
    read_decoded_text,
    read_json,
    utc_now_iso,
)
from .srt import SrtDocument
from .text import (
    CJK_RE,
    clean_subtitle_text,
    invalid_revised_reason,
    is_formula_like,
    normalize_space,
    remove_spreadsheet_guard,
    spreadsheet_safe_text,
    validate_final_text,
)


DISPLAY_HEADERS = ["序号", "中文", "英文", "英文修改版", "时间线"]
INTERNAL_HEADERS = ["__source_sha256", "__cue_fingerprint", "__schema_version"]
ALL_EXPORT_HEADERS = DISPLAY_HEADERS + INTERNAL_HEADERS

ALIASES: dict[str, set[str]] = {
    "index": {"序号", "index", "number", "no", "no.", "编号"},
    "zh": {"中文", "中文字幕", "chinese", "source", "source text", "原文"},
    "english": {
        "英文",
        "英文字幕",
        "英文初稿",
        "english",
        "translation",
        "draft english",
    },
    "revised": {
        "英文修改版",
        "英文修订版",
        "english revised",
        "revised english",
        "revision",
        "final english",
    },
    "timing": {"时间线", "时间轴", "timeline", "timestamp", "timing", "timecode"},
    "status": {"校对状态", "审核状态", "status", "review status"},
    "notes": {"修改意见", "备注", "修改意见/备注", "审核备注", "notes", "comment"},
    "source_sha256": {"__source_sha256", "source_sha256", "来源哈希"},
    "cue_fingerprint": {"__cue_fingerprint", "cue_fingerprint", "字幕指纹"},
    "schema_version": {"__schema_version", "schema_version"},
}


def normalized_header(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", "" if value is None else str(value)).strip(),
    ).casefold()


NORMALIZED_ALIASES = {
    field: {normalized_header(alias) for alias in aliases}
    for field, aliases in ALIASES.items()
}


def parse_review_index(value: Any, *, row_number: int) -> int:
    if isinstance(value, bool):
        raise SubtitleError(
            "Boolean is not a valid review cue index",
            code="invalid_review_index",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"row": row_number, "value": value},
        )
    if isinstance(value, int):
        index = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise SubtitleError(
                "Review cue index must be integral",
                code="invalid_review_index",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"row": row_number, "value": value},
            )
        index = int(value)
    elif isinstance(value, str):
        stripped = unicodedata.normalize("NFKC", value).strip()
        if not re.fullmatch(r"[+]?\d+(?:\.0+)?", stripped):
            raise SubtitleError(
                "Review cue index must be an integer",
                code="invalid_review_index",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"row": row_number, "value": value},
            )
        try:
            decimal = Decimal(stripped)
        except InvalidOperation as exc:
            raise SubtitleError(
                "Review cue index is invalid",
                code="invalid_review_index",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"row": row_number, "value": value},
            ) from exc
        if decimal != decimal.to_integral_value():
            raise SubtitleError(
                "Review cue index must be integral",
                code="invalid_review_index",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"row": row_number, "value": value},
            )
        index = int(decimal)
    else:
        raise SubtitleError(
            "Review cue index must be an integer",
            code="invalid_review_index",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"row": row_number, "value": repr(value)},
        )
    if index <= 0:
        raise SubtitleError(
            "Review cue index must be positive",
            code="invalid_review_index",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"row": row_number, "value": index},
        )
    return index


def _find_header_mapping(
    headers: Iterable[Any], *, reject_duplicates: bool = True
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    seen_headers: set[str] = set()
    for header_value in headers:
        header = "" if header_value is None else str(header_value).strip()
        normalized = normalized_header(header)
        if not normalized:
            continue
        if normalized in seen_headers:
            if reject_duplicates:
                raise SubtitleError(
                    f"Duplicate review header: {header}",
                    code="duplicate_review_header",
                    exit_code=ExitCode.REVIEW_BLOCKED,
                    details={"header": header},
                )
            continue
        seen_headers.add(normalized)
        for field, aliases in NORMALIZED_ALIASES.items():
            if normalized in aliases and field not in mapping:
                mapping[field] = header
                break
    return mapping


@dataclass(frozen=True)
class ReviewRow:
    physical_row: int
    values: dict[str, Any]
    raw_values: dict[str, Any]

    def get(self, field: str) -> str:
        value = self.values.get(field, "")
        if value is None:
            return ""
        return remove_spreadsheet_guard(str(value)).strip()

    @property
    def is_blank(self) -> bool:
        return not any(str(value).strip() for value in self.raw_values.values() if value is not None)


@dataclass(frozen=True)
class ReviewTable:
    path: str
    rows: tuple[ReviewRow, ...]
    metadata: dict[str, Any]
    headers: tuple[str, ...]
    header_row: int
    sheet_name: str | None = None


def _rows_from_dicts(
    raw_rows: list[dict[str, Any]],
    *,
    path: str,
    header_row: int,
    sheet_name: str | None,
    metadata: dict[str, Any] | None = None,
) -> ReviewTable:
    if not raw_rows:
        return ReviewTable(
            path=path,
            rows=(),
            metadata=metadata or {},
            headers=(),
            header_row=header_row,
            sheet_name=sheet_name,
        )
    ordered_headers: list[str] = []
    for row in raw_rows:
        for key in row:
            key_text = "" if key is None else str(key).strip()
            if key_text and key_text not in ordered_headers:
                ordered_headers.append(key_text)
    mapping = _find_header_mapping(ordered_headers)
    if "index" not in mapping or "english" not in mapping:
        raise SubtitleError(
            "Review table requires an index and English column",
            code="review_headers_missing",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={
                "path": path,
                "headers": ordered_headers,
                "recognized_fields": sorted(mapping),
            },
        )
    rows: list[ReviewRow] = []
    for offset, raw in enumerate(raw_rows, 1):
        values = {
            field: raw.get(header, "")
            for field, header in mapping.items()
        }
        review_row = ReviewRow(
            physical_row=header_row + offset,
            values=values,
            raw_values=dict(raw),
        )
        if not review_row.is_blank:
            rows.append(review_row)
    return ReviewTable(
        path=path,
        rows=tuple(rows),
        metadata=metadata or {},
        headers=tuple(ordered_headers),
        header_row=header_row,
        sheet_name=sheet_name,
    )


def _load_delimited(path: Path, *, delimiter: str) -> ReviewTable:
    decoded = read_decoded_text(path)
    reader = csv.DictReader(StringIO(decoded.text), delimiter=delimiter)
    if reader.fieldnames is None:
        raise SubtitleError(
            "Review table has no header row",
            code="review_headers_missing",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"path": str(path)},
        )
    raw_rows = [dict(row) for row in reader]
    return _rows_from_dicts(
        raw_rows,
        path=str(path),
        header_row=1,
        sheet_name=None,
    )


def _load_xlsx(
    path: Path,
    *,
    sheet_name: str | None,
    header_row: int | None,
) -> ReviewTable:
    try:
        import openpyxl
    except ImportError as exc:
        raise SubtitleError(
            "Reading .xlsx requires openpyxl",
            code="xlsx_dependency_missing",
            exit_code=ExitCode.DEPENDENCY_MISSING,
            details={"path": str(path)},
        ) from exc
    try:
        workbook = openpyxl.load_workbook(path, read_only=False, data_only=False)
    except Exception as exc:
        raise SubtitleError(
            f"Cannot open review workbook: {path}",
            code="xlsx_read_failed",
            exit_code=ExitCode.REVIEW_BLOCKED,
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        if sheet_name:
            if sheet_name not in workbook.sheetnames:
                raise SubtitleError(
                    f"Workbook sheet does not exist: {sheet_name}",
                    code="review_sheet_missing",
                    exit_code=ExitCode.REVIEW_BLOCKED,
                    details={"path": str(path), "sheet": sheet_name},
                )
            worksheet = workbook[sheet_name]
        elif "subtitle_review" in workbook.sheetnames:
            worksheet = workbook["subtitle_review"]
        else:
            worksheet = workbook.active

        metadata: dict[str, Any] = {}
        if "_metadata" in workbook.sheetnames:
            metadata_sheet = workbook["_metadata"]
            for row in metadata_sheet.iter_rows(min_row=1, max_col=2, values_only=True):
                if row[0] is not None:
                    metadata[str(row[0])] = row[1]

        rows = list(worksheet.iter_rows(values_only=True))
        if not rows:
            raise SubtitleError(
                "Review workbook is empty",
                code="empty_review_table",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"path": str(path), "sheet": worksheet.title},
            )
        selected_header = header_row
        if selected_header is None:
            scan_limit = min(len(rows), 100)
            for one_based, candidate in enumerate(rows[:scan_limit], 1):
                mapping = _find_header_mapping(candidate, reject_duplicates=False)
                if "index" in mapping and "english" in mapping:
                    selected_header = one_based
                    break
        if selected_header is None or selected_header < 1 or selected_header > len(rows):
            raise SubtitleError(
                "Could not locate review-table headers in the first 100 rows",
                code="review_headers_missing",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"path": str(path), "sheet": worksheet.title},
            )

        headers = [
            "" if value is None else str(value).strip()
            for value in rows[selected_header - 1]
        ]
        _find_header_mapping(headers)
        raw_rows: list[dict[str, Any]] = []
        for values in rows[selected_header:]:
            raw_rows.append(
                {
                    headers[position]: values[position] if position < len(values) else ""
                    for position in range(len(headers))
                    if headers[position]
                }
            )
        return _rows_from_dicts(
            raw_rows,
            path=str(path),
            header_row=selected_header,
            sheet_name=worksheet.title,
            metadata=metadata,
        )
    finally:
        workbook.close()


def load_review_table(
    path: Path,
    *,
    sheet_name: str | None = None,
    header_row: int | None = None,
) -> ReviewTable:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        return _load_delimited(path, delimiter=",")
    if suffix == ".tsv":
        return _load_delimited(path, delimiter="\t")
    if suffix == ".xlsx":
        return _load_xlsx(path, sheet_name=sheet_name, header_row=header_row)
    if suffix == ".json":
        data = read_json(path)
        metadata: dict[str, Any] = {}
        if isinstance(data, dict):
            metadata_raw = data.get("metadata", {})
            if isinstance(metadata_raw, dict):
                metadata = metadata_raw
            data = data.get("rows", data.get("items"))
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise SubtitleError(
                "Review JSON must contain a list of row objects",
                code="invalid_review_json",
                exit_code=ExitCode.REVIEW_BLOCKED,
                details={"path": str(path)},
            )
        return _rows_from_dicts(
            [dict(row) for row in data],
            path=str(path),
            header_row=1,
            sheet_name=None,
            metadata=metadata,
        )
    raise SubtitleError(
        f"Unsupported review table format: {path.suffix}",
        code="unsupported_review_format",
        exit_code=ExitCode.UNSUPPORTED,
        details={"path": str(path), "suffix": path.suffix},
    )


def review_export_rows(
    source: SrtDocument,
    translations: dict[int, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cue in source.cues:
        rows.append(
            {
                "序号": cue.index,
                "中文": cue.text,
                "英文": translations[cue.index],
                "英文修改版": "",
                "时间线": cue.raw_timing,
                "__source_sha256": source.source_sha256,
                "__cue_fingerprint": cue.fingerprint,
                "__schema_version": REVIEW_SCHEMA,
            }
        )
    return rows


def _write_delimited(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    delimiter: str,
) -> None:
    buffer = StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=ALL_EXPORT_HEADERS, delimiter=delimiter)
    writer.writeheader()
    for row in rows:
        safe = dict(row)
        for header in ("中文", "英文", "英文修改版"):
            safe[header] = spreadsheet_safe_text(str(safe.get(header, "")))
        writer.writerow({header: safe.get(header, "") for header in ALL_EXPORT_HEADERS})
    atomic_write_text(path, buffer.getvalue(), encoding="utf-8-sig")


def _write_xlsx(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    metadata: dict[str, Any],
) -> None:
    try:
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill, Protection
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise SubtitleError(
            "Writing .xlsx requires openpyxl",
            code="xlsx_dependency_missing",
            exit_code=ExitCode.DEPENDENCY_MISSING,
            details={"path": str(path)},
        ) from exc
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "subtitle_review"
    worksheet.append(ALL_EXPORT_HEADERS)
    for row in rows:
        worksheet.append([row.get(header, "") for header in ALL_EXPORT_HEADERS])
    for row_cells in worksheet.iter_rows(min_row=2):
        for position, cell in enumerate(row_cells, 1):
            if position in {2, 3, 4} and isinstance(cell.value, str):
                cell.data_type = "s"
            if position in {1, 2, 3, 5, 6, 7, 8}:
                cell.protection = Protection(locked=True)
            else:
                cell.protection = Protection(locked=False)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    for cell in worksheet[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    widths = [10, 42, 42, 42, 30, 15, 15, 15]
    for position, width in enumerate(widths, 1):
        worksheet.column_dimensions[get_column_letter(position)].width = width
    for position in range(6, 9):
        worksheet.column_dimensions[get_column_letter(position)].hidden = True
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = f"A1:E{max(1, worksheet.max_row)}"
    worksheet.protection.sheet = True
    worksheet.protection.enable()

    metadata_sheet = workbook.create_sheet("_metadata")
    metadata_sheet.sheet_state = "hidden"
    for key, value in metadata.items():
        metadata_sheet.append([key, value])

    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    atomic_write_bytes(path, buffer.getvalue())


def write_review_table(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    source: SrtDocument | None,
) -> None:
    suffix = path.suffix.casefold()
    row_source_hash = next(
        (
            str(row.get("__source_sha256") or "")
            for row in rows
            if row.get("__source_sha256")
        ),
        "",
    )
    metadata = {
        "schema_version": REVIEW_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "source_sha256": source.source_sha256 if source else row_source_hash,
        "source_cue_count": len(source.cues) if source else len(rows),
        "created_at": utc_now_iso(),
    }
    if suffix == ".csv":
        _write_delimited(path, rows, delimiter=",")
    elif suffix == ".tsv":
        _write_delimited(path, rows, delimiter="\t")
    elif suffix == ".xlsx":
        _write_xlsx(path, rows, metadata=metadata)
    else:
        raise SubtitleError(
            f"Review output must be .csv, .tsv, or .xlsx: {path}",
            code="unsupported_review_format",
            exit_code=ExitCode.UNSUPPORTED,
            details={"path": str(path)},
        )


@dataclass
class ResolvedReviewRow:
    index: int
    physical_row: int
    zh: str
    timing: str
    english: str
    revised: str
    final: str
    used_revised: bool
    invalid_revised_reason: str | None
    source_sha256: str
    cue_fingerprint: str
    issues: list[dict[str, Any]] = field(default_factory=list)


def _issue(
    code: str,
    message: str,
    *,
    severity: str,
    row: int | None = None,
    index: int | None = None,
    action: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "code": code,
        "message": message,
        "severity": severity,
    }
    if row is not None:
        value["row"] = row
    if index is not None:
        value["index"] = index
    if action:
        value["action"] = action
    return value


def _resolve_row(
    row: ReviewRow,
    *,
    profile: dict[str, Any],
) -> ResolvedReviewRow:
    index = parse_review_index(row.values.get("index"), row_number=row.physical_row)
    english_raw = row.get("english")
    revised_raw = row.get("revised")
    english = clean_subtitle_text(english_raw, profile)
    invalid_reason = invalid_revised_reason(revised_raw, profile)
    issues: list[dict[str, Any]] = []
    if revised_raw and is_formula_like(revised_raw):
        invalid_reason = "formula_like_revision"
    elif revised_raw and CJK_RE.search(revised_raw):
        invalid_reason = "cjk_revision"
    revised_clean = clean_subtitle_text(revised_raw, profile) if revised_raw else ""
    if revised_clean:
        revised_text_issues = validate_final_text(revised_clean, profile)
        serious = [
            issue
            for issue in revised_text_issues
            if issue.code not in {"terminal_punctuation"}
        ]
        if serious and invalid_reason is None:
            invalid_reason = serious[0].code
    used_revised = bool(revised_raw) and invalid_reason is None
    final = revised_clean if used_revised else english
    revised = revised_clean if used_revised else ""
    return ResolvedReviewRow(
        index=index,
        physical_row=row.physical_row,
        zh=row.get("zh"),
        timing=row.get("timing"),
        english=english,
        revised=revised,
        final=final,
        used_revised=used_revised,
        invalid_revised_reason=invalid_reason,
        source_sha256=row.get("source_sha256"),
        cue_fingerprint=row.get("cue_fingerprint"),
        issues=issues,
    )


def audit_review_table(
    table: ReviewTable,
    *,
    source: SrtDocument | None,
    profile: dict[str, Any],
    strict: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[int, str]]:
    issues: list[dict[str, Any]] = []
    resolved_rows: list[ResolvedReviewRow] = []
    seen: dict[int, int] = {}
    invalid_revisions = 0
    valid_revisions = 0
    fallback_count = 0

    if source is None:
        issues.append(
            _issue(
                "source_not_provided",
                "Review audit is not bound to an original SRT",
                severity="error" if strict else "warning",
            )
        )

    for row in table.rows:
        raw_index = row.values.get("index")
        if raw_index is None or not str(raw_index).strip():
            issues.append(
                _issue(
                    "missing_review_index",
                    "Nonblank review row has no cue index",
                    severity="error",
                    row=row.physical_row,
                )
            )
            continue
        try:
            resolved = _resolve_row(row, profile=profile)
        except SubtitleError as exc:
            issues.append(
                _issue(
                    exc.code,
                    exc.message,
                    severity="error",
                    row=row.physical_row,
                )
            )
            continue
        if resolved.index in seen:
            issues.append(
                _issue(
                    "duplicate_review_index",
                    f"Cue {resolved.index} appears more than once",
                    severity="error",
                    row=resolved.physical_row,
                    index=resolved.index,
                )
            )
        else:
            seen[resolved.index] = resolved.physical_row

        if resolved.invalid_revised_reason:
            invalid_revisions += 1
            fallback_count += 1
            severity = (
                "warning"
                if resolved.invalid_revised_reason
                in {"known_placeholder", "numeric_only"}
                else "error"
            )
            issues.append(
                _issue(
                    "invalid_revised_value",
                    f"Invalid revised English: {resolved.invalid_revised_reason}",
                    severity=severity,
                    row=resolved.physical_row,
                    index=resolved.index,
                    action="fell back to 英文",
                )
            )
        elif resolved.used_revised and resolved.revised != resolved.english:
            valid_revisions += 1

        final_issues = validate_final_text(resolved.final, profile)
        for final_issue in final_issues:
            issues.append(
                _issue(
                    final_issue.code,
                    final_issue.message,
                    severity="error",
                    row=resolved.physical_row,
                    index=resolved.index,
                )
            )
        resolved_rows.append(resolved)

    if source is not None:
        expected = [cue.index for cue in source.cues]
        actual = [row.index for row in resolved_rows]
        missing = [index for index in expected if index not in seen]
        extra = sorted(set(actual) - set(expected))
        if missing:
            issues.append(
                _issue(
                    "missing_review_cues",
                    f"Review table is missing source cues: {missing}",
                    severity="error",
                )
            )
        if extra:
            issues.append(
                _issue(
                    "extra_review_cues",
                    f"Review table has unknown cues: {extra}",
                    severity="error",
                )
            )
        if not missing and not extra and actual != expected:
            issues.append(
                _issue(
                    "review_order_changed",
                    "Review cue order does not match the source SRT",
                    severity="error",
                )
            )

        source_by_index = {cue.index: cue for cue in source.cues}
        table_hash = str(table.metadata.get("source_sha256") or "")
        if table_hash and table_hash != source.source_sha256:
            issues.append(
                _issue(
                    "source_hash_mismatch",
                    "Review workbook metadata belongs to a different source SRT",
                    severity="error",
                )
            )
        elif not table_hash and strict:
            # CSV review files carry the hash per row instead of a metadata sheet.
            row_hashes = {row.source_sha256 for row in resolved_rows if row.source_sha256}
            if not row_hashes:
                issues.append(
                    _issue(
                        "source_hash_missing",
                        "Strict review audit requires source-hash metadata",
                        severity="error",
                    )
                )
        for resolved in resolved_rows:
            cue = source_by_index.get(resolved.index)
            if cue is None:
                continue
            if normalize_space(resolved.zh) != normalize_space(cue.text):
                issues.append(
                    _issue(
                        "source_text_mismatch",
                        "Review Chinese text differs from the source cue",
                        severity="error",
                        row=resolved.physical_row,
                        index=resolved.index,
                    )
                )
            if resolved.timing.strip() != cue.raw_timing.strip():
                issues.append(
                    _issue(
                        "source_timing_mismatch",
                        "Review timing differs from the source cue",
                        severity="error",
                        row=resolved.physical_row,
                        index=resolved.index,
                    )
                )
            if resolved.source_sha256 and resolved.source_sha256 != source.source_sha256:
                issues.append(
                    _issue(
                        "source_hash_mismatch",
                        "Review row belongs to a different source SRT",
                        severity="error",
                        row=resolved.physical_row,
                        index=resolved.index,
                    )
                )
            if resolved.cue_fingerprint and resolved.cue_fingerprint != cue.fingerprint:
                issues.append(
                    _issue(
                        "cue_fingerprint_mismatch",
                        "Review row fingerprint differs from the source cue",
                        severity="error",
                        row=resolved.physical_row,
                        index=resolved.index,
                    )
                )

    blockers = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    clean_rows: list[dict[str, Any]] = []
    translations: dict[int, str] = {}
    source_by_index = {cue.index: cue for cue in source.cues} if source else {}
    for resolved in resolved_rows:
        cue = source_by_index.get(resolved.index)
        zh = cue.text if cue else resolved.zh
        timing = cue.raw_timing if cue else resolved.timing
        fingerprint = cue.fingerprint if cue else resolved.cue_fingerprint
        source_hash = source.source_sha256 if source else resolved.source_sha256
        clean_rows.append(
            {
                "序号": resolved.index,
                "中文": zh,
                "英文": resolved.english,
                "英文修改版": resolved.revised,
                "时间线": timing,
                "__source_sha256": source_hash,
                "__cue_fingerprint": fingerprint,
                "__schema_version": REVIEW_SCHEMA,
            }
        )
        if resolved.final:
            translations[resolved.index] = resolved.final

    report = {
        "schema_version": REVIEW_AUDIT_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "created_at": utc_now_iso(),
        "passed": not blockers,
        "strict": strict,
        "review_table": table.path,
        "source": source.source_manifest() if source else None,
        "summary": {
            "total_rows": len(table.rows),
            "resolved_rows": len(resolved_rows),
            "valid_revisions": valid_revisions,
            "invalid_revisions": invalid_revisions,
            "fallback_count": fallback_count,
            "revision_rate": round(valid_revisions / len(resolved_rows), 6)
            if resolved_rows
            else 0.0,
            "errors": len(blockers),
            "warnings": len(warnings),
        },
        "issues": issues,
    }
    return report, clean_rows, translations
