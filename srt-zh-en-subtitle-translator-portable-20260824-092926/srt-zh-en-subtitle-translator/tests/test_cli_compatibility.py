from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tests.support import (
    FIXTURES,
    STYLE,
    minimal_srt,
    read_csv,
    run_cli,
    write_text,
)


class CliCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_help_preserves_legacy_commands_and_exposes_v2_gates(self) -> None:
        result = run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        for command in (
            "extract",
            "prepare",
            "text",
            "apply",
            "export-review",
            "deliver",
            "audit-review",
            "apply-review",
            "learn",
            "validate",
            "doctor",
            "finalize",
        ):
            with self.subTest(command=command):
                self.assertIn(command, output)

    def test_doctor_passes_with_xlsx_dependency_and_writable_workspace(self) -> None:
        result = run_cli(
            "doctor",
            "--require-xlsx",
            "--work-dir",
            self.work,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout.upper())

    def test_legacy_translation_list_still_applies_exactly(self) -> None:
        source = FIXTURES / "golden" / "source.zh.srt"
        translations = FIXTURES / "golden" / "translations.legacy.json"
        expected = FIXTURES / "golden" / "expected.en.srt"
        output = self.work / "legacy.en.srt"
        result = run_cli(
            "apply",
            source,
            translations,
            output,
            "--style-profile",
            STYLE,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            output.read_text(encoding="utf-8"),
            expected.read_text(encoding="utf-8"),
        )

    def test_extract_and_text_legacy_commands_remain_usable(self) -> None:
        source = self.work / "source.srt"
        extracted = self.work / "extracted.json"
        write_text(source, minimal_srt(["第一句", "第二句"]))
        run_cli("extract", source, extracted, check=True)
        values = json.loads(extracted.read_text(encoding="utf-8"))
        self.assertEqual([item["index"] for item in values], [1, 2])

        text_result = run_cli("text", source)
        self.assertEqual(text_result.returncode, 0, text_result.stderr)
        self.assertIn("1. 第一句", text_result.stdout)
        self.assertIn("2. 第二句", text_result.stdout)

    def test_export_review_keeps_visible_legacy_columns_first(self) -> None:
        source = FIXTURES / "golden" / "source.zh.srt"
        translations = FIXTURES / "golden" / "translations.legacy.json"
        review = self.work / "review.csv"
        run_cli("export-review", source, translations, review, check=True)
        fields, rows = read_csv(review)
        self.assertEqual(fields[:5], ["序号", "中文", "英文", "英文修改版", "时间线"])
        self.assertEqual(len(rows), 5)


if __name__ == "__main__":
    unittest.main()
