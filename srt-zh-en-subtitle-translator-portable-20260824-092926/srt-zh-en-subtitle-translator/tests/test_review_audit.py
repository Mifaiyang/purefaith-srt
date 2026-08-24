from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

try:
    import openpyxl
except ImportError:  # pragma: no cover - exercised in dependency-light runtimes
    openpyxl = None

from tests.support import (
    STYLE,
    minimal_srt,
    prepare_source,
    read_csv,
    read_json,
    run_cli,
    write_bound_translations,
    write_csv,
    write_text,
)


class ReviewAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.source = self.work / "source.zh.srt"
        write_text(self.source, minimal_srt(["第一句", "投入了12万元", "最后一句"]))
        prepared = prepare_source(self.source, self.work / "prepared.json")
        self.translations = self.work / "translations.json"
        write_bound_translations(
            self.translations,
            prepared,
            [
                (1, "First line"),
                (2, "We invested RMB 120,000"),
                (3, "Final line"),
            ],
        )
        self.review = self.work / "review.csv"
        self.export_report = self.work / "export-report.json"
        run_cli(
            "export-review",
            self.source,
            self.translations,
            self.review,
            "--style-profile",
            STYLE,
            "--require-source-binding",
            "--report",
            self.export_report,
            check=True,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def audit(self, review: Path, label: str, *, strict: bool = True):
        report = self.work / f"{label}.audit.json"
        clean = self.work / f"{label}.clean.csv"
        args: list[object] = [
            "audit-review",
            review,
            report,
            "--source",
            self.source,
            "--clean-output",
            clean,
            "--style-profile",
            STYLE,
        ]
        if strict:
            args.append("--strict")
        result = run_cli(*args)
        return result, report, clean

    def mutate_review(self, label: str, mutation) -> Path:
        fields, rows = read_csv(self.review)
        mutation(fields, rows)
        output = self.work / f"{label}.csv"
        write_csv(output, fields, rows)
        return output

    def assert_report_mentions(self, report_path: Path, *terms: str) -> dict:
        self.assertTrue(report_path.exists(), f"missing report: {report_path}")
        report = read_json(report_path)
        serialized = json.dumps(report, ensure_ascii=False).casefold()
        self.assertTrue(
            any(term.casefold() in serialized for term in terms),
            f"none of {terms!r} found in {serialized}",
        )
        return report

    def test_source_hash_mismatch_is_a_hard_failure(self) -> None:
        def mutate(_fields, rows):
            for row in rows:
                row["__source_sha256"] = "0" * 64

        review = self.mutate_review("bad-hash", mutate)
        result, report, _clean = self.audit(review, "bad-hash")
        self.assertNotEqual(result.returncode, 0)
        self.assert_report_mentions(report, "source_hash", "source sha", "source mismatch")

    def test_review_exported_for_a_different_source_is_rejected(self) -> None:
        wrong_source = self.work / "wrong.zh.srt"
        write_text(wrong_source, minimal_srt(["完全不同", "不是原稿", "第三句"]))
        report = self.work / "wrong-source.audit.json"
        result = run_cli(
            "audit-review",
            self.review,
            report,
            "--source",
            wrong_source,
            "--strict",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assert_report_mentions(report, "source", "fingerprint")

    def test_duplicate_fractional_and_missing_rows_are_rejected(self) -> None:
        mutations = {
            "duplicate": lambda _fields, rows: rows.append(dict(rows[0])),
            "fractional": lambda _fields, rows: rows[0].__setitem__("序号", "1.5"),
            "missing": lambda _fields, rows: rows.pop(),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                review = self.mutate_review(label, mutation)
                result, report, _clean = self.audit(review, label)
                self.assertNotEqual(result.returncode, 0)
                self.assert_report_mentions(
                    report,
                    label,
                    "row",
                    "index",
                    "missing",
                    "duplicate",
                )

    def test_numeric_placeholder_falls_back_but_legitimate_number_is_kept(self) -> None:
        placeholder = self.mutate_review(
            "placeholder",
            lambda _fields, rows: rows[1].__setitem__("英文修改版", "12"),
        )
        _result, report, clean = self.audit(placeholder, "placeholder")
        self.assert_report_mentions(report, "numeric", "placeholder", "fallback")
        _fields, cleaned_rows = read_csv(clean)
        self.assertEqual(cleaned_rows[1]["英文"], "We invested RMB 120,000")
        self.assertFalse(cleaned_rows[1]["英文修改版"])

        legitimate = self.mutate_review(
            "legitimate-number",
            lambda _fields, rows: rows[1].__setitem__(
                "英文修改版", "We invested RMB 12 million"
            ),
        )
        result, legitimate_report, legitimate_clean = self.audit(
            legitimate, "legitimate-number"
        )
        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        report_data = read_json(legitimate_report)
        serialized = json.dumps(report_data, ensure_ascii=False).casefold()
        self.assertNotIn("numeric_only", serialized)
        _fields, cleaned_rows = read_csv(legitimate_clean)
        self.assertEqual(
            cleaned_rows[1]["英文修改版"],
            "We invested RMB 12 million",
        )

    def test_formula_and_chinese_revisions_never_become_final(self) -> None:
        cases = {
            "formula": "=SUM(1,1)",
            "chinese": "全部中文",
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                review = self.mutate_review(
                    label,
                    lambda _fields, rows, value=value: rows[0].__setitem__(
                        "英文修改版", value
                    ),
                )
                _result, report, clean = self.audit(review, label)
                self.assert_report_mentions(
                    report,
                    label,
                    "formula",
                    "chinese",
                    "non_english",
                    "fallback",
                )
                _fields, rows = read_csv(clean)
                self.assertEqual(rows[0]["英文"], "First line")
                self.assertFalse(rows[0]["英文修改版"])

    def test_punctuation_only_revision_is_rejected(self) -> None:
        review = self.mutate_review(
            "punctuation-only",
            lambda _fields, rows: rows[0].__setitem__("英文修改版", "!?"),
        )
        _result, report, clean = self.audit(review, "punctuation-only")
        self.assert_report_mentions(report, "punctuation", "empty", "fallback")
        _fields, rows = read_csv(clean)
        self.assertEqual(rows[0]["英文"], "First line")
        self.assertFalse(rows[0]["英文修改版"])

    def test_tampered_timeline_and_chinese_cells_are_rejected(self) -> None:
        cases = {
            "timeline": ("时间线", "00:00:09,000 --> 00:00:10,000"),
            "source-text": ("中文", "被篡改"),
        }
        for label, (column, value) in cases.items():
            with self.subTest(label=label):
                review = self.mutate_review(
                    label,
                    lambda _fields, rows, column=column, value=value: rows[0].__setitem__(
                        column, value
                    ),
                )
                result, report, _clean = self.audit(review, label)
                self.assertNotEqual(result.returncode, 0)
                self.assert_report_mentions(report, "tamper", "mismatch", column)

    @unittest.skipIf(openpyxl is None, "openpyxl is not installed")
    def test_legacy_xlsx_title_rows_and_subtitle_header_aliases_are_supported(self) -> None:
        workbook_path = self.work / "legacy-review.xlsx"
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["中英字幕校验表"])
        sheet.append(["如需修改，请填写英文修改版"])
        sheet.append(["序号", "中文字幕", "英文字幕", "英文修改版", "时间线"])
        sheet.append(
            [
                1,
                "第一句",
                "First line",
                "",
                "00:00:00,000 --> 00:00:01,500",
            ]
        )
        sheet.append(
            [
                2,
                "投入了12万元",
                "We invested RMB 120,000",
                "",
                "00:00:02,000 --> 00:00:03,500",
            ]
        )
        sheet.append(
            [
                3,
                "最后一句",
                "Final line",
                "",
                "00:00:04,000 --> 00:00:05,500",
            ]
        )
        workbook.save(workbook_path)

        output = self.work / "legacy-final.srt"
        result = run_cli(
            "apply-review",
            self.source,
            workbook_path,
            output,
            "--style-profile",
            STYLE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        run_cli("validate", self.source, output, "--style-profile", STYLE, check=True)


if __name__ == "__main__":
    unittest.main()
